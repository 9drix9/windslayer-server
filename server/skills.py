#!/usr/bin/env python3
"""
skills.py - skill definitions and the client's skill-range tables (cs-skill-defs)
================================================================================
combat_skill.md 1.1 / 1.2 / 3.3. A skill is an item-table record (hii `Type` 3) addressed by
its u16 id; every skill packet carries only that id, and the client takes cost, cooldown,
duration and animation from its OWN record. So the server's numbers must be the EN client's:

    import skills as SK
    sd = SK.skill_def(292)            # Increase Attack Power Lv1
    sd.mp_cost, sd.ct, sd.con         # 5, 10000, 30000
    sd.family, sd.level               # 291, 1
    sd.kind                           # 'self_buff'
    SK.skill_def(501).kind            # 'self_heal'   (Self Heal Lv1, HP +20)
    SK.skill_def(2609).kind           # 'trap'        (Booby Trap Lv1, Con 7020)
    SK.skill_def(179)                 # None: the Wooden Stick is not a skill

Source (F7, 1.13): the EN `hs/windslayer.hii` through en_content. The KR gamedef was diffed
against it for this stage: all 1186 Type-3 rows with id <= 4248 carry the same Type / HP /
MP / mHP / mMP / Job / Job2 / Attribute / Attri_* / Skill_P_* / Skill_A_* / Lv / Buy / PMoney
/ CT / Con / Skill_Lv (combat_skill.md open question 16: answered, no difference), and
test_skills.py re-checks that whenever both files are present. gamedef is therefore never
read here.

The range functions are ports of the client's hard-coded id tables (decomp checked for
this stage, not copied from the design doc):

    FUN_004257a0  slot_inserting(id)   0 for the targeted/debuff ranges: an S2C 0x3B for
                                       them inserts no buff slot (use 0x41 on the target)
    FUN_004258a0  forced_slot(id)      1 for the summon ranges that get a 0x3B slot even
                                       with Con 0
    FUN_004258d0  caster_hp(id)        0 when the record's HP is NOT applied to the caster
                                       by the 0x25/0x3B self logic (the value is the effect
                                       on the target: Poison -3, Heal +40, ...)
    FUN_00424e20  party_buff(id)       the aura / group-buff ranges the caster's client fans
                                       out to its party
    FUN_00403fa0  family_max_level(id) def+0x142: 10, or 1 for the single-level families

Nothing here sends a packet. The learned-skill MODEL (cs-skill-learn) lives here too, as
pure functions over the character record; the server sends what they decide:

    SK.learn(char, 292, level=12)             # Learned(ok=True, id=292, replaced=None, why='')
    SK.learn(char, 94, check=False)           # a quest reward the client already learned
    SK.learned(char)                          # [292, 94] - what the 0x07 skill list carries
    SK.ensure(char)                           # store migration: char['skills'] normalized

Casting is cs-skill-cast (GameServer._handle_cast_skill), buffs and their expiry cs-buffs
(buffs.py).
"""
from collections import namedtuple

import en_content as EC
import progression

# The EN item table size (hii header): a skill id above it does not exist client-side.
SKILL_MAX_ID = EC.EN_ITEM_MAX_ID
TYPE_SKILL = EC.TYPE_SKILL
# Learned-skill slots of the client entity (+0x29A u16[30], cooldown stamps +0x7B4 u32[30]).
MAX_LEARNED = 30
# Buff slots of the client entity (+0xE84, 21 x 0x18).
MAX_BUFF_SLOTS = 21
# 0x3B slot duration the client forces for summons (FUN_00424e20 @0x425124: MOV [slot+0xC],
# 0xEA60 for 0x9C3..0x9F9), whatever the record's Con says.
SUMMON_SLOT_MS = 60000

Range = namedtuple('Range', 'lo hi')


def _ranges(*pairs):
    return tuple(Range(lo, hi) for lo, hi in pairs)


