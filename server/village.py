#!/usr/bin/env python3
"""
village.py - the Garan Maria village transfer rules (world-village-transfer,
world_movement_npc.md 1.6 / F5; C2S 0x5D -> S2C 0x81 + MapTransfer)
===============================================================================
Talking to Garan Maria (NpcId 117, hni UI 600) opens window 600. FUN_0043dc40 computes the
fee for the picked destination with FUN_00422050 (below), takes 10 % off when the manner
at scene+0xEE0 is >= 500, shows it in confirm window 0x237, checks `gold >= fee` itself
("You are short of gold.", no packet) and sends C2S 0x5D {u8 village_index, u16 fee}.
The server answers S2C 0x81 {1, u64 gold} (absolute; the label moves, coin sound 0x29,
nothing else - live world_movement_npc#31) and then moves the player itself with the F6
MapTransfer; any other result shows "Village transfer failed. Please try again in a few
minutes." and reads nothing more (#32).

    import village as V
    V.fee(501, 2, manner=0)       # 2840  (Amakusa -> Popola)
    V.fee(501, 2, manner=500)     # 2556
    V.town_map(2)                 # 201

Tables read from WindSlayer.exe (u16 arrays; re-read by test_village.py when the exe is
present):
    0x70D07C fee by distance   0, 1000, 1940, 2840, 3700, 4520, 5300, 6040, 6740, 7400, 8020, 8600, 9140
    0x70D098 travel order      0, 1, 2, 3, 4, 5, 5, 6, 7, 8, 9
    0x70D04C village -> group  0, 1, 2, 4, 7, 5, 8, 6, 9, 10, 11   (map_code // 100)
    0x70B8B4 names             NAMES below

FUN_00422050(dest) (asm 0x422050-0x422111), the map code being the client's own
[scene+0xF88]+0x1B8:
    g = u16(map_code // 100); GROUP[dest] == g -> 0 ("You can't move to where you already are.")
    cur = the first i in 1..10 with GROUP[i] == g; none -> 0 (not a village group)
    d = |ORDER[cur] - ORDER[dest]|; d == 0 (Amakusa <-> Mining Settlement) -> FEE[1], no bonus
    (cur == 5 and dest > 5) or (dest == 5 and cur > 5) -> d += 1
    FEE[d]
So the fee depends only on the map GROUP: a field map of a village group pays the same
(102 -> Popola = 1000). The discount is FMUL 0.9 under a chop-mode FISTP (0x43DCED..
0x43DD15): trunc(fee * 0.9), manner compared signed (JL 0x1F4).
"""

FEES = (0, 1000, 1940, 2840, 3700, 4520, 5300, 6040, 6740, 7400, 8020, 8600, 9140)   # 0x70D07C
ORDER = (0, 1, 2, 3, 4, 5, 5, 6, 7, 8, 9)                                             # 0x70D098
GROUP = (0, 1, 2, 4, 7, 5, 8, 6, 9, 10, 11)                                           # 0x70D04C
NAMES = ('Nearest Village', 'The Beginning of the Adventure', 'Popola Village', 'Ozzi Village',
         'Balderan', 'Amakusa', 'Mining Settlement', 'Atajokuna', 'Serien, the City of Water',
         'Serien Underworld', 'Underwater City RA')                                    # 0x70B8B4
VILLAGE_MIN, VILLAGE_MAX = 1, 10         # window 600 controls 3..12; > 10 refused client-side
MANNER_DISCOUNT_MIN = 500                # scene+0xEE0 >= 500 -> 90 %
DISCOUNT = 0.9                           # double 0x6F8AF8
CROSSING_VILLAGE = 5                     # Amakusa: crossing it costs one more step
GARAN_MARIA = 117                        # hni NpcId of the transfer NPC (UI 600)


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def town_map(index):
    """The town map of village `index` (1..10): GROUP[index] * 100 + 1 - 101, 201, 401,
    701, 501, 801, 601, 901, 1001, 1101 (world doc 1.6 TOWN)."""
    index = _int(index)
    if not VILLAGE_MIN <= index <= VILLAGE_MAX:
        return None
    return GROUP[index] * 100 + 1


def village_of(map_code):
    """The village index whose group the map belongs to (FUN_00422050's `cur`), or None."""
    g = (_int(map_code) // 100) & 0xFFFF
    for i in range(VILLAGE_MIN, VILLAGE_MAX + 1):
        if GROUP[i] == g:
            return i
    return None


def base_fee(map_code, dest):
    """FUN_00422050: the undiscounted fee from `map_code` to village `dest`, 0 when the
    client would not open the dialog (already there, or not a village group)."""
    dest = _int(dest)
    if not 0 <= dest <= VILLAGE_MAX:
        return 0
    g = (_int(map_code) // 100) & 0xFFFF
    if GROUP[dest] == g:
        return 0
    cur = village_of(map_code)
    if cur is None:
        return 0
    d = abs(ORDER[cur] - ORDER[dest])
    if d == 0:
        return FEES[1]
    if (cur == CROSSING_VILLAGE and dest > CROSSING_VILLAGE) or \
            (dest == CROSSING_VILLAGE and cur > CROSSING_VILLAGE):
        d += 1
    return FEES[d]


def fee(map_code, dest, manner=0):
    """The fee FUN_0043dc40 shows and sends: base_fee, times 0.9 (truncated to u16) when
    the manner is >= 500. 0 = no transfer."""
    value = base_fee(map_code, dest)
    if value and _int(manner) >= MANNER_DISCOUNT_MIN:
        value = int(value * DISCOUNT) & 0xFFFF
    return value


def name(index):
    index = _int(index)
    return NAMES[index] if 0 <= index < len(NAMES) else f'village {index}'


def arrival(index, *, overrides=None, fallback=None, map_data=None):
    """(map, x, y, how) where a transfer to village `index` lands, or None for a bad index.

    Arrival points are not in the client (world doc 1.6 / T-5D-3), so:
    1. config VILLAGE_ARRIVALS {"<town map>": [x, y]} wins (a live fix needs no code);
    2. else next to Garan Maria in the town's own EN map file: the middle of the floor line
       her tile stands on, lifted by en_maps.ARRIVAL_ABOVE_LINE like a portal arrival, so
       the body drops onto it (she is on 501/601/701/801/901/1001/1101);
    3. else `fallback(town)` -> (x, y) - the server passes the revive-town point
       (combat.revive_point: START for 101, the first incoming portal for 201/401).
    map_data(town) -> en_maps.MapData or None (tests pass a stub)."""
    town = town_map(index)
    if town is None:
        return None
    for key in (str(town), town):
        row = (overrides or {}).get(key)
        if row is not None and len(row) >= 2:
            return town, float(row[0]), float(row[1]), 'config VILLAGE_ARRIVALS'
    if map_data is None:
        import en_maps                          # late: reads the client's map files
        map_data = en_maps.load_map
    try:
        data = map_data(town)
    except Exception:                           # an unreadable map file falls through to 3
        data = None
    if data is not None:
        from en_maps import ARRIVAL_ABOVE_LINE
        for tile in data.npc_tiles():
            if tile.npc_id != GARAN_MARIA:
                continue
            ln = data.floor_line(tile)
            if ln is not None:
                return (town, (ln.x1 + ln.x2) / 2.0, float(ln.y1) - ARRIVAL_ABOVE_LINE,
                        f'next to Garan Maria (tile {tile.x},{tile.y})')
    if fallback is not None:
        point = fallback(town)
        if point is not None:
            return town, float(point[0]), float(point[1]), 'revive-town point (no Garan Maria)'
    return None
