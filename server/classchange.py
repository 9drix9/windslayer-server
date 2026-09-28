#!/usr/bin/env python3
"""
classchange.py - job-change items and the class model (lc-class-change, login_character.md
F8 / 3.5.5; quest_cards_misc-quest-rewards-skill-class)
=========================================================================================
The EN client changes a class only through a Type-4 "Job Change" item: S2C 0x18 {gold,
victy, item_id, 1} -> FUN_00440920 case 4 (needs the local player, scene+0x970) ->
FUN_004249E0 (the table below; 0 = refused, nothing happens) -> FUN_00440BC0 (base class:
FUN_0041ace0 + FUN_00427d40/FUN_00427f40 recompute the maxima WITHOUT a heal; every class:
HUD redraw and the popup "<class> is your new class."). The item is never bagged. S2C 0x27
applies a Type-4 quest reward through the same FUN_00440920, so a quest reward and a 0x18
grant obey exactly this table (live login_character#17: the first 0x18 180 popped "Warrior
is your new class.", a second one did nothing).

    import classchange as CC
    res = CC.apply(0, 0, 180)       # Change(ok=True, cls=1, tier=0, ...)
    res = CC.apply(1, 0, 3077)      # Paladin: tier 2
    CC.class_name(1, 2)             # 'Counter' - the client's tier-2 row is shifted by one

The character record keeps `class` (+0x110, 0 Novice .. 6 Priest) and `job2` (+0x111,
tier 0..2); records.py puts them in 0x02 class_id/job_branch (select screen), 0x07
job/job2 (HUD) and the 0x04/0x05 remote records.

FUN_004249E0 (decomp 0x4249E0, verified byte for byte against the switch):
- base items 180 / 203..207 need +0x110 == 0 and set ONLY +0x110 (the tier is not tested
  and not written);
- tier items 3076..3081 / 3083..3088 need +0x111 == 0 AND +0x110 == their base class and
  set ONLY +0x111;
- any other Type-4 id (3082 "====", and every id above 0xC10) hits the default case: it
  returns 1 with no change, and FUN_00440BC0 skips it too (no recompute, no popup). The
  server never grants those (there is nothing to apply).
"""
from collections import namedtuple

# item id -> (required +0x110, required +0x111 or None = not tested, field, new value)
# field 'class' writes +0x110, 'tier' writes +0x111 (FUN_004249E0).
JOB_ITEMS = {
    180: (0, None, 'class', 1),        # 0xB4 Class Change: Warrior
    203: (0, None, 'class', 2),        # 0xCB Job Change: Monk
    204: (0, None, 'class', 3),        # 0xCC Job Change: Archer
    205: (0, None, 'class', 6),        # 0xCD Job Change: Priest
    206: (0, None, 'class', 5),        # 0xCE Job Change: Mage
    207: (0, None, 'class', 4),        # 0xCF Job Change: Rogue
    3076: (1, 0, 'tier', 1),           # 0xC04 Berserker
    3077: (1, 0, 'tier', 2),           # 0xC05 Paladin
    3078: (2, 0, 'tier', 1),           # 0xC06 Fighter
    3079: (2, 0, 'tier', 2),           # 0xC07 Counter
    3080: (4, 0, 'tier', 1),           # 0xC08 Assassin
    3081: (4, 0, 'tier', 2),           # 0xC09 Trapper
    3083: (5, 0, 'tier', 1),           # 0xC0B Elementalist
    3084: (5, 0, 'tier', 2),           # 0xC0C Summoner
    3085: (6, 0, 'tier', 1),           # 0xC0D Bishop
    3086: (6, 0, 'tier', 2),           # 0xC0E Dark Priest (shares the tier-2 write, LAB_00424ae3)
    3087: (3, 0, 'tier', 1),           # 0xC0F Sniper
    3088: (3, 0, 'tier', 2),           # 0xC10 Beast Master
}
BASE_ITEMS = frozenset(i for i, row in JOB_ITEMS.items() if row[2] == 'class')

