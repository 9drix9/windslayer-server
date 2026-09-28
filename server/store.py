#!/usr/bin/env python3
"""
store.py - the account/character store (roadmap 1.5 F4: lc-data-model)
======================================================================
accounts.json stays the backend through P7 (D20). This module is its only reader and
writer; GameServer keeps no separate copy.

    st = Store('accounts.json', debounce_secs=2.0).load()   # migrates + backs up once
    st.attach(scheduler)                     # debounced saves on the tick thread (F8)
    with st.lock:                            # db_lock: every read-modify-write
        char['exp'] = 60
    st.mark_dirty('exp')                     # on disk within debounce_secs
    st.save_now()                            # create/delete: immediately

Guarantees
----------
- Atomic write (fixes S1-08b / login_character B17): JSON goes to a temp file next to
  accounts.json, is fsynced, then os.replace()s the old file. A crash or exception at any
  point leaves either the old or the new complete file, never a torn one.
- db_lock (RLock) guards the dict and the write, so a save serializes one consistent
  snapshot; writes never interleave.
- Debounce: the first mark_dirty() after a save schedules one flush debounce_secs later
  (a change is on disk at most that long after it was made); autosave every autosave_secs
  as a backstop. Without a scheduler, callers flush explicitly.
- One-shot migration at load (F4 "Migration", login_character.md 3.7), idempotent per
  record: a character with a `look` array and an `exp` total, and an account with a valid
  unique `uid` and an already hashed password, are left as they are. Before the first
  migration rewrites an existing file, its original bytes are copied once to
  accounts.json.bak-pre-p1.
- Passwords are hashed at rest (auth.py, lc-login-errors). A hand-added account may still
  be written as {"password": "abc"}: login accepts it and the next load hashes it in place.

Schema written by this stage (F4; later owners add their own fields to the same records)
----------------------------------------------------------------------------------------
account:   password (pbkdf2 hash, auth.py; lc-login-errors), uid (1..0xEFFFF, per account,
           F3), gender (0x02 flag), manner (i32), banned, deleted, characters[<= 5]
character: name, created_at, class, job2, exp (u32 total, the only truth; level is always
           derived via progression.level_for_exp), look[14], str/dex/int/spr, fame,
           map, x, y, hp, mp, gm, gm_hidden
P2 adds, through the owning modules' own `ensure()` (so one normalizer serves the
migration and every runtime mutation):
character: gold (u64), victy (u32), inventory{equip[], consume{}, etc{}, tab_slots[3]},
           equipped{slot: {id, w[6]}}          -> inventory.py (item_inventory-model-persist,
                                                  shop_storage-wallet, trade-economy-persist)
           quests{active[3], progress[3], completed[[id, times]]}, card_deck[]
                                               -> quests.py (quest_cards_misc-quest-state-model)
P3 adds (cs-skill-learn, cs-buffs), through skills.ensure / buffs.ensure:
character: skills[<= 30 exact learned ids, the 0x07 skill list], buffs[{id, remaining_ms,
           x, y, src} <= 21, the 0x07 buff list; remaining time frozen while offline]
P4 adds (shop_storage-bank-model), through bank_tabs.ensure:
character: bank_slots[3] (S2C 0x65 capacities 0..60, default 35), bank{equip[{id, w[6]} |
           None], consume[{id, qty} | None], etc[...], gold u64} - slot-ordered like the
           client's own u16[60] arrays. account.second_password (optional, never written):
           when set, C2S 0x51 compares against it instead of the login password.
P5 adds (chat_mail_gm-privacy-flags; D13), through privacy.ensure:
character: refuse{whisper, exchange, party, talk, friend} (bools; the C2S 0x2B bytes 0-4 /
           C2S 0x40 of the character's last session). The first load that adds it writes
           the one-time accounts.json.bak-pre-p5.
P6 adds (social_friend-persistence, P6 stage 2), through social.ensure / social.ensure_account:
character: friends[names], friend_capacity (20..50), mentor (name | null), mentees[names],
           memos[{id, from, text, t}] (<= 100), memo_seq (the last memo id: ids never reused)
account:   social{compliment_given_day, complimented_accounts, compliments_received, report_day}
           (the P7 compliment / report limits, created empty). The first load that adds them
           writes the one-time accounts.json.bak-pre-p6.
P7 adds (shop_storage-stall-registry, P7 stage 2), through stall.ensure:
character: stall_escrow[{id, qty, price, w?}] (the items of the character's open flea-market
           stall, out of the bag while it sells; with no stall open, what a full bag could not
           take back at a close - merged back into the bag at the next Start or login,
           market.Market._restore_kept / recover). The first load that adds it
           writes the one-time accounts.json.bak-pre-p7.
`map`, `x`, `y`, `hp` and `mp` existed but were never written back (world-persistence): the
server now saves them on every map transfer, on disconnect and on the world-state tick.
client-2009-login adds, only in a store opened for the 2009 build (Store(client_build='2009'),
config CLIENT_BUILD), through migrate_character_2009 / new_character:
character: gender (0/1: the 2009 client keeps gender per character - S2C 0x02 record bool at
           entity+0x11B, chosen in C2S 0x0E; defaults to the account's 2008 gender flag),
           look_ext[3] (appearance words 14..16 of the 2009 17-word array; 0..13 stay `look`)
The first 2009 load that adds them writes the one-time accounts.json.bak-pre-client2009. A
2008 store neither writes nor needs them, so both builds can share one accounts.json.
Legacy appearance keys (face/top/bottom/shoes/hair/weapon/head) on migrated records are
kept but no longer written: since lc-charlist / lc-spawn-fields the 0x02 and 0x07 builders
send `look` verbatim (records.py). `look` (+ `look_ext`) is the COMPOSED appearance: every
equip / unequip applies the client's own composition routine to it (inventory.compose), and
a look stored before that is composed from the worn items once, at the next login
(GameServer._compose_looks; idempotent, no schema change). `weapon` is only a derived copy
of look word 11 on the records that have it (nothing reads it; an older server sharing the
file would still render the same hand).
"""
import json
import logging
import os
import threading
import time