def _in(skill_id, ranges):
    try:
        skill_id = int(skill_id) & 0xFFFF
    except (TypeError, ValueError):
        return False
    return any(r.lo <= skill_id <= r.hi for r in ranges)


# FUN_004257a0 returns 0 for these (the 0x3B handler then reads no x,y and inserts nothing).
TARGETED_RANGES = _ranges(
    (0x187, 0x191),   # Poison
    (0x1B3, 0x1BD),   # Stun
    (0x242, 0x24C),   # Threaten
    (0x78B, 0x795),   # Arrow Grapple
    (0x7B7, 0x7C1),   # Thornbush
    (0x8BB, 0x8C5),   # Weapon Shackle
    (0x981, 0x98B),   # Cartilage Smash
    (0x997, 0x9A1),   # Poison Arrow
    (0xA05, 0xA0F),   # Forced Blindfold
    (0xA3C, 0xA46),   # Puppet
    (0xA47, 0xA51),   # Time Bomb
    (0xA52, 0xA5C),   # Spider Web
    (0xA5D, 0xA67),   # Poison Cloud
    (0xB0F, 0xB19),   # Strike of Darkness
    (0xB30, 0xB3A),   # Frenzy
    (0xB3B, 0xB45),   # Mutation
)
# FUN_004258a0 returns 1 for these (summons whose 0x3B slot is inserted even with Con 0).
FORCED_SLOT_RANGES = _ranges((0x9CE, 0x9D8), (0x9E4, 0x9EE), (0x9EF, 0x9F9))
# FUN_004258d0 returns 0 for these: the record's HP is the effect on the TARGET, so the
# caster's own client does not add it (server: caster_hp_delta = HP only if caster_hp()).
NOT_CASTER_HP_RANGES = _ranges(
    (0x187, 0x191),   # Poison
    (0x1BE, 0x1C8),   # Heal
    (0x24D, 0x257),   # Remote Heal
    (0x8C6, 0x8D0),   # Berserk (self-damage ticks instead, host-only on the client)
    (0x8F2, 0x8FC),   # Healing Aura
    (0x997, 0x9A1),   # Poison Arrow
    (0x9C3, 0x9F9),   # summons (five 11-id blocks, contiguous)
    (0xA5D, 0xA67),   # Poison Cloud
    (0xAF9, 0xB03),   # Breath of Life
    (0xB0F, 0xB19),   # Strike of Darkness
    (0xB25, 0xB2F),   # Vampiric Attack
)
# FUN_00424e20 returns non-zero (party fan-out) for these: Advance / Healing / Support /
# Movement auras and the group buffs (Group Protection, Breath of Vitality).
PARTY_RANGES = _ranges((0x8E7, 0x912), (0xAE3, 0xAF8))
# Classifier-only ranges (combat_skill.md 3.3; the client special-cases them in the 0x25
# handler and FUN_00416380).
TRAP_RANGES = _ranges((0xA31, 0xA3A))            # Booby Trap Lv1-10 (0xA3B is Puppet's base)
PARTY_HEAL_RANGES = _ranges((0xAD8, 0xAE1))      # Group Heal
UTILITY_RANGES = _ranges((0xC2, 0xC2), (0x876, 0x88C))   # Open Stall; refining/concoction/reinforce UIs
SUMMON_RANGES = _ranges((0x9C3, 0x9F9))
# Client-side ranges the server must emulate because FUN_00417e10 runs them only as a room
# host (combat_skill.md 1.2 last rows): DoT every 990 ms, Healing Aura every 5010 ms, and
# the delayed detonation of Cartilage Smash / Time Bomb when the slot reaches 0.
DOT_RANGES = _ranges((0x187, 0x191), (0x997, 0x9A1), (0x9C3, 0x9CD), (0xA5D, 0xA67), (0xB0F, 0xB19))
DETONATE_RANGES = _ranges((0x981, 0x98B), (0xA47, 0xA51))
AURA_HEAL_RANGES = _ranges((0x8F2, 0x8FC))
BERSERK_RANGES = _ranges((0x8C6, 0x8D0))
DOT_PERIOD_MS = 990              # FUN_00417e10: slot % 0x3DE
AURA_HEAL_PERIOD_MS = 5010       # FUN_00417e10: slot % 0x1392
# HP-cost gate of FUN_0044c090: these casts need cur HP > |HP| ("Not enough HP.").
HP_GATED_RANGES = _ranges((0x124, 0x12E), (0x8DC, 0x8E6))
# FUN_00403fa0: def+0x142 is 1 for these Type-3 ids (Dash, Mining, Herb Gathering, Strong
# Attack, Guard, Double Jump, Open Stall, Reinforce, Fairy Attack, Beast Attack), else 10.
SINGLE_LEVEL_IDS = frozenset((0x50, 0x52, 0x56, 0x58, 0x5A, 0x5E, 0xC2, 0x88C, 0xAD6, 0xC2C))

