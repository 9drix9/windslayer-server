#!/usr/bin/env python3
"""
ground.py - items lying on a map (item_inventory-ground-loot-pickup, -drop-bag-item,
-drop-equipped; item_inventory.md 1.5, F6-F9, 3.4)
====================================================================================
The world-side registry of ground items and the wire records of S2C 0x11 / 0x12 / 0x13.
Pure model: no sockets, no store. GameServer owns one GroundRegistry (self.ground) and
does the sends, the bag moves and the despawn timers.

    reg = GroundRegistry(max_per_map=100)
    item, evicted = reg.add(102, 5, 1, None, 1200, 714, owner_uid=1, source_uid=0xF0003)
    P.send(..., '0x12', drop_fields([item], [client_clock]))          # the killer / dropper
    taken, why = reg.take(102, item.ground_id, picker_uid=1)          # C2S 0x1F
    P.send(..., '0x13', picked_fields(taken, picker_uid=1))           # everybody on the map

Wire layout (re-derived for P4 stage 2 from both specs AND both disassemblies; the P0
builder that wrote item,x,y,count,0,u32 uid,u32 x,u32 0 is what the old "misaligned drop
position" / "You don't have ownership" live notes were):

    S2C 0x11 / 0x12   u8 entry_count, then per entry
        u16 item_id, u16 quantity, u16 x, u16 y, u16 ground_id,
        u32 drop_time, u32 owner_uid, u32 source_uid,
        u8 opt_count, opt_count x u16 opt_word, u16 opt_tail,
        [0x11 only: u8 state]

  2008 handler 0x451516 reads, in this order (GetDataFromPacket targets, esp at the call
  minus the 3 pushes): u16 esp+0xCC, u16 +0xC4, u16 +0x164, u16 +0x19C, u16 +0xE0,
  u32 +0xD8, u32 +0xFC, u32 +0xB4, u8 +0x5F, words at +0x218, u16 +0x222, (0x11) u8 +0x1C0.
  The pushes before CALL FUN_00423a10 at 0x4518AE put them into (scene, item_id=+0xCC,
  opts=+0x218, x=+0x164, y=+0x19C, ground_id=+0xE0, owner=+0xFC, source=+0xB4,
  drop_time=+0xD8, quantity=+0xC4, state), and FUN_00423a10 stores rec+0x0E x, +0x10 y,
  +0x12 ground_id, +0x14 quantity, +0x16 state, +0x1C owner, +0x20 source, +0x24 drop_time.
  2009 handler 0x4561CE (FUN_00451960) reads the same sequence (u16 +0xF8, +0xE0, +0x1E8,
  +0x190, +0xF0, ulong +0xD4, uint +0x120, uint +0xB4, u8 +0x59, words +0x29C, tail +0x2A6,
  (0x11) u8 +0x284) and pushes the same argument order into FUN_00424cb0 at 0x456591 - the
  wire format is identical, only the client offsets moved (spec_2009 diff_vs_2008).
  Live: the 37 B 0x11 of item_inventory#06 and the 36 B 0x12 of #09 decode to exactly
  these fields, and #09's memory dump showed x/y/gid/qty/owner/src at the offsets above.

  0x12 positions the item at the entity whose uid == source_uid (2008 +0x11F8/+0x1288,
  2009 +0x1298/+0x1328), else at owner_uid's entity, else at the wire x/y; state is forced
  to 1 (pop-in 800 ms, then a fall). 0x11 always uses the wire x/y and the wire state.
  The option buffer is zeroed ONCE per packet, so every entry carries all 5 words
  (inventory.ALWAYS_FIVE_WORDS) - never a shorter list that inherits the previous entry's.

    S2C 0x13   u16 ground_id, u16 quantity, u32 picker_uid          (8 B)
  2008 FUN_00440e50 / 2009 FUN_00441630: the first record with rec+0x12 == ground_id is
  marked picked (state 2, flies to the picker, freed 1000 ms later); only when picker_uid
  is the receiver's own uid is the item added to its bag - by the record's category, with
  `quantity` for stacks and the record's 6 words for equipment - with "you've received
  %s. (Count:%u)". picker_uid 0 is the despawn: no bag change, no notice, no crash
  (live-verified on 2008, results_destructive item_inventory#08 / T-S13b; 2009 by the same
  code: the draw FUN_0043d0f0 skips a state-2 record whose owner is 0, the tick
  FUN_0042e790 frees it after 1000 ms).

    C2S 0x1F   u16 ground_item_uid                                  (2 B)
  2008 0x43D80F (key W, FUN_0043d5c0), 2009 0x43DA4E (FUN_0043d870) and the 2009 pet
  auto-loot 0x42EA76. The client sends it only for an item within 25 px whose owner is 0 /
  itself / 15 s past drop_time and that fits its bag, but it does not mark the item as
  requested: a held key (and the pet, every 30 ms tick) repeats it, so a pickup must be
  idempotent - the registry forgets the item at the first grant.

Ground ids are u16 and PER MAP (ids.GROUND_ITEM): the client matches 0x13 on the first
record carrying the id and never de-duplicates, and a picked record lingers for 1 s. So
ids are allocated round-robin (a freed id is not handed out again until the counter wraps
at 65535), never 0, and never one still lying on that map.

Clock (item_inventory.md 1.6): drop_time is in the RECEIVER's scene clock (2008
scene+0xF1C, 2009 +0xF34), set by S2C 0x03 / 0x08 and advanced 30 per logic frame. Loot
protection lapses client-side at drop_time + 15000 < clock; the server checks the same
15 s on its own monotonic clock, so neither side depends on the other's estimate.
"""
import threading
import time
from dataclasses import dataclass, field