import auth
import bank_tabs as bankmod
import buffs as buffmod
import ids
import inventory as invmod
import privacy
import progression
import quests as questmod
import skills as skillmod
import social
import stall as stallmod

log = logging.getLogger('WS')

BACKUP_SUFFIX = '.bak-pre-p1'
# One backup per phase that changed the persisted schema, written from the file's ORIGINAL
# bytes the first time a migration rewrites it. Each name is written at most once ever, so
# re-running a migration (which is itself idempotent) never overwrites an earlier snapshot.
BACKUP_SUFFIXES = (BACKUP_SUFFIX, '.bak-pre-p2', '.bak-pre-p3', '.bak-pre-p4', '.bak-pre-p5',
                   '.bak-pre-p6', '.bak-pre-p7')
# client-2009-login: the 2009 schema step (per-character gender, look_ext) - written only by
# a store of the 2009 build (Store.backup_paths).
BACKUP_SUFFIX_2009 = '.bak-pre-client2009'
BUILD_2009 = '2009'
MAX_CHARACTERS = 5
LOOK_SLOTS = 14
# The 2009 appearance array is 17 u16 (spec_2009 0x02/0x07 repeat(17)); 0..13 keep their 2008
# meaning (`look`), 14..16 are new (spec_2009 0x02 open question) and live in `look_ext`.
LOOK_EXT_SLOTS = 3

# Creation defaults of window 0x13 (FUN_00446300): gender flag 0 -> s10=s1=1, s6=s5=s9=2;
# flag != 0 -> the 101-based set (s9 stays 2).
DEFAULT_LOOK_SLOTS = {0: {'s10': 1, 's1': 1, 's6': 2, 's5': 2, 's9': 2},
                      1: {'s10': 0x65, 's1': 0x65, 's6': 0x66, 's5': 0x66, 's9': 2}}
# The old create handler stored look slot 10 (the body) as `class` (B7): 1..4, or 101..104.
LEGACY_BODY_IDS = frozenset(range(1, 5)) | frozenset(range(0x65, 0x69))
DEFAULT_STATS = {'str': 3, 'dex': 2, 'int': 1, 'spr': 3}      # sum 9 = stat_total(1)
# Fallback CURRENT HP/MP for a legacy record that has none (the migration writes them). New
# characters are born at their computed maxima instead (hpmp.new_character_vitals); a
# maximum is never stored and never read from here (cs-hp-mp-model).
DEFAULT_HP, DEFAULT_MP = 100, 50
# Starting wallet (shop_storage-wallet). It is a one-time balance written into the record,
# NOT a display default: the 999999 session default and the 100000 constant the old 0x03
# sent are both gone, and the client now only ever shows what is in the file. 100000 keeps
# the balance a migrated character has been seeing.
DEFAULT_GOLD, DEFAULT_VICTY = 100000, 0
# What a record gets for a field it does not have yet. Store overrides it from the config
# (START_MAP/X/Y, START_GOLD/START_VICTY) through record_defaults().
DEFAULT_RECORD = {'gold': DEFAULT_GOLD, 'victy': DEFAULT_VICTY,
                  'map': 101, 'x': 700.0, 'y': 812.0}
# chat_mail_gm-gm-flag-manner (F10.0): `gm` is the u16 gm_level of the 0x07 record and the
# only value the client's GM parser accepts is 1 (handler gate 0x44F093); `gm_hidden` is
# the bool that follows it. Both are written on every character so that flipping a GM on
# is editing a 0 to a 1 in accounts.json with the server stopped, as F10.0 step 1 asks.
GM_LEVEL_ON = 1
DEFAULT_GM = {'gm': 0, 'gm_hidden': 0}
# The account manner i32 (0x02 manner_points, 0x07/0x04/0x05 karma). Clamped to the wire
# type: the client reads it as a signed 32-bit value and shows it in the status window.
MANNER_MIN, MANNER_MAX = -0x80000000, 0x7FFFFFFF