# Passive families FUN_0041ace0 reads for natural regeneration (hpmp.regen): the learned id
# in [first, first + 9] (FUN_00426c50, EDI = 10) adds its HP / MP column every 15 s.
REGEN_HP_FAMILY_FIRST = 0x6F     # 111..120 Improve Rejuvenation Lv1-10
REGEN_MP_FAMILY_FIRST = 0x1EA    # 490..499 Improve Mana Recovery Lv1-10
PASSIVE_FAMILY_SPAN = 10

KINDS = ('passive', 'base', 'trap', 'party_heal', 'aura', 'utility', 'summon', 'debuff',
         'self_buff', 'target_heal', 'self_heal', 'attack')
# Content fixes: {family base id: kind} wins over the classifier (combat_skill.md 3.3).
SKILL_KIND_OVERRIDE = {}


def slot_inserting(skill_id):
    """FUN_004257a0: 1 unless the id is in a targeted (debuff) range."""
    return not _in(skill_id, TARGETED_RANGES)


def forced_slot(skill_id):
    """FUN_004258a0: 1 for the summon ranges that get a buff slot with Con 0."""
    return _in(skill_id, FORCED_SLOT_RANGES)


def caster_hp(skill_id):
    """FUN_004258d0: 1 when the record's HP value applies to the caster itself."""
    return not _in(skill_id, NOT_CASTER_HP_RANGES)


def party_buff(skill_id):
    """FUN_00424e20's party return: auras and group buffs fan out to the party."""
    return _in(skill_id, PARTY_RANGES)


def family_max_level(skill_id):
    """def+0x142 as FUN_00403fa0 writes it for a Type-3 record (quick-slot upgrades use it)."""
    try:
        return 1 if int(skill_id) in SINGLE_LEVEL_IDS else 10
    except (TypeError, ValueError):
        return 10


