#!/usr/bin/env python3
"""
crafting.py - Concoction / Mineral Refining, Reinforcement and Gathering (P4 stage 3:
item_inventory-crafting, item_inventory-reinforcement, item_inventory-gathering)
=========================================================================================
item_inventory.md F11 / F12 / F13. All three are CLIENT-TIMED: the client runs a 5 s
progress bar (window 0x293 craft / reinforce, 0x295 gather) and sends only the completion
(C2S 0x67 / 0x68 / 0x69), with a busy flag set ([ctx+0x2C] craft and reinforce, [ctx+0x28]
gather) that only the result packet (S2C 0x8D / 0x8E / 0x8F) clears. So every completion
gets exactly ONE result, and the result handler then applies the inventory change to the
client's bag BY ITSELF, from its own item table:

    S2C 0x8D {result, product}     1: remove every recipe material, add 1 product
                                   0 / 0x11: remove the materials, no product
                                   0x0F / 0x10 / anything else: no change
    S2C 0x8E {result, equip, stone [, 6 old words, new option]}
                                   1: write the new option into the first zero word of the
                                   bag instance whose id AND 12 bytes match, remove 1 stone
                                   anything else: no change
    S2C 0x8F {result, tool [, reward]}
                                   ANY result (tool id with an item def): remove 1 tool
                                   1: add 1 reward (zero option block)

The functions here decide the result and apply the SAME change to the server's bag model
(inventory.Inventory over the character record), so the two bags stay equal and the change
survives a portal (the next S2C 0x03 lists the model) and a relog (store.py). Nothing here
sends a packet or touches a session: GameServer._handle_craft_complete / _reinforce_ /
_gather_ send the one reply, persist, rate-limit and log. Both EN builds: every rule and
table below is the same in the 2008 and the 2009 (Build 14) exe - VAs are cited for both.

Concoction / Mineral Refining (F11)
-----------------------------------
A recipe is the product's own hii record: `Union` / `Union_Cnt` = 8 x (material, count),
`Union_Kind` 1 = Concoction (window 0x28D, the Alchemy station 0x13B) / 2 = Mineral Refining
(window 0x28E, the Forge 0x13C). The item loader files every record with Union_Kind != 0 in
the matching recipe list (FUN_00403fa0 -> FUN_00467e20), so the window lists ALL recipes of
its kind; what the skill level changes is the success chance the window prints:

    FUN_00468280 (2009 FUN_004720a0): "Success probability:%d%%" =
        table[skill_level * 11 + col],  col = product Lv / 10  (Lv 99 -> 10)
    table = DAT_006f09ec (2008) = DAT_005250c4 (2009), rows 1..10 byte-identical.

skill_level is the Skill_Lv (def+0x1DC) of the cast skill: Concoction 0x881..0x88A
(2177..2186), Mineral Refining 0x876..0x87F (2166..2175) - the S2C 0x25 ranges that open the
windows (2008 0x44D5D0, 2009 FUN_0044f070). The server reads the learned level from the
character's skill list. EN recipe materials are all Type 0 or 2; a Type 1 material removes
exactly ONE instance whatever its count (FUN_00467140 -> FUN_00423ff0 with no block: the
first slot with that id). The client pre-checks every material ("Out of item.") and room for
the product ("There isn't empty space ...") before it starts, so the refusals below are for
a bag the server disagrees with, not for normal play.

Reinforcement (F12)
-------------------
C2S 0x68 names a BAG equipment instance (id + its 6 words) and an Elementirium stone
(2975..2979 Chipped/Flawed/-/Flawless/Perfect, Lv 7/14/25/49/59; the client accepts
0xB9F..0xBA4 but 2980 is "Melee Attack Booster", Type 0, so the range is 0xB9F..0xBA3). A
success writes an option STONE into the first zero word: the elemental option stones
2945..2974 are six groups of five grades (Lv 1..5 = Chipped..Perfect: Earth Tol, Wind Dex,
Fire max HP, Water max MP, Earth HP regen, Wind MP regen - hii Attribute / columns). Retail's
pick is unknown (Q8): the grade follows the Elementirium grade and the group is random. The
success rate is config REINFORCE_SUCCESS_PCT (retail unknown). A failure consumes NOTHING:
the client keeps the stone on every non-1 result, so the model does too.

Gathering (F13)
---------------
`gather_node_record_index` is the node template's hni position (en_content.gather_nodes).
The client's gate (FUN_00467b00, 2009 FUN_00471960): a garden shovel 0x8A7..0x8AA for a herb
(node type 2, Herb Gathering 0x56 learned), a shovel 0x8AB..0x8AE for a mineral vein (type 1,
Mining 0x52), tool Lv >= node Lv (FUN_00427310) and tool Lv <= the character level. The
reward is rolled from the node's hni `item:` / `Drop:` columns: every node's rates add up to
65550 (63650 for the Common Mineral Vein), which reads as a per-100000 chance - config
GATHER_RATE_DIVISOR (0 = always a reward, weighted by the same column). The tool goes on
EVERY result (the client removes it before it looks at the result), the reward is added
only on result 1.

Elemental stone extraction (F14; item_inventory-stone-extraction, P8 stage 4)
----------------------------------------------------------------------------
Not client-timed: window 0x473 (opened by an Element Separator from the cash bag) sends C2S
0x72 {tool, equipment, picked stone, the 6 record words} and closes. extract() takes the
stone out of the bag instance (the first equal socket, the rest shift down) and bags it;
GameServer._handle_stone_extract consumes one use of the tool's cash record (cash.consume,
the client's own consume-by-serial of the 0x9C serial) and sends S2C 0x9C then the 0x18
that grants the stone. Any refusal changes nothing and is 0x9C {0}.
"""
from collections import namedtuple
import random as _random