def compose_look(s10, s1, s6, s5, s9):
    """The 14-word appearance exactly as the client builds it after S2C 0x1C (OnReceive):
    [0, s1, 0, 0, s10, s5, s6, 0, 0, s9, s10, 0, 0, s10]."""
    s10, s1, s6, s5, s9 = (int(v) & 0xFFFF for v in (s10, s1, s6, s5, s9))
    return [0, s1, 0, 0, s10, s5, s6, 0, 0, s9, s10, 0, 0, s10]


def default_look(gender=0):
    return compose_look(**DEFAULT_LOOK_SLOTS[1 if gender else 0])


class StoreError(RuntimeError):
    pass


def snapshot(value):
    """An independent copy of a nested JSON structure that a concurrent mutation cannot
    break, for callers that walk a record no lock is held on (the save).

    Handler threads mutate the live records (a bag add, a wallet debit, ensure() repairing
    a malformed field) without holding `Store.lock`, and every container copy here is one C
    call - `list(d.items())`, `list(l)` - which no other thread can interleave with. Walking
    the live structure with Python-level iteration instead (json.dumps, copy.deepcopy)
    raises "dictionary changed size during iteration" as soon as a nested dict grows a key
    mid-walk, which is exactly what save_now did under load. A value that changes between
    two container copies simply lands one save late; nothing is ever torn."""
    if isinstance(value, dict):
        return {k: snapshot(v) for k, v in list(value.items())}
    if isinstance(value, (list, tuple)):
        return [snapshot(v) for v in list(value)]
    return value