class SkillDef:
    """One Type-3 hii record with the derived values the cast / learn / buff code needs.
    Attribute names follow combat_skill.md 1.1; raw columns keep their hii meaning."""
    __slots__ = ('id', 'name', 'hp', 'mp', 'mhp', 'mmp', 'job', 'job2', 'attribute',
                 'attri_atk', 'attri_def', 'skill_p_a', 'skill_p_d', 'skill_a_a', 'skill_a_d',
                 'lv', 'buy', 'pmoney', 'ct', 'con', 'skill_lv')

    def __init__(self, item):
        self.id = int(item.id)
        self.name = EC.item_name(self.id)
        self.hp = int(item.hp)                  # def+0x158: signed, caster cost/heal or effect
        self.mp = int(item.mp)                  # def+0x15C: signed, cost when < 0
        self.mhp = int(item.mhp)                # def+0x160
        self.mmp = int(item.mmp)                # def+0x164
        job = list(item.job or [])
        self.job = tuple((job + [0] * 7)[:7])   # [0] traveler .. [6] priest
        self.job2 = tuple(item.job2 or ())
        self.attribute = int(item.attribute)    # element (damage)
        self.attri_atk = int(item.attri_atk)
        self.attri_def = int(item.attri_def)
        self.skill_p_a = int(item.skill_p_a)
        self.skill_p_d = int(item.skill_p_d)
        self.skill_a_a = int(item.skill_a_a)
        self.skill_a_d = int(item.skill_a_d)
        self.lv = int(item.lv)                  # required character level
        self.buy = int(item.buy)                # gold price at a skill master
        self.pmoney = int(item.pmoney)          # Victy price
        self.ct = int(item.ct)                  # cooldown, ms
        self.con = int(item.con)                # buff slot duration, ms (0 = instant)
        self.skill_lv = int(item.skill_lv)      # 1..10 castable, 0 base record, < 0 passive

    # ------------------------------------------------------------ derived ---
    @property
    def level(self):
        """Level inside the family (the client's family math uses abs(Skill_Lv))."""
        return abs(self.skill_lv)

    @property
    def family(self):
        """The family's base record id (`id - abs(Skill_Lv)`), the key of cooldowns and of
        "one level per family" in the learned list."""
        return self.id - abs(self.skill_lv)

    @property
    def passive(self):
        return self.skill_lv < 0

    @property
    def base(self):
        return self.skill_lv == 0

    @property
    def castable(self):
        """FUN_0044c090's first gate: Skill_Lv > 0 (signed)."""
        return self.skill_lv > 0

    @property
    def mp_cost(self):
        """MP the cast takes (the client's "MP low." gate compares cur MP >= |MP|)."""
        return -self.mp if self.mp < 0 else 0

    @property
    def hp_cost(self):
        """HP a cast takes from its caster (only when the HP value applies to the caster)."""
        return -self.hp if self.hp < 0 and self.caster_hp else 0

    @property
    def hp_gated(self):
        """FUN_0044c090 also requires cur HP > |HP| for these ids ("Not enough HP.")."""
        return _in(self.id, HP_GATED_RANGES)

    @property
    def caster_hp(self):
        return caster_hp(self.id)

    @property
    def caster_hp_delta(self):
        """What the 0x25/0x3B self logic adds to the caster's HP (cost or self heal)."""
        return self.hp if self.caster_hp else 0

    @property
    def slot_on_3b(self):
        """S2C 0x3B inserts a buff slot: (Con != 0 or FUN_004258a0) and FUN_004257a0."""
        return (self.con != 0 or forced_slot(self.id)) and slot_inserting(self.id)

    @property
    def duration_ms(self):
        """Slot duration the client gives this skill: Con, or 60 s for summons."""
        return SUMMON_SLOT_MS if _in(self.id, SUMMON_RANGES) else self.con

    @property
    def stat_mod(self):
        """Slot changes stats (0x43 must recompute on expiry, not 0x3C): any Skill_P/A
        modifier or a max-HP/MP bonus, or an aura (combat_skill.md F5 step 2)."""
        return bool(self.skill_p_a or self.skill_p_d or self.skill_a_a or self.skill_a_d
                    or self.mhp or self.mmp or party_buff(self.id))

    @property
    def party(self):
        return party_buff(self.id)

    @property
    def dot(self):
        return _in(self.id, DOT_RANGES)

    @property
    def detonates(self):
        return _in(self.id, DETONATE_RANGES)

    @property
    def max_family_level(self):
        return family_max_level(self.id)

    @property
    def kind(self):
        return classify(self)

    def allows_job(self, job1):
        """The class gate the client applies at learn time (FUN_00467680) and cast time
        (FUN_0044c090): `Job[job1] or Job[0]`."""
        try:
            job1 = int(job1)
        except (TypeError, ValueError):
            return False
        return bool(self.job[0] or (0 <= job1 < len(self.job) and self.job[job1]))

    def __repr__(self):
        return (f'<SkillDef {self.id} {self.name!r} lv={self.skill_lv} kind={self.kind} '
                f'mp={self.mp} hp={self.hp} ct={self.ct} con={self.con}>')