CLASS_COUNT = 7                        # FUN_00440BC0 shows the popup only for +0x110 < 7
TIER_COUNT = 3                         # ... and +0x111 < 3 (the quest filter has no bound)
# The 21 x 15-byte name table at VA 0x70C588, read from WindSlayer.exe, indexed
# (tier * 7 + class). The tier-2 row starts one entry early: a tier-2 Warrior shows
# "Counter", a tier-2 Priest a blank (login_character 3.5.5; lc-classname-patch is the
# optional exe fix). Logs and chat use class_name() so they say what the player sees.
CLASS_NAMES = ('Novice', 'Warrior', 'Monk', 'Archer', 'Rogue', 'Mage', 'Priest',
               '', 'Berserker', 'Fighter', 'Sniper', 'Assassin', 'Elementalist', 'Bishop',
               'Paladin', 'Counter', 'BeastMaster', 'Trapper', 'Summoner', 'DarkPriest', '')
NOVICE = (0, 0)

Change = namedtuple('Change', 'ok item cls tier old why')


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def of(char):
    """(class, tier) of a character record (`class`, `job2`)."""
    return _int((char or {}).get('class')) & 0xFF, _int((char or {}).get('job2')) & 0xFF


def is_job_item(item_id):
    """True for the 18 ids FUN_004249E0 can apply."""
    return _int(item_id) in JOB_ITEMS


def class_name(cls, tier=0):
    """What the client prints for (class, tier): the 0x70C588 entry, shift included.
    Out of range (class >= 7 or tier >= 3) is '' - the client shows nothing either."""
    cls, tier = _int(cls), _int(tier)
    if not (0 <= cls < CLASS_COUNT and 0 <= tier < TIER_COUNT):
        return ''
    return CLASS_NAMES[tier * CLASS_COUNT + cls]


def popup_text(cls, tier=0):
    """FUN_00440BC0's "<class> is your new class." (None when it shows no popup)."""
    cls, tier = _int(cls), _int(tier)
    if not (0 <= cls < CLASS_COUNT and 0 <= tier < TIER_COUNT):
        return None
    return f'{class_name(cls, tier)} is your new class.'


def refusal(cls, tier, item_id):
    """Why FUN_004249E0 would refuse `item_id` for (class, tier), or None."""
    item_id = _int(item_id)
    row = JOB_ITEMS.get(item_id)
    if row is None:
        return f'item {item_id} is not a job-change item (FUN_004249E0 applies nothing)'
    need_cls, need_tier, _field, _value = row
    if _int(cls) != need_cls:
        return f'needs class {need_cls} ({class_name(need_cls)}), is class {_int(cls)}'
    if need_tier is not None and _int(tier) != need_tier:
        return f'needs tier {need_tier}, is tier {_int(tier)} (a class advances only once)'
    return None


def apply(cls, tier, item_id):
    """Change(ok, item, cls, tier, old=(cls, tier), why): the (class, tier) FUN_004249E0
    leaves behind. A refusal returns the old values unchanged, as the client does."""
    cls, tier, item_id = _int(cls), _int(tier), _int(item_id)
    why = refusal(cls, tier, item_id)
    if why is not None:
        return Change(False, item_id, cls, tier, (cls, tier), why)
    _need_cls, _need_tier, field, value = JOB_ITEMS[item_id]
    new_cls, new_tier = (value, tier) if field == 'class' else (cls, value)
    return Change(True, item_id, new_cls, new_tier, (cls, tier), None)


def prerequisite(item_id):
    """(class, tier) that makes `item_id` applicable: Novice for a base item, the base
    class at tier 0 for a tier item. The dev `!job` uses it to jump a character there first
    (S2C 0x58 to self, login_character F8 step 3)."""
    row = JOB_ITEMS.get(_int(item_id))
    if row is None:
        return None
    return (row[0], 0)


def recomputes_maxima(item_id):
    """FUN_00440BC0 recomputes max HP/MP only for the six base items (180, 0xCB..0xCF):
    the class indexes the HP/MP tables, the tier does not (hpmp.py)."""
    return _int(item_id) in BASE_ITEMS
