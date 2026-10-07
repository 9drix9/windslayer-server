#!/usr/bin/env python3
"""
ids.py - the single id-space table and its allocators (roadmap 1.4 F3, decisions D1/D2)
======================================================================================
Every number the server puts in a uid/id field comes from one of these spaces, so a
monster can never collide with a player (the old uid-0x1000 test mob, D2) and the server
never allocates a number the client reserves for itself.

| Space              | Range                              | Owner                                   |
|--------------------|------------------------------------|-----------------------------------------|
| player uid         | 1 .. 0x000EFFFF (one per ACCOUNT)  | store.py (lc-data-model, lc-uid-online) |
| monster / NPC uid  | 0x000F0000 .. 0x001FFFFF           | world-shared-monsters (server-wide)     |
| select-screen uid  | 30,000,000 + slot                  | client only (S2C 0x02 / 0x1C entities)  |
| map-file NPC tiles | 33,000,000 +                       | client only; never allocate (2009: also |
|  and 2009 pets     |                                    | every pet sprite, gs+0x3FC, pet.md H6)  |
| ground item id     | u16 per map, 1 .. 0xFFFF           | item_inventory-ground-loot-pickup       |
| room number        | 1 .. 128, never 0x81               | pvp-room-model (arena/play/battle)      |
| messenger room     | u32 counter, never 0               | social_friend-chat-room                 |
| cash item serial   | global, >= 0x1000                  | premium_cash-wallet-model               |
| character id (cid) | 1 .. 0x7FFFFFFF, never reused      | store (ROADMAP_2009_ADDENDUM C4 / X14)  |

The player uid is per account, not per character (D1): the client writes scene+0x220
from the S2C 0x02 account_id before character select, and every later local-player
record (0x07 registration gate 0x4221A2, 0x1D, 0x22, ...) must carry that value.

The character id is the server's own stable key of one CHARACTER (it never goes on the
wire): a rename changes the name, never the cid, so guild membership (P14) and blacklist
entries (P12) keyed by it follow a renamed character (ROADMAP_2009_ADDENDUM X14 / C4).
"""
import threading
from dataclasses import dataclass


class IdSpaceExhausted(RuntimeError):
    pass


@dataclass(frozen=True)
class IdSpace:
    name: str
    lo: int
    hi: int                     # inclusive
    owner: str
    server_allocates: bool = True

    def __contains__(self, value):
        try:
            return self.lo <= int(value) <= self.hi
        except (TypeError, ValueError):
            return False


PLAYER = IdSpace('player uid', 1, 0x000EFFFF, 'store (lc-data-model / lc-uid-online)')
MONSTER = IdSpace('monster/NPC uid', 0x000F0000, 0x001FFFFF, 'world-shared-monsters')
SELECT_SCREEN = IdSpace('select-screen entity uid', 30_000_000, 30_000_004, 'client (0x02/0x1C)',
                        server_allocates=False)
# The 2009 pet sprites take their uid from the same client counter (gs+0x3FC, seeded to 33,000,000
# by FUN_00445970 on every 0x03 / 0x08 map load; FUN_00447f40 :95/:162): a server uid must stay
# below it (pet.md 7 H6) - every server space above ends far lower (test_pets.IdSpaces).
MAP_NPC = IdSpace('map-file NPC tile / 2009 pet sprite uid', 33_000_000, 0xFFFFFFFF, 'client (map files, pets)',
                  server_allocates=False)
GROUND_ITEM = IdSpace('ground item id (per map)', 1, 0xFFFF, 'item_inventory-ground-loot-pickup')
ROOM = IdSpace('room number', 1, 128, 'pvp-room-model')
MESSENGER_ROOM = IdSpace('messenger chat room', 1, 0xFFFFFFFF, 'social_friend-chat-room')
CASH_SERIAL = IdSpace('cash item serial', 0x1000, 0xFFFFFFFF, 'premium_cash-wallet-model')
CHARACTER = IdSpace('character id', 1, 0x7FFFFFFF, 'store (ROADMAP_2009_ADDENDUM C4 / X14)')

SPACES = (PLAYER, MONSTER, SELECT_SCREEN, MAP_NPC, GROUND_ITEM, ROOM, MESSENGER_ROOM, CASH_SERIAL, CHARACTER)

# Room number 0x81 marks "in the cash shop" in the 0x07 room_id field (S2-45), so no real
# room may use it. It lies just past 1..128 today; the skip keeps it out if the range grows.
ROOM_SKIP = frozenset({0x81})

# Accounts whose uid is fixed (F3): wsdev/wsview in-world detection and the live test plan
# assume test = 1 and the second client's admin = 2. Reserved even while the account is absent.
FIXED_PLAYER_UIDS = {'test': 1, 'admin': 2}


def is_player_uid(uid):
    return uid in PLAYER


def is_monster_uid(uid):
    return uid in MONSTER


def space_of(value):
    """The server-allocated space a uid-field value belongs to (player / monster), or None.
    Ground ids, rooms and serials share numbers with players; they live in other fields."""
    for space in (PLAYER, MONSTER, SELECT_SCREEN, MAP_NPC):
        if value in space:
            return space
    return None


def next_player_uid(used):
    """New account uid = max(used and the fixed uids) + 1 (F3 "new = max+1"). If that runs
    past the space, the lowest free uid is reused instead."""
    taken = {int(u) for u in used if u in PLAYER} | set(FIXED_PLAYER_UIDS.values())
    candidate = max(taken) + 1
    if candidate in PLAYER:
        return candidate
    return lowest_free(PLAYER, taken)


def lowest_free(space, in_use, skip=()):
    """Lowest value of `space` not in `in_use` or `skip` (room numbers, per-map ground ids)."""
    in_use = set(in_use) | set(skip)
    for value in range(space.lo, space.hi + 1):
        if value not in in_use:
            return value
    raise IdSpaceExhausted(f'{space.name}: all of {space.lo}..{space.hi} in use')


def next_room_number(in_use):
    return lowest_free(ROOM, in_use, ROOM_SKIP)


class Counter:
    """Thread-safe wrapping counter over a space (messenger rooms: never 0; cash serials:
    >= 0x1000). `in_use` (optional callable -> container) skips numbers still held after a
    wrap."""

    def __init__(self, space, start=None, in_use=None):
        if not space.server_allocates:
            raise ValueError(f'{space.name} is client-owned; the server never allocates it')
        self.space = space
        self._next = space.lo if start is None else int(start)
        if self._next not in space:
            raise ValueError(f'start {self._next} outside {space.name}')
        self._in_use = in_use
        self._lock = threading.Lock()

    def next(self):
        with self._lock:
            size = self.space.hi - self.space.lo + 1
            held = self._in_use() if self._in_use else ()
            for _ in range(min(size, len(held) + 1)):
                value = self._next
                self._next = value + 1 if value < self.space.hi else self.space.lo
                if value not in held:
                    return value
            raise IdSpaceExhausted(f'{self.space.name}: no free value')