def classify(sd):
    """The skill kind, first match wins (combat_skill.md 3.3). An override keyed by the
    family base id wins over everything (content fixes)."""
    override = SKILL_KIND_OVERRIDE.get(sd.family)
    if override is not None:
        return override
    if sd.skill_lv < 0:
        return 'passive'
    if sd.skill_lv == 0:
        return 'base'
    if _in(sd.id, TRAP_RANGES):
        return 'trap'
    if _in(sd.id, PARTY_HEAL_RANGES):
        return 'party_heal'
    if party_buff(sd.id):
        return 'aura'
    if _in(sd.id, UTILITY_RANGES):
        return 'utility'
    if _in(sd.id, SUMMON_RANGES):
        return 'summon'
    if sd.con > 0 and not slot_inserting(sd.id):
        return 'debuff'
    if sd.con > 0:
        return 'self_buff'
    if sd.hp > 0 and not caster_hp(sd.id):
        return 'target_heal'
    if sd.hp > 0:
        return 'self_heal'
    return 'attack'


def skill_def(skill_id, catalog=None):
    """The SkillDef of an EN Type-3 record, or None (unknown id, id > 4248, not a skill)."""
    catalog = EC.items() if catalog is None else catalog
    if not catalog.exists(skill_id):
        return None
    item = catalog.get(skill_id)
    if item is None or item.type != TYPE_SKILL:
        return None
    return SkillDef(item)


def all_skills(catalog=None):
    """{id: SkillDef} for every Type-3 record of the EN table."""
    catalog = EC.items() if catalog is None else catalog
    return {i: SkillDef(d) for i, d in catalog.defs.items()
            if d.type == TYPE_SKILL and 1 <= i <= SKILL_MAX_ID}


def learned_in_family(learned, first, span=PASSIVE_FAMILY_SPAN):
    """FUN_00426c50(first) with EDI = span: the lowest id in [first, first + span - 1] that
    is in the learned list, or 0. The client scans its u16[30] list up to the first 0."""
    ids = []
    for s in list(learned or [])[:MAX_LEARNED]:
        try:
            s = int(s.get('skill_id') if isinstance(s, dict) else s) & 0xFFFF
        except (TypeError, ValueError, AttributeError):
            continue
        if s == 0:
            break
        ids.append(s)
    for k in range(span):
        if first + k in ids:
            return first + k
    return 0


# ------------------------------------------------------------ learned skills ---
# cs-skill-learn (combat_skill.md F1 / 3.2). `char['skills']` is the persisted learned list:
# EXACT ids (the family's current level, passives included), in the client's slot order, at
# most MAX_LEARNED. It is what the 0x07 skill_count list sends (records.skill_rows), so a
# skill the server does not store here is gone after the next portal or relog - the live
# bug where quest 26's Double Jump (94) vanished on relog.
Learned = namedtuple('Learned', 'ok id replaced why')


def _skill_id(value):
    try:
        value = int(value.get('skill_id') if isinstance(value, dict) else value)
    except (TypeError, ValueError, AttributeError):
        return 0
    return value if 1 <= value <= SKILL_MAX_ID else 0


def ensure(char):
    """Normalize (and create) char['skills'] in place; the store migration and every learn
    run it. Structural only - ints in 1..4248, no 0 (the client's list scan stops at the
    first 0, FUN_00426c50), no exact duplicate, at most MAX_LEARNED - so a server started
    without the EN hii (no Skill_Lv column) never drops a real skill. A conforming list is
    left untouched (same object: callers may hold it)."""
    if not isinstance(char, dict):
        raise TypeError('character record must be a dict')
    raw = char.get('skills')
    if not isinstance(raw, list):
        raw = char['skills'] = []
    clean, seen = [], set()
    for value in list(raw):
        sid = _skill_id(value)
        if sid and sid not in seen and len(clean) < MAX_LEARNED:
            seen.add(sid)
            clean.append(sid)
    if clean != raw:
        raw[:] = clean
    return raw


def learned(char):
    """The learned ids, normalized (a read of char['skills'])."""
    return list(ensure(char)) if isinstance(char, dict) else []