import en_content as EC
import inventory as INV
import records as R
import skills as SK

# ------------------------------------------------------------------ results ---
RESULT_OK = 1
RESULT_FAILED = 0                 # 0x8D: "<kind>Failed." materials consumed; 0x8E/0x8F: plain failure
RESULT_GENERIC = 2                # 0x8D: plain "<kind>Failed." with NO consumption (any other value)
RESULT_SHORT = 0x0F               # 0x8D: "Short of material item", nothing consumed
RESULT_NO_SKILL = 0x10            # 0x8D: "The skill hasn't been learned yet.", nothing consumed
RESULT_FULL = 0x11                # 0x8D: "Out of empty slot..." + materials consumed
                                  # 0x8F: "There isn't empty space in the inventory." (tool gone)
RESULT_CANNOT_REINFORCE = 0x11    # 0x8E: "This equipment cannot be reinforced.", no change

# ----------------------------------------------------------- craft content ---
UNION_KIND_CONCOCTION = 1         # window 0x28D, ctx+0x58 skill level
UNION_KIND_REFINING = 2           # window 0x28E, ctx+0x5A skill level
# First Lv-1 id of each craft skill family (the S2C 0x25 window ranges). 10 levels each.
CRAFT_SKILL_FIRST = {UNION_KIND_CONCOCTION: 0x881, UNION_KIND_REFINING: 0x876}
CRAFT_SKILL_LEVELS = 10
CRAFT_KIND_NAMES = {UNION_KIND_CONCOCTION: 'Concoction', UNION_KIND_REFINING: 'Mineral Refining'}
# DAT_006f09ec (2008) / DAT_005250c4 (2009): success % by [skill level][product Lv / 10].
# Row 0 is unrelated data in both exes (skill level 0 = not learned: result 0x10 instead).
CRAFT_SUCCESS_PCT = (
    None,
    (70, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (75, 15, 1, 0, 0, 0, 0, 0, 0, 0, 0),
    (80, 30, 15, 1, 0, 0, 0, 0, 0, 0, 0),
    (85, 45, 30, 15, 1, 0, 0, 0, 0, 0, 0),
    (90, 60, 45, 30, 15, 1, 0, 0, 0, 0, 0),
    (95, 75, 60, 45, 30, 15, 1, 0, 0, 0, 0),
    (100, 90, 75, 60, 45, 30, 15, 1, 0, 0, 0),
    (100, 100, 90, 75, 60, 45, 30, 15, 1, 0, 0),
    (100, 100, 100, 90, 75, 60, 45, 30, 15, 1, 0),
    (100, 100, 100, 100, 90, 75, 60, 45, 30, 15, 1),
)
CRAFT_SUCCESS_COLUMNS = 11

# ------------------------------------------------------- reinforce content ---
REINFORCE_SKILL = 0x88C           # 2188 Reinforce (single level; FUN_004687f0 opens window 0x28C)
ELEMENTIRIUM_FIRST, ELEMENTIRIUM_LAST = 0xB9F, 0xBA3      # 2975..2979
OPTION_STONE_FIRST = 2945         # 2945..2974 elemental option stones
OPTION_STONE_GRADES = 5           # Chipped, Flawed, -, Flawless, Perfect (hii Lv 1..5)
OPTION_STONE_GROUPS = 6
OPTION_STONE_LAST = OPTION_STONE_FIRST + OPTION_STONE_GROUPS * OPTION_STONE_GRADES - 1     # 2974
DEFAULT_REINFORCE_SUCCESS_PCT = 70

# ------------------------------------------------------ extraction content ---
# The Element Separators (item_inventory F14, I-20 / Q12; hii Type 5 cash items, Cash_Cls 14,
# Cash_T 1 counted, Cash_V 1 or 6 uses - the same rows in the 2008 and 2009 hii). C2S 0x72
# mode_id IS the tool the window 0x473 was opened with. The selective ones need a picked
# stone (the client itself refuses 0xF70 / 0xD6D without one, "Select the elemental stone to
# extract."); the random ones send stone 0 and the server rolls the socket. 4378 is KR-only.
EXTRACT_SELECTIVE = frozenset({0xF70, 0xD6D})      # 3952 Selective x1, 3437 Selective x6
EXTRACT_RANDOM = frozenset({0xF6F, 0xD6C})         # 3951 Random x1, 3436 Random x6

# ---------------------------------------------------------- gather content ---
HERB_GATHERING_SKILL = 0x56       # 86
MINING_SKILL = 0x52               # 82
GARDEN_SHOVELS = range(0x8A7, 0x8AB)      # 2215..2218: herbs
SHOVELS = range(0x8AB, 0x8AF)             # 2219..2222: mineral veins
GATHER_TOOLS = {EC.NODE_TYPE_HERB: (GARDEN_SHOVELS, HERB_GATHERING_SKILL, 'Herb Gathering'),
                EC.NODE_TYPE_MINERAL: (SHOVELS, MINING_SKILL, 'Mining')}
DEFAULT_GATHER_RATE_DIVISOR = 100000

# Client-timed features: a completion comes at least one progress bar after the previous
# one. 5000 ms in both builds, except the 2009 gather bar, which drops to 4500 ms with a
# certain pet out (FUN_00471960: float 5000.0 or 4500.0 at ctx+0x64). The server allows 4 s.
MIN_INTERVAL_SECS = 4.0

Material = namedtuple('Material', 'item_id count tab')
CraftOutcome = namedtuple('CraftOutcome', 'result product kind skill_level pct consumed granted why')
ReinforceOutcome = namedtuple('ReinforceOutcome', 'result equip_id stone_id old_words new_option why')
ExtractOutcome = namedtuple('ExtractOutcome', 'result equip_id old_words stone_id why')
GatherOutcome = namedtuple('GatherOutcome', 'result tool_id reward node tool_removed why')


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _catalog(catalog):
    return EC.items() if catalog is None else catalog


def _learned(char, first, span):
    """The learned id of the family [first, first + span - 1] (FUN_00426c50), or 0."""
    return SK.learned_in_family(SK.learned(char), first, span)


# ================================================================ crafting ===
def craft_kind(item_def):
    """UNION_KIND_CONCOCTION / UNION_KIND_REFINING for a recipe product, or None."""
    if item_def is None or item_def.union_kind not in CRAFT_SKILL_FIRST or not item_def.recipe:
        return None
    return item_def.union_kind


def craft_skill_level(char, kind, catalog=None):
    """The level (1..10) of the craft skill of `kind` the character has learned, 0 if none."""
    first = CRAFT_SKILL_FIRST.get(kind)
    if first is None:
        return 0
    sid = _learned(char, first, CRAFT_SKILL_LEVELS)
    if not sid:
        return 0
    sd = SK.skill_def(sid, _catalog(catalog))
    return sd.level if sd is not None else sid - first + 1


def success_pct(skill_level, product_lv):
    """The client's "Success probability" for a skill level and a product Lv (FUN_00468280:
    column Lv / 10, Lv 99 -> 10). 0 for an unlearned skill."""
    level = _int(skill_level)
    if not 1 <= level <= CRAFT_SKILL_LEVELS:
        return 0
    lv = max(0, _int(product_lv))
    col = 10 if lv == 99 else lv // 10
    return CRAFT_SUCCESS_PCT[level][min(col, CRAFT_SUCCESS_COLUMNS - 1)]


def materials(item_def, catalog=None):
    """The recipe as the client consumes it: [Material(id, count, tab)] with duplicate ids
    merged and a Type-1 material counted as ONE instance per entry (FUN_00423ff0 removes
    exactly one, whatever Union_Cnt says).
    A material the client has no record of has tab None: the client's material check
    (FUN_004679c0) shows it as missing, so the recipe can never be started."""
    catalog = _catalog(catalog)
    merged = {}
    for mat, count in item_def.recipe:
        tab = catalog.tab_of(mat)
        tab = tab if tab in INV.TABS else None
        # the client's loop removes per ENTRY: a repeated id is removed again (one more
        # instance for equipment, `count` more for a stack)
        need = 1 if tab == 'equip' else max(1, _int(count))
        if mat in merged:
            need += merged[mat].count
        merged[mat] = Material(mat, need, tab)
    return list(merged.values())


def _scratch(bag):
    """A throw-away copy of the character's bag `bag` (an INV.Inventory) to simulate a change
    on (the product's room is checked AFTER the materials leave, the order the client applies
    them). The copy holds `inventory` alone, so it carries the real bag's pet count: a 2009
    bagged pet takes an equipment-tab slot (INV.Inventory `pets`), and without it a pet-filled
    tab would pass the check and the product be lost after the materials were consumed."""
    import copy
    return INV.Inventory({'inventory': copy.deepcopy(bag.char.get('inventory')), 'equipped': {}},
                         bag.catalog, pets=bag.pet_slots())


def _remove_materials(bag, mats):
    for m in mats:
        if m.tab is not None:
            bag.remove(m.item_id, m.count)


def craft(char, product_id, catalog=None, rng=None):
    """Decide and apply one Concoction / Mineral Refining completion (item_inventory F11).
    Returns a CraftOutcome; the character's bag already holds the result (consumed
    materials removed, product added) when it returns - exactly what S2C 0x8D `result` makes
    the client do, and nothing on the no-change results."""
    catalog = _catalog(catalog)
    rng = rng or _random
    product_id = _int(product_id)
    d = catalog.get(product_id) if catalog.exists(product_id) else None

    def out(result, why, kind=None, level=0, pct=0, consumed=(), granted=False):
        return CraftOutcome(result, product_id, kind, level, pct, list(consumed), granted, why)

    if d is None:
        # The client drops a 0x8D whose product it has no record of (busy flag stays set);
        # the reply is still the no-change one.
        return out(RESULT_SHORT, f'product {product_id} is not in the EN client catalog')
    kind = craft_kind(d)
    if kind is None:
        return out(RESULT_GENERIC, f'item {product_id} has no recipe (Union_Kind {d.union_kind})')
    level = craft_skill_level(char, kind, catalog)
    if level <= 0:
        return out(RESULT_NO_SKILL, f'{CRAFT_KIND_NAMES[kind]} is not learned', kind)
    pct = success_pct(level, d.lv)
    mats = materials(d, catalog)
    bag = INV.Inventory(char, catalog)
    short = [m for m in mats if m.tab is None or bag.count(m.item_id) < m.count]
    if short:
        return out(RESULT_SHORT, 'short of ' + ', '.join(
            f'{m.item_id} x{m.count} (have {bag.count(m.item_id) if m.tab else "no record"})' for m in short),
            kind, level, pct)
    # Room for the product once the materials are gone (the client removes them first).
    scratch = _scratch(bag)
    _remove_materials(scratch, mats)
    full = scratch.fits(product_id, 1)
    if full is not None:
        _remove_materials(bag, mats)
        return out(RESULT_FULL, f'no room for the product: {full}', kind, level, pct, mats)
    if rng.randrange(100) >= pct:
        _remove_materials(bag, mats)
        return out(RESULT_FAILED, f'roll failed ({pct}% at skill Lv {level}, product Lv {d.lv})',
                   kind, level, pct, mats)
    _remove_materials(bag, mats)
    bag.add(product_id, 1)
    return out(RESULT_OK, f'{pct}% at skill Lv {level}', kind, level, pct, mats, True)


# ============================================================ reinforcement ===
def is_elementirium(item_id):
    return ELEMENTIRIUM_FIRST <= _int(item_id) <= ELEMENTIRIUM_LAST


def is_option_stone(item_id):
    """An elemental option stone 2945..2974: the only word a socket gets from the client's
    reinforcement (option_stone) and the only one an extraction may hand back."""
    return OPTION_STONE_FIRST <= _int(item_id) <= OPTION_STONE_LAST


def option_stone(stone_id, group):
    """The option stone a success writes: the Elementirium's grade in option group `group`."""
    grade = _int(stone_id) - ELEMENTIRIUM_FIRST
    return OPTION_STONE_FIRST + (_int(group) % OPTION_STONE_GROUPS) * OPTION_STONE_GRADES + grade


def packed(words):
    """True when w0..w4 have no zero before a non-zero word: the client keeps them packed
    (FUN_004241d0 fills the first zero, 0x9C extraction shifts down), so a gap means a record
    the server cannot match the way the client will."""
    head = [_int(w) for w in list(words)[:INV.WIRE_OPTION_WORDS]]
    seen_zero = False
    for w in head:
        if not w:
            seen_zero = True
        elif seen_zero:
            return False
    return True


def reinforce(char, equip_id, stone_id, words, catalog=None, rng=None, success_rate=None):
    """Decide and apply one Reinforcement completion (item_inventory F12). `words` = the six
    u16 C2S 0x68 carried (the bag record's +0x02..+0x0C). Returns a ReinforceOutcome whose
    old_words are those six words unchanged (S2C 0x8E success echoes them: the client finds
    the slot by a memcmp of those 12 bytes)."""
    catalog = _catalog(catalog)
    rng = rng or _random
    equip_id, stone_id = _int(equip_id), _int(stone_id)
    words = [_int(w) & 0xFFFF for w in list(words or [])][:INV.OPTION_WORDS]
    words += [0] * (INV.OPTION_WORDS - len(words))
    pct = DEFAULT_REINFORCE_SUCCESS_PCT if success_rate is None else max(0, min(100, _int(success_rate)))

    def out(result, why, new_option=0):
        return ReinforceOutcome(result, equip_id, stone_id, list(words), new_option, why)

    bag = INV.Inventory(char, catalog)
    equip = catalog.get(equip_id) if catalog.exists(equip_id) else None
    stone = catalog.get(stone_id) if catalog.exists(stone_id) else None
    if equip is None or bag.tab_of(equip_id) != 'equip':
        return out(RESULT_CANNOT_REINFORCE, f'{equip_id} is not EN equipment')
    if not is_elementirium(stone_id) or stone is None:
        return out(RESULT_CANNOT_REINFORCE, f'{stone_id} is not an Elementirium '
                                            f'({ELEMENTIRIUM_FIRST}..{ELEMENTIRIUM_LAST})')
    if not packed(words):
        return out(RESULT_CANNOT_REINFORCE, f'option words {words} are not packed')
    if all(words[:INV.WIRE_OPTION_WORDS]):
        return out(RESULT_CANNOT_REINFORCE, f'no free option word in {words[:INV.WIRE_OPTION_WORDS]}')
    if stone.lv > equip.lv:
        return out(RESULT_CANNOT_REINFORCE, f'stone Lv {stone.lv} > equipment Lv {equip.lv}')
    instance = bag.instance(equip_id, words)
    if instance is None:
        return out(RESULT_CANNOT_REINFORCE, f'no bag instance of {equip_id} with block {words}')
    if bag.count(stone_id) < 1:
        return out(RESULT_CANNOT_REINFORCE, f'no {stone_id} in the bag')
    if not _learned(char, REINFORCE_SKILL, 1):
        return out(RESULT_FAILED, f'Reinforce ({REINFORCE_SKILL}) is not learned')
    if rng.randrange(100) >= pct:
        return out(RESULT_FAILED, f'roll failed ({pct}%)')
    new_option = option_stone(stone_id, rng.randrange(OPTION_STONE_GROUPS))
    if not catalog.exists(new_option):
        # the client would take the failure path for an id past its table (spec 0x8E)
        return out(RESULT_FAILED, f'option stone {new_option} is not in the EN client catalog')
    # FUN_004241d0: the first zero word among w0..w4 of the matched slot. The model's block
    # is packed, so that is right after the last option and it stays packed.
    block = INV.pack_words(instance['w'])
    block[block[:INV.WIRE_OPTION_WORDS].index(0)] = new_option
    instance['w'] = block
    bag.remove(stone_id, 1)
    return out(RESULT_OK, f'{pct}%', new_option)


# ================================================================ extraction ===
def extract_tool_kind(mode_id):
    """'selective' / 'random' for an Element Separator id (C2S 0x72 mode_id), else None."""
    mode_id = _int(mode_id)
    if mode_id in EXTRACT_SELECTIVE:
        return 'selective'
    if mode_id in EXTRACT_RANDOM:
        return 'random'
    return None


def extract(char, mode_id, equip_id, stone_id, words, catalog=None, rng=None, tool_owned=True):
    """Decide and apply one elemental stone extraction (item_inventory F14; C2S 0x72, spec
    0x46C0B8/0x72, 2009 0x4765EE/0x72). `words` = the six u16 the request carried (socket
    words w0..w4 + equip_extra = the bag record's +0x02..+0x0C), `tool_owned` whether the
    caller found a usable cash record of the tool (the premium_cash serial the 0x9C names).

    Success (ExtractOutcome.result 1) has already changed the model the way S2C 0x9C + 0x18
    change the client: the stone word is taken out of the matched bag instance and the later
    words shift toward w0 (FUN_00424260 / 2009 FUN_00425690: the FIRST socket equal to
    stone_id, then the shift, w5 untouched), and the stone is added to the etc tab (0x18 -
    0x9C itself grants nothing). old_words are the six words the client sent: the 0x9C
    echoes them, because the client finds its slot by a memcmp of exactly those 12 bytes.
    stone_id is the stone actually removed and is never 0: a 0 would make the client pick
    rand() % count on its own (a desync), and with every socket empty divide by zero
    (0x42430D / 0x42573D). A random tool (3951 / 3436) sends stone 0; the server rolls the
    socket among the words that are elemental option stones (is_option_stone): a socket word
    that is none (GM-made or corrupt data, P8 review) is never handed out through the 0x18 as a
    "stone" - a selective pick of it is refused. Any other result changes nothing (the caller
    answers 0x9C {0}: "Elemental stone extraction failed.", the client consumes no tool)."""
    catalog = _catalog(catalog)
    rng = rng or _random
    mode_id, equip_id, stone_id = _int(mode_id), _int(equip_id), _int(stone_id)
    words = [_int(w) & 0xFFFF for w in list(words or [])][:INV.OPTION_WORDS]
    words += [0] * (INV.OPTION_WORDS - len(words))

    def out(result, why, stone=0):
        return ExtractOutcome(result, equip_id, list(words), stone, why)

    kind = extract_tool_kind(mode_id)
    if kind is None:
        return out(RESULT_FAILED, f'mode {mode_id} is no Element Separator '
                                  f'({sorted(EXTRACT_SELECTIVE | EXTRACT_RANDOM)})')
    if not tool_owned:
        return out(RESULT_FAILED, f'no usable {mode_id} record in the cash inventory')
    bag = INV.Inventory(char, catalog)
    if not catalog.exists(equip_id) or bag.tab_of(equip_id) != 'equip':
        return out(RESULT_FAILED, f'{equip_id} is not EN equipment')
    if not packed(words):
        return out(RESULT_FAILED, f'socket words {words} are not packed')
    sockets = [w for w in words[:INV.WIRE_OPTION_WORDS] if w]
    if not sockets:
        # the client's own gate ("The equipment doesn't have without elemental stone..")
        return out(RESULT_FAILED, f'no stone in the sockets of {equip_id}')
    instance = bag.instance(equip_id, words)
    if instance is None:
        return out(RESULT_FAILED, f'no bag instance of {equip_id} with block {words}')
    if kind == 'selective':
        if not stone_id:
            return out(RESULT_FAILED, 'a selective separator needs a picked stone')
        if stone_id not in sockets:
            return out(RESULT_FAILED, f'stone {stone_id} is not in the sockets {sockets}')
        if not is_option_stone(stone_id):
            return out(RESULT_FAILED, f'socket word {stone_id} is not an elemental option stone '
                                      f'({OPTION_STONE_FIRST}..{OPTION_STONE_LAST})')
        taken = stone_id
    else:
        stones = [w for w in sockets if is_option_stone(w)]
        if not stones:
            return out(RESULT_FAILED, f'no elemental option stone among the socket words {sockets}')
        taken = stones[rng.randrange(len(stones))]
    full = bag.fits(taken, 1)
    if full is not None:
        return out(RESULT_FAILED, f'the stone {taken} does not fit: {full}')
    block = INV.pack_words(instance['w'])
    head = block[:INV.WIRE_OPTION_WORDS]
    head.remove(taken)                       # the first equal word; the later ones shift down
    instance['w'] = head + [0] + [block[5]]
    bag.add(taken, 1)
    how = 'picked' if kind == 'selective' else f'rolled from {sockets}' + (
        f' (client named {stone_id})' if stone_id else '')
    return out(RESULT_OK, f'{kind} separator, stone {taken} {how}', taken)


# ================================================================ gathering ===
def gather_rewards(node, catalog=None):
    """[(item id, rate)] of a node's drop columns that the client can put in a bag tab (an id
    past the EN table, or a Type 3/4/5 record, is never granted)."""
    catalog = _catalog(catalog)
    return [(i, max(0, _int(r))) for i, r in node.drops
            if catalog.exists(i) and catalog.tab_of(i) in INV.TABS]


def roll_reward(node, catalog=None, rng=None, divisor=DEFAULT_GATHER_RATE_DIVISOR):
    """The reward id of one successful gather, or 0 for "Failed to obtain item.". divisor:
    the unit of the hni Drop column (a per-`divisor` chance per entry); 0 = always a reward,
    picked with the column as weights."""
    rng = rng or _random
    rewards = [(i, r) for i, r in gather_rewards(node, catalog) if r > 0]
    total = sum(r for _, r in rewards)
    if not rewards or total <= 0:
        return 0
    divisor = _int(divisor)
    roll = rng.randrange(total if divisor <= 0 else max(divisor, 1))
    for item_id, rate in rewards:
        if roll < rate:
            return item_id
        roll -= rate
    return 0


def gather(char, map_code, node_index, tool_id, catalog=None, npcs=None, rng=None,
           divisor=DEFAULT_GATHER_RATE_DIVISOR, refuse=None):
    """Decide and apply one gathering completion (item_inventory F13). The tool leaves the
    bag on every result when the client has a record of it (it removes one before it looks
    at the result), so the model mirrors that first. `refuse`: a reason decided by the
    caller (rate limit) that fails the gather after the tool is gone, like any failure."""
    catalog = _catalog(catalog)
    npcs = EC.npcs() if npcs is None else npcs
    rng = rng or _random
    tool_id, node_index = _int(tool_id), _int(node_index)
    node = npcs.at_index(node_index) if node_index > 0 else None

    def out(result, why, reward=0, removed=False):
        return GatherOutcome(result, tool_id, reward, node, removed, why)

    if not catalog.exists(tool_id):
        # no record: the client only clears its busy flag (no tool removal, no text)
        return out(RESULT_FAILED, f'tool {tool_id} is not in the EN client catalog')
    bag = INV.Inventory(char, catalog)
    owned = bag.count(tool_id) >= 1
    removed = owned and bag.remove(tool_id, 1) == 1
    if refuse:
        return out(RESULT_FAILED, refuse, removed=removed)
    if not owned:
        return out(RESULT_FAILED, f'tool {tool_id} is not in the bag', removed=removed)
    if node is None or node.type not in GATHER_TOOLS:
        return out(RESULT_FAILED, f'record {node_index} is no gather node', removed=removed)
    if map_code is None or node.idx not in EC.gather_nodes(map_code):
        return out(RESULT_FAILED, f'no {node.name} ({node.idx}) on map {map_code}', removed=removed)
    tools, skill, what = GATHER_TOOLS[node.type]
    if tool_id not in tools:
        return out(RESULT_FAILED, f'{tool_id} is not a {what} tool ({tools.start}..{tools.stop - 1})',
                   removed=removed)
    if not _learned(char, skill, 1):
        return out(RESULT_FAILED, f'{what} ({skill}) is not learned', removed=removed)
    tool = catalog.get(tool_id)
    if tool.lv < node.lv:
        return out(RESULT_FAILED, f'tool Lv {tool.lv} < node grade {node.lv}', removed=removed)
    if tool.lv > R.level_of(char):
        return out(RESULT_FAILED, f'tool Lv {tool.lv} > character level {R.level_of(char)}',
                   removed=removed)
    reward = roll_reward(node, catalog, rng, divisor)
    if not reward:
        return out(RESULT_FAILED, f'nothing found ({node.name}, rates per {divisor or "weight"})',
                   removed=removed)
    full = bag.fits(reward, 1)
    if full is not None:
        return out(RESULT_FULL, f'no room for {reward}: {full}', removed=removed)
    bag.add(reward, 1)
    return out(RESULT_OK, node.name, reward, removed)