import ids
import inventory as INV

# FUN_0043d5c0 / FUN_0043d870: an owned item is protected until drop_time + 15000 ms.
OWNER_PROTECT_MS = 15000
OWNER_PROTECT_SECS = OWNER_PROTECT_MS / 1000.0
# Client pickup reach, dx^2 + dy^2 < 625 (25 px). Client-side only: an ordinary C2S 0x0D
# carries no position, so the server cannot check it (item_inventory.md F7 step 2).
PICKUP_RANGE_SQ = 625
# Ground record state byte rec+0x16 (0x11 sends it; 0x12 forces POP_IN).
STATE_RESTING, STATE_POP_IN, STATE_PICKED, STATE_FALLING = 0, 1, 2, 3
# One 0x11 / 0x12 entry with its 5 option words: 2*5 + 4*3 + 1 + 10 + 2 = 35 B (+1 state
# byte for 0x11). A server payload is at most 2038 B (MAX_PKT 0x7FF - 8 B header - opcode)
# and entry_count is a u8.
ENTRY_BYTES = {'0x11': 36, '0x12': 35}
MAX_PAYLOAD = 0x7FF - 8 - 1
U16_MAX, U32_MAX = 0xFFFF, 0xFFFFFFFF


def _u16(value):
    return max(0, min(U16_MAX, int(round(float(value or 0)))))


@dataclass
class GroundItem:
    ground_id: int
    map_code: int
    item_id: int
    qty: int
    words: list                          # the 6-word option block (inventory.OPTION_WORDS)
    x: float
    y: float
    owner_uid: int = 0                   # loot rights; 0 = anyone
    source_uid: int = 0                  # the dropping entity (0x12 positions on it)
    dropped_at: float = 0.0              # server monotonic seconds
    what: str = ''
    timer: object = field(default=None, repr=False, compare=False)   # ticks.Timer (despawn)

    def age_ms(self, now=None):
        now = time.monotonic() if now is None else now
        return max(0, int((now - self.dropped_at) * 1000))


def ownership_refusal(item, picker_uid, now=None):
    """None when `picker_uid` may take `item` (FUN_0043d5c0's rule: owner 0, the owner
    himself, or the 15 s protection is over), else why not."""
    if not item.owner_uid or int(item.owner_uid) == int(picker_uid or 0):
        return None
    now = time.monotonic() if now is None else now
    if now - item.dropped_at >= OWNER_PROTECT_SECS:
        return None
    return (f'owned by uid {item.owner_uid} for another '
            f'{OWNER_PROTECT_SECS - (now - item.dropped_at):.1f} s')