def learn_slot(ids, sd, catalog=None):
    """FUN_00426b80 (called by S2C 0x18 / 0x57 / 0x27 type 3 and the shop's own check):
    where the client puts `sd` in its u16[30] learned list. Returns (index, replaced_id):

        (i, old)      the family is learned at a LOWER level: slot i is overwritten
        (len, None)   the family is new and a slot is free: appended
        (None, why)   nothing changes client-side (Skill_Lv 0, family already at >= this
                      level, or all 30 slots used)

    The family match is the client's: an entry e with record r is the same family when
    e - |r.Skill_Lv| == id - |Skill_Lv|, scanned over all 30 entries before a free slot."""
    if sd is None:
        return None, 'not a skill record'
    if sd.skill_lv == 0:
        return None, 'a family base record (Skill_Lv 0) is never learned'
    ids = list(ids or [])
    for i, entry in enumerate(ids[:MAX_LEARNED]):
        other = skill_def(entry, catalog)
        if other is not None and other.family == sd.family:
            if entry < sd.id:
                return i, entry
            return None, (f'already learned ({entry} >= {sd.id})' if entry != sd.id
                          else 'already learned')
    if len(ids) >= MAX_LEARNED:
        return None, f'all {MAX_LEARNED} skill slots are used'
    return len(ids), None


def learn_refusal(char, sd, level, catalog=None):
    """Why a validated learn (skill-master shop, GM/dev grant) must be refused, or None
    (combat_skill.md F1 step 3; the same gates the client's FUN_00467680 applies before it
    sends C2S 0x0B, re-checked because the client filters only what it displays)."""
    if sd is None:
        return 'not an EN skill record'
    if sd.skill_lv == 0:
        return 'a family base record (Skill_Lv 0) cannot be learned'
    if sd.lv > int(level or 0):
        return f'needs level {sd.lv} (character is {level})'
    job1 = (char or {}).get('class', 0)
    if not sd.allows_job(job1):
        return f'class {job1} may not learn it (Job flags {list(sd.job)})'
    _slot, why = learn_slot(learned(char), sd, catalog)
    if _slot is None:
        return why
    return None


def learn(char, skill_id, *, level=None, check=True, catalog=None):
    """Put `skill_id` into char['skills'] exactly as the client's FUN_00426b80 puts it into
    its own list (a lower level of the family is replaced in place, a new family appended).

    check=True  (shop buy, dev !learn): the level/class gates of learn_refusal as well.
    check=False (quest reward, !give): mirror a grant the CLIENT has already applied on its
                own - S2C 0x27/0x18 type 3 learns with no level or class check (FUN_00440920
                case 3), so the server must record whatever the client recorded.

    Returns Learned(ok, id, replaced_id, why). The caller persists (store.mark_dirty) and
    sends the packets; nothing here touches the network."""
    sd = skill_def(skill_id, catalog)
    if sd is None:
        return Learned(False, _skill_id(skill_id), None, 'not an EN skill record')
    ids = ensure(char)
    if level is None:
        # The level is derived from exp everywhere (records.level_of); nothing stores one.
        level = progression.level_for_exp(int(char.get('exp') or 0))
    if check:
        why = learn_refusal(char, sd, level, catalog)
        if why is not None:
            return Learned(False, sd.id, None, why)
    slot, info = learn_slot(ids, sd, catalog)
    if slot is None:
        return Learned(False, sd.id, None, info)
    if slot < len(ids):
        ids[slot] = sd.id
        return Learned(True, sd.id, info, '')
    ids.append(sd.id)
    return Learned(True, sd.id, None, '')


def unlearn(char, skill_id):
    """Remove an exact id from char['skills'] (dev aid). The client keeps its own copy
    until the next map load rebuilds the list from the 0x07 record."""
    ids = ensure(char)
    sid = _skill_id(skill_id)
    if sid in ids:
        ids.remove(sid)
        return True
    return False