def _json_shape(value):
    """An independent COPY of `value` in the shape a JSON round-trip leaves it: dict keys
    as strings, tuples as lists. The normalizers key their stacks by int id ({5: 12}) and
    the file gives them back as strings ({'5': 12}), so only this form can tell a real
    repair from that round-trip - and only a copy survives a repair made in place."""
    if isinstance(value, dict):
        return {str(k): _json_shape(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_shape(v) for v in value]
    return value


# ---------------------------------------------------------------- migration ---
def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_MISSING = object()


def migrate_character(char, gender=0, defaults=None):
    """Bring one character record to the lc-data-model schema. Returns the change list
    (empty when the record already conforms)."""
    changes = []
    name = char.get('name', '?')
    if not isinstance(char.get('look'), list) or len(char['look']) != LOOK_SLOTS:
        slots = dict(DEFAULT_LOOK_SLOTS[1 if gender else 0])
        old_class = _as_int(char.get('class'), 0)
        if old_class in LEGACY_BODY_IDS:
            slots['s10'] = old_class
        for slot, keys in (('s1', ('face', 'hair')), ('s6', ('top',)), ('s5', ('bottom',)), ('s9', ('shoes',))):
            for key in keys:
                if _as_int(char.get(key), 0):
                    slots[slot] = _as_int(char[key])
                    break
        char['look'] = compose_look(**slots)
        changes.append(f'{name}: look {char["look"]} (legacy class {old_class})')
        # EN characters are born Novice; the old `class` was the body look (S2-24, B7).
        if old_class != 0 or 'class' not in char:
            changes.append(f'{name}: class {char.get("class")} -> 0')
        char['class'] = 0
        char['job2'] = 0
    for key, default in (('class', 0), ('job2', 0), ('created_at', 0), ('fame', 0)):
        if key not in char:
            char[key] = default
            changes.append(f'{name}: {key} = {default}')
    if 'exp' not in char:
        level = _as_int(char.get('level'), 1)
        char['exp'] = progression.exp_for_level(level)
        changes.append(f'{name}: exp {char["exp"]} from level {level}')
    elif char['exp'] != progression.clamp_exp(_as_int(char['exp'])):
        char['exp'] = progression.clamp_exp(_as_int(char['exp']))
        changes.append(f'{name}: exp clamped to {char["exp"]}')
    if 'level' in char:
        # Level is derived from exp everywhere; a stored level would be a second truth.
        del char['level']
        changes.append(f'{name}: stored level dropped (derived from exp)')
    for key, default in DEFAULT_STATS.items():
        if key not in char:
            char[key] = default
            changes.append(f'{name}: {key} = {default}')
    for key, default in (('hp', DEFAULT_HP), ('mp', DEFAULT_MP)):
        if key not in char:
            char[key] = default
            changes.append(f'{name}: {key} = {default}')
    # GM identity (chat_mail_gm-gm-flag-manner, F10.0): written as 0 so the flag is visible
    # in the file; a hand-set gm/gm_hidden is normalized to an int and never reset.
    for key, default in DEFAULT_GM.items():
        value = _as_int(char.get(key), default) if key in char else default
        if char.get(key) != value:
            changes.append(f'{name}: {key} = {value}')
        char[key] = value
    # P2 (item_inventory-model-persist / shop_storage-wallet / quest_cards_misc-quest-state-model):
    # the wallet starts at the configured balance only the FIRST time (a stored 0 is a real
    # balance, not "missing"), and the item/quest normalizers create or repair their own
    # fields. Both are idempotent, so a second load leaves a conforming record untouched.
    start = dict(DEFAULT_RECORD)
    start.update(defaults or {})
    for key in ('gold', 'victy'):
        if key not in char:
            char[key] = int(start[key])
            changes.append(f'{name}: {key} = {char[key]}')
    # world-persistence (F4 migration bullet 5): a record written before the start point was
    # configurable sits on map 0 at (100,100), which is not a map the client can load; enter
    # world already substituted START_MAP, so the stored point is moved with it.
    if not _as_int(char.get('map')):
        char['map'], char['x'], char['y'] = int(start['map']), float(start['x']), float(start['y'])
        changes.append(f'{name}: map 0 -> {char["map"]} ({char["x"]}, {char["y"]})')
    # A COPY of what is stored: both ensure()s repair their fields in place, so comparing
    # the objects themselves always said "unchanged" and a malformed bag or quest log was
    # silently repaired with no change line - hence no save and no .bak-pre-p2 of the file
    # that held it.
    fields = ('inventory', 'equipped', 'quests', 'card_deck', 'skills', 'buffs', 'bank', 'bank_slots',
              'refuse')
    before = tuple(_json_shape(char.get(k)) for k in fields)
    invmod.ensure(char)
    questmod.ensure(char)
    # P3 (cs-skill-learn / cs-buffs): the learned list and the persisted buffs. Both
    # normalizers are structural (no catalog lookups), so a server started without the EN
    # hii never drops a real skill here; the first load that adds them writes .bak-pre-p3.
    skillmod.ensure(char)
    buffmod.ensure(char)
    # P4 (shop_storage-bank-model): the per-character bank - slot-ordered tabs, bank gold and
    # the three S2C 0x65 capacities. Structural too; the first load that adds them writes
    # .bak-pre-p4.
    bankmod.ensure(char)
    # P5 (chat_mail_gm-privacy-flags): the five refuse flags, kept per character so a check
    # against a character between sessions has an answer (the client re-sends them in every
    # C2S 0x2B). Structural; the first load that adds them writes .bak-pre-p5.
    privacy.ensure(char)
    after = tuple(_json_shape(char[k]) for k in fields)
    for label, old, new in zip(fields, before, after):
        if old != new:
            changes.append(f'{name}: {label} normalized' if old is not None else f'{name}: {label} created')
    # P6 (social_friend-persistence): the messenger fields. A missing key is told apart from a
    # stored null (`mentor`), so a record without them is written (and backed up) once.
    before = {k: _json_shape(char[k]) if k in char else _MISSING for k in social.CHAR_FIELDS}
    social.ensure(char)
    for label in social.CHAR_FIELDS:
        old, new = before[label], _json_shape(char[label])
        if old is _MISSING:
            changes.append(f'{name}: {label} created')
        elif old != new:
            changes.append(f'{name}: {label} normalized')
    # P7 (shop_storage-stall-registry): the persisted stall escrow (crash safety, F14.5).
    # Structural; the first load that adds it writes .bak-pre-p7.
    had = _json_shape(char['stall_escrow']) if 'stall_escrow' in char else _MISSING
    stallmod.ensure(char)
    if had is _MISSING:
        changes.append(f'{name}: stall_escrow created')
    elif had != _json_shape(char['stall_escrow']):
        changes.append(f'{name}: stall_escrow normalized')
    return changes


def migrate_character_2009(char, gender=0):
    """The 2009 schema step of one character (client-2009-login). Idempotent; returns the
    change list. `gender` is the per-character gender the 2009 client shows and gates
    "(M)"/"(F)" items with (spec_2009 0x02 record `gender`, entity+0x11B): an existing record
    takes its account's 2008 gender flag, which is what the 2008 build rendered it with.
    `look_ext` holds appearance words 14..16 (unknown meaning, 0 = nothing)."""
    changes = []
    name = char.get('name', '?')
    value = 1 if _as_int(char.get('gender'), 1 if gender else 0) else 0
    if char.get('gender') != value:
        changes.append(f'{name}: gender = {value}')
        char['gender'] = value
    ext = char.get('look_ext')
    fixed = [_as_int(v) & 0xFFFF for v in ext][:LOOK_EXT_SLOTS] if isinstance(ext, list) else []
    fixed += [0] * (LOOK_EXT_SLOTS - len(fixed))
    if ext != fixed:
        changes.append(f'{name}: look_ext {fixed}' if ext is None else f'{name}: look_ext normalized')
        char['look_ext'] = fixed
    return changes


def migrate_accounts(accounts, hash_passwords=True, defaults=None, client_build=None):
    """Migrate every account and character in place. Returns the change list.
    Uids: test = 1, admin = 2, every other account without a valid unique uid gets
    max + 1 in file order (F3). With `hash_passwords`, every plaintext password is
    replaced by its pbkdf2 hash once (lc-login-errors); an already hashed record is left
    alone, so the migration is idempotent and re-running it costs nothing.
    client_build '2009' also runs migrate_character_2009 on every character."""
    if not isinstance(accounts, dict):
        raise StoreError('accounts file: the top level must be an object keyed by login id')
    changes = []
    seen = set()
    needs_uid = []
    for username, acc in accounts.items():
        if not isinstance(acc, dict):
            raise StoreError(f'account {username!r}: record must be an object')
        uid = acc.get('uid')
        if isinstance(uid, int) and not isinstance(uid, bool) and uid in ids.PLAYER and uid not in seen \
                and not (uid in ids.FIXED_PLAYER_UIDS.values() and ids.FIXED_PLAYER_UIDS.get(username) != uid):
            seen.add(uid)
        else:
            if 'uid' in acc:
                changes.append(f'{username}: invalid or duplicate uid {uid!r} replaced')
            needs_uid.append(username)
    for username in needs_uid:
        fixed = ids.FIXED_PLAYER_UIDS.get(username)
        uid = fixed if fixed is not None and fixed not in seen else ids.next_player_uid(seen)
        accounts[username]['uid'] = uid
        seen.add(uid)
        changes.append(f'{username}: uid {uid}')
    for username, acc in accounts.items():
        for key, default in (('gender', 0), ('manner', 0), ('banned', False), ('deleted', False)):
            if key not in acc:
                acc[key] = default
                changes.append(f'{username}: {key} = {default}')
        # P6 (social_friend-persistence): the account-wide compliment / report limits.
        had = _json_shape(acc['social']) if 'social' in acc else _MISSING
        social.ensure_account(acc)
        if had is _MISSING:
            changes.append(f'{username}: social created')
        elif had != _json_shape(acc['social']):
            changes.append(f'{username}: social normalized')
        if hash_passwords and auth.needs_upgrade(acc.get('password')):
            # Never log the value, only that it moved (login_character.md 3.1).
            acc['password'] = auth.hash_password(acc.get('password') or '')
            changes.append(f'{username}: password stored as {auth.ALGORITHM}')
        chars = acc.get('characters')
        if not isinstance(chars, list):
            acc['characters'] = chars = []
            changes.append(f'{username}: characters = []')
        for char in chars:
            if not isinstance(char, dict):
                raise StoreError(f'account {username!r}: character record must be an object')
            changes.extend(f'{username}/{c}'
                           for c in migrate_character(char, acc.get('gender', 0), defaults))
            if client_build == BUILD_2009:
                changes.extend(f'{username}/{c}'
                               for c in migrate_character_2009(char, acc.get('gender', 0)))
    return changes


# ------------------------------------------------------------------- writing ---
def atomic_write(path, data, retries=5):
    """Write bytes to `path` so readers only ever see the old or the new complete file."""
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows: a reader (editor, antivirus, the backup copy) holds the target open.
                if attempt == retries - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------- store ---
class Store:
    def __init__(self, path, *, debounce_secs=2.0, autosave_secs=60.0,
                 start_map=101, start_x=700.0, start_y=812.0, backup=True,
                 hash_passwords=True, start_gold=DEFAULT_GOLD, start_victy=DEFAULT_VICTY,
                 client_build=None):
        self.path = os.path.abspath(path)
        # config CLIENT_BUILD (client-2009-login): '2009' adds the per-character gender and
        # look_ext fields (migration + new_character); None / '2008' is the 2008 schema.
        self.client_build = client_build
        self.debounce_secs = float(debounce_secs)
        self.autosave_secs = float(autosave_secs)
        self.start = (int(start_map), float(start_x), float(start_y))
        self.start_wallet = (int(start_gold), int(start_victy))
        self.backup = backup
        self.hash_passwords = bool(hash_passwords)
        self.lock = threading.RLock()            # db_lock (F4)
        self.accounts = {}
        self.name_index = {}                     # lower(name) -> username (unique names: lc-create)
        self.dirty = False
        self.saves = 0
        self.migration_changes = []
        self._scheduler = None
        self._pending = None
        self._autosave = None

    @classmethod
    def from_config(cls, cfg, path=None):
        return cls(path or cfg.accounts_path, debounce_secs=cfg.SAVE_DEBOUNCE_SECS,
                   autosave_secs=cfg.AUTOSAVE_SECS, start_map=cfg.START_MAP,
                   start_x=cfg.START_X, start_y=cfg.START_Y,
                   hash_passwords=cfg.HASH_PASSWORDS,
                   start_gold=cfg.START_GOLD, start_victy=cfg.START_VICTY,
                   client_build=cfg.get('CLIENT_BUILD'))

    @property
    def backup_path(self):
        return self.path + BACKUP_SUFFIX

    def backup_paths(self):
        suffixes = BACKUP_SUFFIXES + ((BACKUP_SUFFIX_2009,) if self.client_build == BUILD_2009 else ())
        return [self.path + suffix for suffix in suffixes]

    def record_defaults(self):
        """The configured values the migration writes into a record that lacks them."""
        start_map, start_x, start_y = self.start
        start_gold, start_victy = self.start_wallet
        return {'gold': start_gold, 'victy': start_victy,
                'map': start_map, 'x': start_x, 'y': start_y}

    # ------------------------------------------------------------ load / save ---
    def load(self):
        with self.lock:
            if not os.path.exists(self.path):
                self.accounts = self.default_accounts()
                self.migration_changes = []
                self._rebuild_name_index()
                self.save_now()
                log.info(f'[STORE] {self.path} not found: created test/test (uid 1) and admin/admin (uid 2)')
                return self
            with open(self.path, 'rb') as f:
                raw = f.read()
            try:
                accounts = json.loads(raw.decode('utf-8'))
            except ValueError as e:
                # Never overwrite a file we cannot read: the operator restores it (the atomic
                # write means this is not a torn save).
                raise StoreError(f'{self.path}: not valid JSON ({e}); not modified') from None
            changes = migrate_accounts(accounts, self.hash_passwords, self.record_defaults(),
                                       client_build=self.client_build)
            self.accounts = accounts
            self.migration_changes = changes
            self._rebuild_name_index()
            if changes:
                for path in self.backup_paths() if self.backup else []:
                    if not os.path.exists(path):
                        atomic_write(path, raw)
                        log.info(f'[STORE] one-time backup of the pre-migration file: {path}')
                for line in changes:
                    log.info(f'[STORE] migrate {line}')
                self.save_now()
            log.info(f'[STORE] loaded {len(accounts)} account(s) from {self.path}'
                     f'{f", {len(changes)} migration change(s)" if changes else ""}')
            return self

    def default_accounts(self):
        """A fresh accounts file: test/test with one Novice, admin/admin with none."""
        hero = self.new_character('TestHero', **DEFAULT_LOOK_SLOTS[0], stats=DEFAULT_STATS, now=0,
                                  gender=0)
        accounts = {
            'test': {'password': 'test', 'uid': 1, 'gender': 0, 'manner': 0,
                     'banned': False, 'deleted': False, 'characters': [hero]},
            'admin': {'password': 'admin', 'uid': 2, 'gender': 0, 'manner': 0,
                      'banned': False, 'deleted': False, 'characters': []},
        }
        for acc in accounts.values():
            social.ensure_account(acc)
        if self.hash_passwords:
            for acc in accounts.values():
                acc['password'] = auth.hash_password(acc['password'])
        return accounts

    def save_now(self):
        """Serialize and atomically replace the file now (create/delete/shutdown).

        The encoder never walks the live records: json.dumps is pure Python, so it gives
        the interpreter a chance to switch threads inside a dict it is iterating, and a
        handler thread adding a bag stack or repairing a field right then raised
        "dictionary changed size during iteration" (reproduced in seconds under load).
        It serializes a snapshot() instead, which every mutator is safe against."""
        with self.lock:
            data = json.dumps(snapshot(self.accounts), indent=2, ensure_ascii=False).encode('utf-8')
            atomic_write(self.path, data)
            self.dirty = False
            self.saves += 1

    def flush(self):
        """Save if anything changed since the last save. Returns True when it wrote."""
        with self.lock:
            if not self.dirty:
                return False
            try:
                self.save_now()
            except Exception:                            # noqa: BLE001 - stays dirty; next flush retries
                log.exception(f'[STORE] save of {self.path} failed; kept dirty for the next attempt')
                return False
            return True

    def mark_dirty(self, reason=''):
        with self.lock:
            self.dirty = True
            if self._scheduler is None or self._pending is not None:
                return
            self._pending = self._scheduler.call_later(self.debounce_secs, self._debounced_flush,
                                                       name='store-save')
        if reason:
            log.debug(f'[STORE] dirty: {reason}')

    def _debounced_flush(self):
        with self.lock:
            self._pending = None
            self.flush()

    def attach(self, scheduler, autosave=True):
        """Run debounced saves (and the autosave backstop) on `scheduler` (ticks.Scheduler)."""
        with self.lock:
            self.detach()
            self._scheduler = scheduler
            if autosave:
                self._autosave = scheduler.call_every(self.autosave_secs, self.flush, name='store-autosave')
            if self.dirty:
                self._pending = scheduler.call_later(self.debounce_secs, self._debounced_flush,
                                                     name='store-save')

    def detach(self):
        with self.lock:
            for t in (self._pending, self._autosave):
                if t is not None:
                    t.cancel()
            self._pending = self._autosave = None
            self._scheduler = None

    # --------------------------------------------------------------- accounts ---
    def account(self, username):
        with self.lock:
            return self.accounts.get(username) if isinstance(username, str) else None

    def uid_of(self, username):
        acc = self.account(username)
        return None if acc is None else acc['uid']

    def account_by_uid(self, uid):
        with self.lock:
            for username, acc in self.accounts.items():
                if acc.get('uid') == uid:
                    return username, acc
            return None

    def create_account(self, username, password, gender=0):
        """New account with the next free uid (F3: max + 1), password hashed when the
        config asks for it. Saved immediately. Who may register (AUTO_REGISTER) is the
        login handler's decision (lc-login-errors)."""
        with self.lock:
            if username in self.accounts:
                raise StoreError(f'account {username!r} exists')
            used = [a.get('uid') for a in self.accounts.values()]
            fixed = ids.FIXED_PLAYER_UIDS.get(username)
            uid = fixed if fixed is not None and fixed not in used else ids.next_player_uid(used)
            acc = {'password': auth.hash_password(password) if self.hash_passwords else password,
                   'uid': uid, 'gender': int(gender), 'manner': 0,
                   'banned': False, 'deleted': False, 'characters': []}
            social.ensure_account(acc)
            self.accounts[username] = acc
            self.save_now()
            return acc

    def verify_password(self, username, password):
        """True when `password` is this account's (auth.verify: hashed or a hand-edited
        plaintext record). An unknown account is False, and the caller must answer it with
        the same S2C 0x02 result 0x11 as a wrong password (F2 2d: no account enumeration)."""
        acc = self.account(username)
        return acc is not None and auth.verify(acc.get('password'), password)

    # ------------------------------------------------------------- characters ---
    def characters(self, username):
        acc = self.account(username)
        return [] if acc is None else acc['characters']

    def find_character(self, username, name):
        """The character named exactly `name` on this account (the client echoes the name it
        was sent), or None."""
        with self.lock:
            for char in self.characters(username):
                if char.get('name') == name:
                    return char
            return None

    def name_owner(self, name):
        """Username owning a character with this name, compared case-insensitively."""
        with self.lock:
            return self.name_index.get(str(name).lower())

    def character_by_name(self, name):
        """(username, account, character) for a character name anywhere in the store,
        matched case-insensitively (the GM types `/manner testhero -50` in any case), or
        None. chat_mail_gm F10.1 step 1: the offline half of "resolve the target"."""
        with self.lock:
            username = self.name_owner(name)
            if username is None:
                return None
            acc = self.accounts.get(username)
            key = str(name).lower()
            for char in (acc or {}).get('characters', []):
                if str(char.get('name', '')).lower() == key:
                    return username, acc, char
            return None

    # ------------------------------------------------------- GM and manner ---
    def manner(self, username):
        """The account's manner i32 (0 for an unknown account)."""
        acc = self.account(username)
        return 0 if acc is None else _as_int(acc.get('manner'), 0)

    def adjust_manner(self, username, delta):
        """Add `delta` to the account manner and persist it (debounced). Returns the new
        value, or None for an unknown account. chat_mail_gm F10.1 step 2: manner is per
        ACCOUNT (Q9/D1: S2C 0x02 carries it before character select), so every character
        of the account shares the penalty."""
        with self.lock:
            acc = self.accounts.get(username)
            if acc is None:
                return None
            value = max(MANNER_MIN, min(MANNER_MAX, _as_int(acc.get('manner'), 0) + int(delta)))
            acc['manner'] = value
        self.mark_dirty(f'manner {username}')
        return value

    def set_gm(self, name, level=GM_LEVEL_ON, hidden=None):
        """Set (or clear) the GM flag of the character called `name` and persist it at once.
        Returns (username, character) or None when no such character exists. The client only
        reads gm_level at spawn, so a change takes effect on the target's next S2C 0x07
        (relog or map change), which is what F11 `!gm` promises."""
        found = self.character_by_name(name)
        if found is None:
            return None
        username, _acc, char = found
        with self.lock:
            char['gm'] = int(level)
            if hidden is not None:
                char['gm_hidden'] = int(bool(hidden))
            self.save_now()
        return username, char

    def _rebuild_name_index(self):
        self.name_index = {}
        for username, acc in self.accounts.items():
            for char in acc.get('characters', []):
                key = str(char.get('name', '')).lower()
                if key in self.name_index and self.name_index[key] != username:
                    log.warning(f'[STORE] character name {char.get("name")!r} is used by '
                                f'{self.name_index[key]!r} and {username!r} (lc-create enforces uniqueness)')
                self.name_index.setdefault(key, username)

    def new_character(self, name, *, s10, s1, s6, s5, s9, stats, now=None, gender=None):
        """A lc-data-model character record for C2S 0x0E values: a Novice (class 0) at the
        configured start point (F4: map 101 (700,812) beside the Elder, not map 0 (100,100)). A 2009 store
        also writes the character's own `gender` (the C2S 0x0E bool) and `look_ext`."""
        start_map, start_x, start_y = self.start
        start_gold, start_victy = self.start_wallet
        if isinstance(stats, dict):
            stats = [stats['str'], stats['dex'], stats['int'], stats['spr']]
        s_str, s_dex, s_int, s_spr = (int(v) for v in stats)
        char = {
            'name': name,
            'created_at': int(time.time() if now is None else now),
            'class': 0, 'job2': 0, 'exp': 0,
            'look': compose_look(s10, s1, s6, s5, s9),
            'str': s_str, 'dex': s_dex, 'int': s_int, 'spr': s_spr,
            'fame': 0,
            'map': start_map, 'x': start_x, 'y': start_y,
            'hp': DEFAULT_HP, 'mp': DEFAULT_MP,
            'gold': start_gold, 'victy': start_victy,
            # Nobody is born a GM; the key is written so F10.0 step 1 is a 0 -> 1 edit.
            **dict(DEFAULT_GM),
        }
        # Empty bag, empty grid, empty quest log and deck, written by their owners' own
        # normalizers so a new record has exactly the shape a migrated one has.
        invmod.ensure(char)
        questmod.ensure(char)
        skillmod.ensure(char)
        buffmod.ensure(char)
        bankmod.ensure(char)
        privacy.ensure(char)
        social.ensure(char)
        stallmod.ensure(char)
        # cs-hp-mp-model: born at the client's own maxima (a default Novice is 140/87), not
        # the old flat 100/50 - which spawned every new character hurt. `hp`/`mp` stay the
        # CURRENT values; the maxima are never stored (hpmp.derive computes them). Imported
        # here because hpmp -> records -> store would be a cycle at module load.
        import hpmp
        char['hp'], char['mp'] = hpmp.new_character_vitals(char)
        if self.client_build == BUILD_2009:
            char['gender'] = 1 if gender else 0
            char['look_ext'] = [0] * LOOK_EXT_SLOTS
        return char

    # -------------------------------------------------------- world state ---
    def save_world_state(self, char, *, map_code=None, x=None, y=None, hp=None, mp=None,
                         buffs=None, reason='world state'):
        """Write a character's live world state back (world-persistence). Only the values
        the caller passes are written, so a map transfer can save map+position without
        touching HP.

        This is the gap live verification found: `_map_transfer` wrote x/y onto a COPY of
        the record and never wrote `map` at all, so a relog always reloaded the stored map
        (usually 101) at the stored point, and the HP you logged out with was replaced by a
        free full heal. Saves are debounced (F4), so the value is on disk within
        `debounce_secs`; disconnect marks dirty once more and the shutdown flush catches it."""
        if not isinstance(char, dict):
            return False
        changed = False
        with self.lock:
            for key, value in (('map', map_code), ('hp', hp), ('mp', mp)):
                if value is None:
                    continue
                value = _as_int(value)
                if char.get(key) != value:
                    char[key] = value
                    changed = True
            for key, value in (('x', x), ('y', y)):
                if value is None:
                    continue
                value = float(value)
                if char.get(key) != value:
                    char[key] = value
                    changed = True
            # cs-buffs: the persisted form (buffs.persist) - remaining ms per slot, so a
            # relog re-sends each buff with the time it had left.
            if buffs is not None:
                buffs = [dict(b) for b in buffs]
                if char.get('buffs') != buffs:
                    char['buffs'] = buffs
                    changed = True
        if changed:
            self.mark_dirty(f'{reason} {char.get("name")}')
        return changed

    def save_refuse(self, char, flags, reason='refuse flags'):
        """Write a character's five refuse flags (chat_mail_gm-privacy-flags) when they
        changed; debounced like every world-state write. True when something changed."""
        if not isinstance(char, dict):
            return False
        flags = privacy.normalize(flags)
        with self.lock:
            if char.get('refuse') == flags:
                return False
            char['refuse'] = flags
        self.mark_dirty(f'{reason} {char.get("name")}')
        return True

    def add_character(self, username, char):
        """Append (select-screen order) and save immediately. Validation (name rules,
        uniqueness, the 5-character cap, 0x1C results) is lc-create."""
        with self.lock:
            acc = self.accounts.get(username)
            if acc is None:
                raise StoreError(f'no account {username!r}')
            acc['characters'].append(char)
            self.name_index.setdefault(str(char.get('name', '')).lower(), username)
            self.save_now()
            return char

    def remove_character(self, username, name):
        """Remove by exact name keeping the order of the rest (the client shifts later
        select entities left, lc-delete F4 2d). Saves immediately. Returns True if removed."""
        with self.lock:
            chars = self.characters(username)
            for i, char in enumerate(chars):
                if char.get('name') == name:
                    del chars[i]
                    self._rebuild_name_index()
                    self.save_now()
                    return True
            return False