class GroundRegistry:
    """Ground items per map. Thread-safe with its own leaf lock: nothing is sent and no
    other lock is taken while it is held, so handlers (under a session's combat lock) and
    the tick thread (under the world lock) can both call it."""

    def __init__(self, max_per_map=100, clock=time.monotonic):
        self.max_per_map = max(1, int(max_per_map))
        self.clock = clock
        self._lock = threading.Lock()
        self._maps = {}          # map_code -> {ground_id: GroundItem}, in drop order
        self._next = {}          # map_code -> the next ground id to try

    # ---------------------------------------------------------------- reads ---
    def get(self, map_code, ground_id):
        with self._lock:
            return self._maps.get(int(map_code), {}).get(int(ground_id))

    def items(self, map_code):
        """The items lying on a map, oldest first."""
        with self._lock:
            return list(self._maps.get(int(map_code), {}).values())

    def count(self, map_code=None):
        with self._lock:
            if map_code is not None:
                return len(self._maps.get(int(map_code), {}))
            return sum(len(m) for m in self._maps.values())

    # ------------------------------------------------------------- mutation ---
    def _allocate(self, map_code, live):
        """Round-robin u16 id: never 0, never an id still on the map, and a freed id is
        not reused before the counter wraps (a picked record lives on for 1 s client-side
        and 0x13 matches the FIRST record with the id)."""
        start = self._next.get(map_code, ids.GROUND_ITEM.lo)
        span = ids.GROUND_ITEM.hi - ids.GROUND_ITEM.lo + 1
        for step in range(span):
            gid = ids.GROUND_ITEM.lo + (start - ids.GROUND_ITEM.lo + step) % span
            if gid not in live:
                self._next[map_code] = ids.GROUND_ITEM.lo + (gid - ids.GROUND_ITEM.lo + 1) % span
                return gid
        raise ids.IdSpaceExhausted(f'{ids.GROUND_ITEM.name}: map {map_code} is full')

    def add(self, map_code, item_id, qty, words, x, y, *, owner_uid=0, source_uid=0, what='',
            now=None):
        """Register one ground item. Returns (item, evicted): `evicted` are the oldest items
        pushed out by the per-map cap - the caller despawns them on the clients (0x13 with
        picker 0) and cancels their timers."""
        map_code = int(map_code)
        now = self.clock() if now is None else now
        with self._lock:
            live = self._maps.setdefault(map_code, {})
            evicted = []
            while len(live) >= self.max_per_map:
                oldest = next(iter(live))
                evicted.append(live.pop(oldest))
            gid = self._allocate(map_code, live)
            item = GroundItem(ground_id=gid, map_code=map_code, item_id=int(item_id), qty=max(1, int(qty)),
                              words=INV.pack_words(words), x=float(x or 0), y=float(y or 0),
                              owner_uid=int(owner_uid or 0) & U32_MAX, source_uid=int(source_uid or 0) & U32_MAX,
                              dropped_at=now, what=what)
            live[gid] = item
        return item, evicted

    def take(self, map_code, ground_id, picker_uid, now=None):
        """C2S 0x1F: remove and return the item when `picker_uid` may have it. (item, None)
        on success, (None, why) otherwise - an unknown id is the normal answer to a repeated
        request (the item already went to the first one)."""
        now = self.clock() if now is None else now
        with self._lock:
            live = self._maps.get(int(map_code), {})
            item = live.get(int(ground_id))
            if item is None:
                return None, f'no ground item {ground_id} on map {map_code} (already picked or gone)'
            why = ownership_refusal(item, picker_uid, now)
            if why is not None:
                return None, why
            del live[item.ground_id]
            return item, None

    def restore(self, item):
        """Put back an item `take` returned when the grant could not complete."""
        with self._lock:
            self._maps.setdefault(item.map_code, {})[item.ground_id] = item

    def remove(self, item):
        """Forget this very item (despawn). False when it is no longer there (picked, or
        already evicted) - the despawn then sends nothing."""
        with self._lock:
            live = self._maps.get(item.map_code, {})
            if live.get(item.ground_id) is not item:
                return False
            del live[item.ground_id]
            return True

    def clear(self):
        with self._lock:
            items = [i for m in self._maps.values() for i in m.values()]
            self._maps.clear()
            return items


# ------------------------------------------------------------- wire records ---
def client_drop_time(receiver_clock_ms, age_ms=0):
    """drop_time in the receiver's scene clock: its clock minus the item's age. Floored at
    1, not 0: the 2009 pet auto-loot (FUN_0042e790) takes an OWNED item only when
    rec+0x24 != 0, and a clamped time only shortens the 15 s protection."""
    return max(1, int(receiver_clock_ms) - max(0, int(age_ms))) & U32_MAX


def entry_fields(item, drop_time, state=None):
    """One 0x11 (with `state`) / 0x12 (state None) entry for `item`."""
    rec = {'item_id': int(item.item_id) & U16_MAX, 'quantity': max(1, min(U16_MAX, int(item.qty))),
           'x': _u16(item.x), 'y': _u16(item.y), 'ground_id': int(item.ground_id) & U16_MAX,
           'drop_time': int(drop_time) & U32_MAX, 'owner_uid': int(item.owner_uid) & U32_MAX,
           'source_uid': int(item.source_uid) & U32_MAX}
    rec.update(INV.block_fields('0x11' if state is not None else '0x12', item.words))
    if state is not None:
        rec['state'] = int(state) & 0xFF
    return rec


def _batches(key, rows):
    per = min(0xFF, (MAX_PAYLOAD - 1) // ENTRY_BYTES[key])
    return [rows[i:i + per] for i in range(0, len(rows), per)] or []


def drop_fields(items, drop_times):
    """S2C 0x12 GroundItemDrop bodies (a list: one per packet) for items that just fell."""
    rows = [entry_fields(i, t) for i, t in zip(items, drop_times)]
    return [{'entry_count': len(b), 'repeat[entry_count]': b} for b in _batches('0x12', rows)]


def list_fields(items, drop_times, state=STATE_RESTING):
    """S2C 0x11 GroundItemSpawn bodies (one per packet): the items already on a map at map
    entry (state 0, spec correction C19: every map load clears the client list) or a drop
    shown at its wire position (state 1)."""
    rows = [entry_fields(i, t, state) for i, t in zip(items, drop_times)]
    return [{'entry_count': len(b), 'repeat[entry_count]': b} for b in _batches('0x11', rows)]


def picked_fields(item, picker_uid):
    """S2C 0x13 for a pickup: `quantity` is the stack count added (the item id and the
    option words come from the client's own ground record); equipment is always 1."""
    return {'ground_id': int(item.ground_id) & U16_MAX, 'quantity': max(1, min(U16_MAX, int(item.qty))),
            'picker_uid': int(picker_uid) & U32_MAX}


def despawn_fields(item):
    """S2C 0x13 {ground_id, 0, picker 0}: the despawn (T-S13b)."""
    return {'ground_id': int(item.ground_id) & U16_MAX, 'quantity': 0, 'picker_uid': 0}
