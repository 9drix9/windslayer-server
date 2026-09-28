# Combat and skill system: server design (EN 2008 client)

Group `combat_skill` (systems `combat` + `skill`). Specs: `re_tools/corpus/systems/combat_skill.json` (16 specs).

| Dir | Key | Name | Spec status | Confidence |
|---|---|---|---|---|
| C2S | `0x44C239/0x15` | UseSkill (UseItemOrSkill, Type-3 branch) | partial | high |
| C2S | `0x4484CC/0x2E` | DeathReviveRequest | missing | medium |
| C2S | `0x417DB0/0x6C` | TrapTriggered | missing | high |
| S2C | `0x25` | UseItemOrSkillConfirm | partial | high |
| S2C | `0x28` | SetLocalHp | implemented | high |
| S2C | `0x29` | EntityDie | implemented (wrong values) | high |
| S2C | `0x3B` | ApplyBuffSkill | missing | high |
| S2C | `0x3C` | BuffRemove | missing | high |
| S2C | `0x3D` | LocalRecoverHpMp | missing | high |
| S2C | `0x3E` | LocalPlayerDeath (action 0xD + death dialog 0x79) | missing | medium |
| S2C | `0x40` | EntityRecoverHpMp | partial (builder only) | high |
| S2C | `0x43` | BuffRemoveRecalc | missing | high |
| S2C | `0x44` | SetLocalMp | implemented | high |
| S2C | `0x57` | EntitySkillLearned | missing | high |
| S2C | `0x5F` | SkillRequestUnlock | missing | high |
| S2C | `0x92` | PartySkillHpEffect | missing | medium |

Sources, in order of authority: spec binary evidence plus the Ghidra decomp in `corpus/decomp/` (checked for this doc: FUN_004034c0, FUN_00403fa0, FUN_0044c090, FUN_0043d8b0, FUN_00443ac0, FUN_00426cc0, FUN_00426b80, FUN_00440920, FUN_00413920, FUN_00417e10, FUN_00418ac0, FUN_0041a230, FUN_00412360, FUN_0041ace0, FUN_004257a0, FUN_00423260, FUN_00458640, OnReceive_MainTcpHandler); `LIVE_TEST_LOG.md`; `server/windslayer_server.py` (2793 lines, line numbers as of 2026-09-17); `server/wsproto.py`; memory notes; PySlayer. Cross-group specs used: C2S 0x0B/0x0D/0x25(trade)/0x38(battlefield)/0x15(item), S2C 0x03/0x06/0x07/0x08/0x18/0x1A/0x1B/0x21/0x2A/0x41/0x42/0x54/0x55. Sibling design docs referenced by plan id: `world_movement_npc.md` (world-*), `item_inventory.md` (I-*), `shop_storage.md` (SS-*), `login_character.md` (lc-*), `party.md` (party-*), `pvp_arena.md` (pvp-*).

---

## 0. Key findings

1. **A skill is an item-table record** (`windslayer.hii`, Type `rec+0x154` = 3) addressed by a u16 id. KR `gamedef.sqlite3 items` ids 1..4248 are the same records as the EN catalog (EN names confirm every family checked: 292 Increase Attack Power, 501 Self Heal, 2609 Booby Trap, 2776 Group Heal, 2875 Mutation). Every skill packet carries only the id; cost, cooldown, duration and animation come from the client's own record.
2. **Every C2S 0x15 skill cast needs exactly one reply**: S2C 0x25 (accept), S2C 0x3B with `target_uid` = caster (accept a slot-inserting buff; it runs the 0x25 self logic too), or S2C 0x5F (reject). Without one, `scene+0x258` stays set: no further casts and "You can't equip or unequip while attacking." on every equip until a map load. **Our server never answers a skill cast** (B1).
3. **EN has no attack or hit-list packet.** C2S 0x25 is TradeFinalConfirm and C2S 0x38 is BattlefieldInfoRequest (T key); our server routes both to damage code (B2, B3). The client does run its own hit math (FUN_00418ac0) but in field mode skips the HP write (decomp line 847: `+0x70 != 1 && scene+0xF24 == 0 → goto` past the `+0x9C -=`). Field combat is server-authoritative.
4. **New: in field mode the client runs no buff timers, no DoT/aura ticks and no HP/MP regeneration.** All of that sits in FUN_00417e10 behind `scene+0xF24 != 0` (room host only, decomp lines 97-209). The server owns buff expiry (0x3C/0x43), poison/aura ticks, delayed detonations (Time Bomb) and natural regen (0x28/0x44). Today the only HP recovery besides potions is the accidental full heal on map change (B9).
5. **Monster death is wrong today.** 0x29 is sent with `respawn_tick=0, x=0, y=0`. FUN_00413920 case 0x16 (decomp 962-983) revives any type-4 entity (all 0x1A monsters) once `+0xE5C - clock < 0`, at (+0x1318,+0x1320) = (0,0), with full HP and cleared buffs (B4). The respawn ticker only runs when a T press or trade confirm arrives (B5), and the respawn 0x1A duplicates the entity (B6).
6. **Our local player is a type-3 entity** (0x07 forces `+0x98=3`). Type 3 never self-revives in field mode, so local death must be S2C 0x3E (death dialog 0x79), then C2S 0x2E, then a server warp. Re-sending 0x07 duplicates the entity; 0x06 on your own uid leaves `scene+0x970` dangling.
7. **Learned skills vanish on every portal**: 0x07 hard-codes `buff_count=0`/`skill_count=0` and 0x03/0x08 destroy all entities first (B7).
8. **The spec codec cannot evaluate hex literals in conditions** (`wsproto._eval`), so the 6-byte trap variant of C2S 0x15 does not decode, 0x07 trap-buff entries encode 4 bytes short, and C2S 0x0D tails (which carry the trap victim uid and the death action) never parse (B17; fix owned by `world-codec-hexfix`).
9. **The trap victim is on the wire after all**: 0x6C carries only the trap id, but the owner's very next C2S 0x0D has `interact_event=0xC` and its tail carries `target_uid` (= victim, player+0xD78) and `item_id` (= trap id, player+0x8D4).

---

## 1. How the client implements combat and skills

### 1.1 The skill record: hii offsets and gamedef columns (verified from the loader)

FUN_004034c0 tokenizes each hii line into a 0x228-byte record whose base is stack `local_64c`, so `local_X` sits at `rec + (0x64C - X)`. FUN_00403fa0 then post-processes it.

| hii offset | Loader token (local) | gamedef `items` column | Meaning / client consumers |
|---|---|---|---|
| +0x000 | 2 | `name` | name string (0x41 bytes) |
| +0x041 | 0x36 | `Text` | description (0x101 bytes) |
| +0x142 | FUN_00403fa0 | (derived) | **family max level**: 10 for every Type-3 record except the single-level ones, which get 1: 0x50 Dash, 0x52 Mining, 0x56 Herb Gathering, 0x58 Strong Attack, 0x5A Guard, 0x5E Double Jump, 0xC2 Open Stall, 0x88C Reinforce, 0xAD6 Fairy Attack, 0xC2C Beast Attack (the switch runs on the table size right after the push, i.e. the id). FUN_00443ac0 uses it to upgrade quick-slot ids |
| +0x144 | FUN_00403fa0 | (idx) | table size after insert = the record's own id |
| +0x154 | 4 (`local_4f8`) | `Type` | 0 consumable, 1 equip, 2 etc, **3 skill**, 4 class change, 5 cash-use |
| +0x158 | 8 | `HP` | signed HP value: caster cost / self heal, or effect magnitude on the target when FUN_004258d0(id)=0 (Poison −3, Heal +40, Poison Cloud −15 …) |
| +0x15C | 10 | `MP` | signed MP value (skills: cost). Passive Improve Mana Recovery 496..499 has +34..+46 |
| +0x160 / +0x164 | 0x5D / 0x5F | `mHP` / `mMP` | max HP/MP bonus while active (Breath of Vitality 0xAEE: mHP 50). `rec+0x160 != 0` makes 0x25 refresh the HP and party bars |
| +0x168..+0x180 | 0x0C..0x12 | `Job` (7 flags) | [0] traveler, [1] warrior, [2] martial artist, [3] archer, [4] thief, [5] mage, [6] priest |
| +0x188 / +0x18C | 0x54 / 0x55 | `Job2` | second-class branch flags |
| +0x190 | 0x14 | `Attribute` | element (server damage) |
| +0x1A0 / +0x1A4 | 0x1C / 0x1E | `Attri_Atk` / `Attri_Def` | element power (server damage) |
| +0x1B8..+0x1C4 | 0x28..0x2E | `Skill_P_A`, `Skill_P_D`, `Skill_A_A`, `Skill_A_D` | stat modifiers while a slot is active (0x3B/0x41/0x43 recompute gate FUN_0041ace0). On Con=0 attack skills (Double Attack 0x76A: 15) they are the skill's damage bonus |
| +0x1C8 | 0x30 (Type 0/3) | `Con` | slot duration, ms (0 = instant). 0x3B/0x41 insert gate |
| +0x1CC | 0x30 (Type 1/5) | `Kind` | equip slot kind (11 = weapon) |
| +0x1D8 | 0x38 | `Lv` | required character level |
| +0x1DC | 0x32 (Type 3) | `Skill_Lv` | level inside the family: 1..10 castable; 0 = family base/teaching record; negative = passive (cast gate is `> 0` signed; family math uses `abs`) |
| +0x1E0 / +0x1E4 / +0x1E8 | 0x3A / 0x3C / 0x3E | `Buy` / `Sell` / `PMoney` | shop gold, sell price, Victy price |
| +0x1EC | 0x40 | `CT` | cooldown, ms |
| +0x1F0 | 0x46 | `Cash` | cash flag |

Skill families come in 11-id blocks: a base record (`Skill_Lv 0`, e.g. 291 Increase Attack Power) and Lv1..10 (292..301). `family_base = id - abs(Skill_Lv)`. The client's hard-coded ranges are written `[base+1, base+11]`, so each range also covers the next family's base record; that is harmless because base records fail the cast gate. Gamedef counts: 1198 Type-3 rows, 116 base records, 67 passives (negative level: Dash 80, Mining 82, Herb Gathering 86, Strong Attack 88, Guard 90, Double Jump 94, Improve Mana Recovery 496..499), 430 castable slot skills (`Con>0`, `Skill_Lv>0`), 12 Type-3 rows beyond the EN catalog (ids > 4248, never use).

### 1.2 Client range tables the server must mirror

| Function | Ranges | Effect |
|---|---|---|
| FUN_004257a0 → 0 ("targeted", no 0x3B insert) | 0x187-0x191 Poison, 0x1B3-0x1BD Stun, 0x242-0x24C Threaten, 0x78B-0x795 Arrow Grapple, 0x7B7-0x7C1 Thornbush, 0x8BB-0x8C5 Weapon Shackle, 0x981-0x98B Cartilage Smash, 0x997-0x9A1 Poison Arrow, 0xA05-0xA0F Forced Blindfold, 0xA3C-0xA46 Puppet, 0xA47-0xA51 Time Bomb, 0xA52-0xA5C Spider Web, 0xA5D-0xA67 Poison Cloud, 0xB0F-0xB19 Strike of Darkness, 0xB30-0xB3A Frenzy, 0xB3B-0xB45 Mutation | 0x3B reads no x,y and inserts nothing; use 0x41 on the target |
| FUN_004258a0 → 1 (0x3B insert even with Con 0) | 0x9CE-0x9D8, 0x9E4-0x9EE, 0x9EF-0x9F9 (summons; 0x9C3-0x9F9 forced to 60000 ms) | summon slot |
| FUN_004258d0 → 0 (HP value is NOT applied to the caster) | 0x187-0x191, 0x1BE-0x1C8 Heal, 0x24D-0x257 Remote Heal, 0x8C6-0x8D0 Berserk, 0x8F2-0x8FC Healing Aura, 0x997-0x9A1, 0x9C3-0x9F9, 0xA5D-0xA67, 0xAF9-0xB03 Breath of Life, 0xB0F-0xB19, 0xB25-0xB2F Vampiric Attack | server: `caster_hp_delta = HP if FUN_004258d0(id) else 0` |
| FUN_00424e20 party return ≠ 0 | 0x8E7-0x912 auras (Advance/Healing/Support/Movement), 0xAE3-0xAF8 group buffs (Group Protection, Breath of Vitality) | caster's client fans the slot out to its party list `scene+0x25C..+0x26C` |
| 0x25 special ids | 0xAD8-0xAE2 Group Heal (effect/sound 0x10E on self + party, HP added to each), 0x1F5-0x1FF Self Heal (effects 0x54/0x37), 0xC2 Open Stall (UI 0x259), 0x876-0x880 / 0x881-0x88B / 0x88C crafting UI helpers, 0xAD6-0xAD7 / 0xC2C-0xC2D variant from buff slots | |
| Host-only ticks in FUN_00417e10 (dead in field mode) | DoT every 990 ms: 0x187-0x191, 0x997-0x9A1, 0x9C3-0x9CD, 0xA5D-0xA67, 0xB0F-0xB19 (caster uid ≠ holder); Berserk 0x8C6-0x8D0 self-damage every 990 ms; Healing Aura 0x8F2-0x8FC heal every 5010 ms; FUN_004256c0 detonation at exactly 0 ms for 0x981-0x98B/0xA47-0xA51 | server must emulate |
| Trap (C2S 0x15 x,y; 0x3B x,y; FUN_00416380) | 0xA31-0xA3A Booby Trap Lv1-10 (0xA3B = Puppet base, never castable) | |

### 1.3 Client state the server has to mirror

| Client state | Location | Set / cleared by |
|---|---|---|
| Pending-skill lock | `scene+0x258` u16 | set right after C2S 0x15 (0x44C248); cleared by S2C 0x25 type 3 (0x453D89), S2C 0x3B to the local uid, S2C 0x5F (0x45779A), map load FUN_0042c360/FUN_0042c450 |
| Cast-animation lock | `scene+0x255 = 7`, `+0x256` = variant | set by 0x25 type 3 by id range; blocks C2S 0x15 while 7. **Not cleared by any TCP packet**: FUN_00423260 sends a UDP state datagram `[03][uid][0x255*10+0x254][0x256*10+0x257]` to 127.0.0.1:`scene+0x244` (own P2P port) and FUN_00458640 clears 0x255/0x256 when the echoed local action is 0x17..0x21; map load also clears it. Live-verify (T-CASTLOCK) |
| Learned skills | local entity `+0x29A` u16[30]; cooldown stamps `+0x7B4` u32[30] | 0x07 `skill_count` list, S2C 0x18 Type 3 (FUN_00440920 case 3 → FUN_00426b80, no level/class check), S2C 0x57 |
| Quick-slot last use | `game_state+0x11C0[16]` | FUN_00443ac0 (called by the 0x25 type-3 tail) copies `+0x7B4[k]` for the slot's family; also rewrites the slot id to the learned level |
| Buff slots | entity `+0xE84`, 21 × 0x18: +0 id, +2 abs(Skill_Lv), +4 rec+0x142, +6 x, +8 y, +0xC remaining ms, +0x10 rec+0x158, +0x14 source uid | insert: 0x07/0x05 buff list, 0x1A effect list, 0x3B, 0x41/0x42; remove: 0x3C/0x43, death memset (case 0xD, type-4 revive). **No countdown in field mode** |
| HP/MP | `+0x9C`/`+0xA0`; max `+0x110C`/`+0x1110` computed by the client (FUN_0041ace0 → FUN_00427d40/FUN_00427f40) | 0x07 cur fields, 0x28, 0x44, 0x3D, 0x40 (local uid only), 0x25/0x92 deltas, 0x54/0x55 (also max) |
| Weapon class | `+0x112` = highest `i` with `Job[i] != 0` on the equipped Kind-11 weapon (FUN_0041ace0 ~line 410) | used by the traveler-weapon cast gate |
| Entity type | `+0x98`: 3 = player record (0x07/0x04/0x05, **including our local player**), 4 = template entity (0x1A) | fixed at spawn |
| Action | `+0x904`: 8 idle, 0x10 dying, 0x16 dead; `+0x94C = 0xD` = die now | 0x29, 0x3E, FUN_0041a230 |
| Respawn deadline | `+0xE5C` vs scene clock `scene+0xF1C` (+30 per 30 ms tick, seeded by 0x03 `game_clock_ms` = 1000 on our server, 0x08 `game_time_ms`) | 0x29 `respawn_tick` |
| Death dialog | UI window 0x79 | opened only by S2C 0x3E; closed by S2C 0x08 when the old map is not PvP |

### 1.4 Cast pipeline on the client

1. Triggers: quick-slot key 1..8 (FUN_0043d8b0: `scene+0xF00 == 6`, no stall, no modal, `game_state+0xD84 == 0`), skill window (window 5, FUN_0046cad0, Type 3 only), inventory activation.
2. FUN_0044c090 gates, in order: `Skill_Lv > 0`; local entity exists; `scene+0x258 == 0`; `scene+0x255 != 7`; `entity+0x15B4 ∈ {6, 8, 0xC}`; if `Job[0] == 0 && entity+0x112 == 0` → "You can't use the skill with elementary traveler weapon." (class skills need a class weapon); `cur MP >= |MP|` else "MP low."; for 0x124-0x12E and 0x8DC-0x8E6 `cur HP > |HP|` else "Not enough HP."; `CT + last_use <= now` else silent.
3. Sends C2S 0x15 `u16 id` (+ `u16 x, u16 y` = entity `+0x15A4/+0x15A8` for 0xA31..0xA3B), then `scene+0x258 = id`.
4. The cooldown is real once a cast is confirmed: the 0x25 tail calls FUN_00426cc0 (stamps `+0x7B4[k] = now` if the id is learned and CT has passed; otherwise it returns 0 and **the handler ignores that**, so cost and animation still apply) and FUN_00443ac0 (copies the stamp into the quick slot). Before the first confirmation `last_use` is 0. The server still enforces CT itself.
5. Replies: **0x25** type 3 → animation variant, clear `+0x258`, cooldown, quick-slot refresh; common tail adds `rec+0x15C` to MP and `rec+0x158` to HP (only if FUN_004258d0), clamps, skips both when map `+0x70 == 1`. **0x3B** → insert slot if `(Con != 0 || FUN_004258a0) && FUN_004257a0`, stat recompute, effects; if target == local uid also the whole 0x25 self logic. **0x5F** → only clears `+0x258`.

### 1.5 Damage, HP and regen in field mode

- Basic attack sends no packet (live log line 29: holding S next to a Pupu produced only 0x0D). Skill casts carry no target. The dev server learns swings from client memory (`_combat_driver` :1737-1813: `+0x8B8 == 1 && +0x15B4 == 4`, position `+0x11F8/+0x1288`).
- FUN_00418ac0 (client hit math) accumulates damage into display counters `+0x1114..+0x1128` (floating numbers via FUN_0042fd20) but writes `+0x9C` only in PvP rooms or as room host. The server owns every HP value.
- Monsters have no HP bar (party frames 0x75..0x78 only); monster feedback is the swing animation plus 0x29 on death.
- **No client regen in field mode**: FUN_00417e10 adds `+0x10C` MP every 15 s and `+0x108` HP every 15 s while idle, only when `scene+0xF24 != 0`. The server must push 0x28/0x44.
- **No client buff countdown in field mode** (same gate): slot `+0xC` stays at Con forever, DoTs never tick, Time Bomb never detonates, auras never heal. Buff icons stay until 0x3C/0x43.

### 1.6 Death on the client

- **Monsters (type 4).** 0x29 → action 0x10 → 0x16. Case 0x16 revives when `+0xE5C - scene+0xF1C < 0`: pos = `+0x1318/+0x1320`, action 8, HP/MP = max, slots cleared. `+0x1318` is also the leash home (FUN_00417e10, |dx| > 600).
- **Local player (type 3, field).** Case 0x16 does nothing unless map `+0x70 == 1` or `scene+0xF24 != 0`. It stays dead until a map load.
- **Client-detected death.** When hit reaction (action 2/0x11) ends with `+0x9C < 1`, FUN_0041a230 sets `+0x1388=1`, `+0x94C=0xD` and, for levels 10..98 without "Waive EXP Penalty" (item 1891 = 0x763 in the `+0x1364` list, FUN_00426d10), subtracts `min(exp into current level (FUN_00427d10), round(float term))` from `+0x112C`, so it never de-levels. It opens **no dialog**, so `0x28 hp=0` can leave a dead player with no way out.
- **S2C 0x3E** sets `+0x94C=0xD` and opens window 0x79. Case 0xD (FUN_00412360:603-614): action 0x10, `+0xE5C = clock + +0xE58`, HP 0, both buff tables memset. No exp change on this path. Any control on 0x79 → **C2S 0x2E** (0 bytes, the choice is not sent).
- C2S 0x0D sends immediately whenever `+0x94C != 0` or `+0x950 != 0`, so death (nibble 0xD) and trap catches (interact 0xC) reach the server in the next movement packet.

---

## 2. Request / response flows

Notation: `C2S 0xNN {fields}` → server logic → `S2C 0xMM {fields}`. Local uid = `session['account_id']` (always 1 today, :2656; per-account uids come with `world-uid-alloc`). `in_world` means the 0x07 for this session was sent and no 0x03/0x08 has been sent since.

### F1. Learn a skill (skill-master NPC shop; buy handler owned by shop SS-4)

1. Client (FUN_00467680) checks gold, `Lv <= level`, `Job[job1] || Job[0]`, family slot (FUN_00426b80: "You already have learned this skill."). Any failure → no packet.
2. **C2S 0x0B NpcShopBuy** `{item_id=skill_id, qty=1, npc_id}`.
3. SS-4 sees `sd.type == 3` and calls `skills.learn(session, skill_id)`:
   - refuse if `sd.skill_lv <= 0 and not passive`, `sd.lv > level`, class flag missing, NPC does not sell it, family already at ≥ this level, 30 families known, or gold short → S2C 0x16 chat reason; nothing else (gold unchanged, no resync needed).
   - accept: drop lower levels of the family from `char['skills']`, append the id, deduct gold, persist.
4. **S2C 0x18 GetItem** `{gold=<new absolute>, victy, item_id=skill_id, count=1}` → "You've learned skill(%s).", skill window and quick-slot refresh. **Do not** add it to the bag model (B8).
5. Observers on the same map: **S2C 0x57** `{uid=learner, skill_item_id}` (6 B, encode with `assume={'entity_with_uid_in_scene': True}`).
- GM/dev grant (`/learn <id>` or starting skills): same as step 3-5 without the gold/NPC checks.

### F2. Enter world / map change: restore skills, buffs, HP/MP

1. 0x07 local record (with `world-player-record`): `buff_count` + `{u16 id, i32 remaining_ms, [u16 x, u16 y if 0xA31..0xA3B]}` for each unexpired `session['buffs']` entry on this player (≤ 21; needs `world-codec-hexfix` for the x,y), then `skill_count` + u16 ids from `char['skills']` (≤ 30), then `cur_hp`/`cur_mp` = session values.
2. Traps are not restored (the owner slot comes back via the buff list only if the server keeps it; policy: drop traps on map change and forget them server-side).
3. S2C 0x28 `{hp=session hp}`, S2C 0x44 `{mp=session mp}` — current, not max (B9).
4. `session['in_world'] = True` only after 0x07; set False before any 0x03/0x08. Suppress 0x25-type-3, 0x3B-to-self, 0x3E and 0x5F while False (null-deref hazards).

### F3. Cast a skill (general)

1. **C2S 0x15** `{skill_id[, pos_x, pos_y]}`. Decode: `id = u16`; if `0xA31 <= id <= 0xA3B` and 6 bytes: `x, y`. (Grammar `0x44C239/0x15` after world-codec-hexfix.)
2. `sd = skill_def(id)` (§3.3). No row / id > 4248 → **S2C 0x5F**. `sd.type == 0` → item group (I-09, F9). `sd.type == 3` → continue. Anything else → ignore (client never sends it).
3. Validation; **any failure → S2C 0x5F** (optionally S2C 0x16 chat "Skill not ready."):
   1. `in_world` and not `dead`.
   2. `sd.skill_lv > 0`.
   3. `id in char['skills']` (exact id).
   4. class: `sd.job[job1] or sd.job[0]`; weapon: if `sd.job[0] == 0`, the equipped weapon (Kind 11) must have some `Job[i] != 0` with `i > 0` (mirror of `entity+0x112`).
   5. `char level >= sd.lv`.
   6. `mp >= abs(sd.mp)` (when `sd.mp < 0`); for 0x124-0x12E / 0x8DC-0x8E6 also `hp > abs(sd.hp)`.
   7. cooldown: `now_ms - last_use[family] >= sd.ct - 150` (tolerance for latency).
   8. not in a PvP room map (pvp group owns rooms).
4. Accept: `last_use[family] = now`; `mp = clamp(mp + min(sd.mp, 0))`; if `FUN_004258d0(id)`: `hp = clamp(hp + sd.hp)`.
5. Branch on `sd.kind` (§3.3): F3a..F3j.
6. Finish with **S2C 0x28** `{hp}` and **S2C 0x44** `{mp}` **after** the 0x25/0x3B/0x92, so the server's absolute value wins over the client's delta. Skip a packet whose value the client already has.

#### F3a. Instant attack (`Con == 0`, not special): Ice Spear 0x104, Wind Cutter 0x13A, Heaven Strike 0x8DC, Double Attack 0x76A …
1. **S2C 0x25** `{item_or_skill_id=id}` → animation, unlock, cooldown, MP/HP cost client-side.
2. Server: `targets = monsters_in_box(pos, facing, SKILL_HITBOX[family])` from the caster's last known position/facing (driver memory `+0x11F8/+0x1288/+0x8BD` today; `world-position-tracking` later). For each: `mob.hp -= skill_damage(sd, char, mob)`; `hp <= 0` → F6. No target → cast still succeeds.
3. F3 step 6.

#### F3b. Self heal (`HP > 0`, `Con == 0`, FUN_004258d0 = 1): Self Heal 0x1F5..0x1FE
1. **S2C 0x25** `{id}` → client adds +HP (Self Heal Lv1 +20) and plays 0x54/0x37.
2. Server `hp = min(max_hp, hp + sd.hp)` (done in F3 step 4); 0x28/0x44.
3. Observers: **S2C 0x40** `{uid=caster, has_hp=1, has_mp=0, hp}` once. Never 0x3D to the caster (double effect).

#### F3c. Target heal (`HP > 0`, FUN_004258d0 = 0): Heal 0x1BE, Remote Heal 0x24D, Breath of Life 0xAF9
1. **S2C 0x25** `{id}` to the caster (cost only; the client adds no HP to itself).
2. Target: no target on the wire (Q4). Interim: lowest-HP% same-map party member within 400 px, else the caster.
3. Target session: `hp += sd.hp` → **S2C 0x3D** `{has_hp=1, has_mp=0, hp}` (heal effect 0x37 + sound). Observers: **S2C 0x40** `{uid=target, 1, 0, hp}`; party frames: **S2C 0x54** `{uid, max_hp, cur_hp}` (party-vitals-sync).

#### F3d. Self buff (`Con > 0`, FUN_004257a0 = 1, not party/trap): Increase Attack Power 0x124, Increase Defense 0x12F, Enhance Concentration 0x145, Sprint 0x192, Magic Shield 0x1D4, Berserk 0x8C6 …
1. **S2C 0x3B** `{item_or_skill_id=id, target_uid=caster}` (6 B). **No 0x25.** Client: slot with Con, stat recompute (Skill_P_A..), effect/sound (0x124-0x12E → 0x36), self logic (cost, cooldown, unlock).
2. Server: `buffs[(caster, family)] = {id, expires=now+Con, source=caster}` (refresh replaces; never two slots of one family); apply modifiers in damage/defense.
3. Berserk 0x8C6: server ticks −HP every 990 ms while active (host-only on the client), 0x28.
4. Expiry → F5 (0x43). Observers: same 0x3B.

#### F3e. Party heal (Group Heal 0xAD8..0xAE1) — needs `party-skill-hooks`
1. **S2C 0x25** `{id}` to the caster (client: effect 0x10E on self and each `scene+0x25C..` member, +HP to each, +HP to self in the tail).
2. Each other same-map member's client: **S2C 0x92** `{skill_id=id}` (heals that client's party slots and itself).
3. Server: `hp += sd.hp` (clamped) for caster and each same-map member; then per member **S2C 0x28** (own HP) and **S2C 0x54** `{uid, max_hp, cur_hp}` to the others (frames).

#### F3f. Aura / group buff (0x8E7-0x912, 0xAE3-0xAF8) — needs `party-skill-hooks`
1. **S2C 0x3B** `{id, target_uid=caster}` to the caster; its client fans the slot out to party members (FUN_00424e20 ≠ 0).
2. Same 0x3B to each same-map member client (their client fans out the same way; Q5).
3. Healing Aura 0x8F2..0x8FB: server tick every 5010 ms for `Con` (21 s): `hp += sd.hp` per affected member → 0x28 (+0x54 frames). Support/Advance auras: modifiers only.
4. Expiry / leaving party: **S2C 0x43** `{id, caster}` to the same clients (FUN_00425af0 re-evaluates auras, HUD refresh).

#### F3g. Ground trap (Booby Trap 0xA31..0xA3A)
1. C2S 0x15 carries `pos_x, pos_y` (caster `+0x15A4/+0x15A8`, low 16 bits).
2. **S2C 0x3B** `{id, target_uid=caster, ground_x=pos_x, ground_y=pos_y}` (10 B; encode with `assume={<exact 0x3B condition string>: True}` or raw bytes). Client: slot in the caster's table, `+0xC = Con` (7020), armed until it fires or is removed.
3. Server: `traps[caster].append({id, x, y, expires: now + Con})` (max one per family; a recast replaces it: send 0x3C for the old one first).
4. Trigger → F7. Expiry without trigger → **S2C 0x3C** `{id, caster}` (removes slot, event 0xE2 at x,y).

#### F3h. Targeted debuff (FUN_004257a0 = 0, `Con > 0`): Poison 0x187, Stun 0x1B3, Threaten 0x242, Cartilage Smash 0x981, Poison Arrow 0x997, Time Bomb 0xA47, Spider Web 0xA52, Poison Cloud 0xA5D, Strike of Darkness 0xB0F, Frenzy 0xB30, Mutation 0xB3B …
1. **S2C 0x25** `{id}` to the caster (0x3B would insert nothing for these ids).
2. Target: nearest alive monster in `SKILL_HITBOX[family]` in front of the caster; none → cast still succeeds.
3. Show it: **S2C 0x41 ItemBuffApply** `{target_uid=mob, source_uid=caster, item_id=id}` to every client that sees the mob (0x41 inserts whenever `Con != 0`, with the caster uid at slot +0x14). Owned builder: item group; verify T-41-DEBUFF.
4. Server effect state `debuffs[mob]`:
   - DoT ids (0x187, 0x997, 0x9C3.., 0xA5D, 0xB0F families): every 990 ms apply `abs(sd.hp)` (scaled, §3.5) to the mob → F6 on death.
   - Delayed strike (0x981 Cartilage Smash, 0xA47 Time Bomb): at `Con` apply the damage and send **S2C 0x3C** `{id, mob}` — the 0x3C handler runs FUN_004256c0 (type-9 hit reaction from the caster found via slot +0x14).
   - Control (Stun, Threaten, Spider Web, Weapon Shackle, Arrow Grapple, Mutation): server-side AI suppression once monster AI exists (`world-monster-ai-1b`).
5. Expiry: **S2C 0x3C** `{id, mob}` (or 0x43 if the record has stat modifiers).

#### F3i. Summons (0x9C3-0x9F9) and utility skills
- Summons: **S2C 0x3B** `{id, caster}` (FUN_004258a0 ids insert a 60 s slot even with Con 0). Pet behaviour untraced (Q9); interim reply only.
- 0xC2 Open Stall: **S2C 0x25** `{id}` opens UI 0x259; shop group owns the stall. 0x876..0x88C Mineral Refining / Concoction / Reinforce: **S2C 0x25** `{id}` opens the crafting UI; item group owns C2S 0x67/0x68/0x69 (I-*).

#### F3j. Passives (`Skill_Lv < 0`)
- Never cast (client gate). Server applies their effect in stat/regen formulas (Improve Mana Recovery MP column as regen bonus, Dash/Double Jump are movement unlocks; Q10).

### F4. Reject or cancel a cast
- **S2C 0x5F** (0 B) for every F3 step-3 failure, unknown ids, and while dead.
- Never before the scene exists (no null check on `[game_state+0x4CC]`): guard with `in_world`.
- Harmless when nothing is pending, so a watchdog may send it if a cast handler throws.

### F5. Buff and effect expiry (server ticker)
1. One scheduler per map (heap of `(expires, key)`, 50 ms resolution; client durations are multiples of 30 ms).
2. Expired stat/aura/mHP slot (any of Skill_P_A..Skill_A_D, mHP, mMP non-zero, or id in 0x8E7-0x912/0xAE3-0xAF8) → **S2C 0x43** `{buff_item_id, target_uid}` to the holder's client and observers. If mHP/mMP was non-zero, follow with 0x28/0x44 (clamp; the 0x43 recompute gate only checks +0x1B8..+0x1C4).
3. Other slots (traps, debuffs without modifiers, summons) → **S2C 0x3C** `{buff_item_id, target_uid}`.
4. Never create two identical adjacent slots (refresh instead): one removal packet per slot is then enough.
5. Holder died (F8) or map changed: clear server state; send nothing (the client memsets on death / destroys on map load).

### F6. Monster death and respawn (with `world-respawn-dedup`)
1. `mob.hp <= 0` under `combat_lock` → `alive = False` (exactly once).
2. **S2C 0x29 EntityDie** `{uid=mob.uid, respawn_tick=0x7FFFFFFF, respawn_x=int(spawn_x), respawn_y=int(spawn_y)}`.
3. Clear `debuffs[mob]`; remove trap references to it.
4. **S2C 0x21** `{exp_delta=+mob.exp}` (lc-exp-persist `grant_exp`), then **S2C 0x18** gold/drop (existing `_kill_monster` :1911-1928; drop ids filtered against the EN catalog, I-01).
5. ~3 s later (corpse animation): **S2C 0x06** `{uid=mob.uid}`.
6. At `MOB_RESPAWN_SECS`, from a periodic ticker (not a C2S handler): `hp = max_hp, alive = True`, **S2C 0x1A** same uid at spawn.
7. Multiplayer: 2, 5, 6 go to every session on the map (`world-shared-monsters`).

### F7. Trap triggered (C2S 0x6C + follow-up 0x0D)
1. Owner client tick (FUN_00416380): first eligible entity overlapping `GetColRect(sprite 0xE1, x, y, 0x1E, 2)` → `+0x950=0xC`, links uids (`owner+0xD78 = victim`), `victim+0x958=600`, `owner+0x8D4 = trap id`, slot `+0xC = 0`, sends **C2S 0x6C** `{trap_skill_id}` (2 B).
2. Same tick path: `+0x950 != 0` forces an immediate **C2S 0x0D** whose tail is present (`interact_event=0xC`): `target_uid` (victim), `pos_x/pos_y` (owner), `target_dx/dy`, …, `item_id` (trap id). Expected length 63 B (18 + 43 + 2).
3. Server: find `trap = traps[session]` with `id == trap_skill_id` and not expired. None → ignore (stale/duplicate/forged).
4. Victim: `target_uid` from the 0x0D that arrives within 500 ms with `interact_event == 0xC` and `item_id == trap id`; validate it is an alive monster within ~120 px of `(trap.x, trap.y)`. If no 0x0D arrives (or the tail cannot be parsed), fall back to the nearest alive monster within ±40 × ±30 px of the trap point.
5. Damage `skill_damage(sd, char, mob)` → F6 if dead. Remove the trap; **S2C 0x3C** `{trap_skill_id, owner_uid}` (slot removal + event 0xE2 at x,y).
6. Reachability: the owner must be a type-3 entity with uid == `scene+0x220` in a non-room map. Our 0x07-spawned player qualifies (T0/T-6C confirm).

### F8. Local player death and revive
1. Damage source (monster AI `world-monster-ai-1b`, DoT, PvP rules) calls `damage_player(session, amount, attacker)` under `combat_lock`: `hp = max(0, hp - amount)`; `hp > 0` → **S2C 0x28** `{hp}`. **Never send `0x28 hp=0`.**
2. `hp == 0`: `dead = True`; clear `buffs`, `traps`, cast cooldown lock state; cancel DoTs on the player.
3. If `in_world`: **S2C 0x3E** (0 B) → death animation, HP 0, slots cleared, window 0x79.
4. Exp penalty (policy, mirrors FUN_0041a230): level 10..98, no active "Waive EXP Penalty" (1891): `penalty = min(exp_into_level, PENALTY(level))`; **S2C 0x21** `{exp_delta=-penalty}` ("Lost (%d) EXP.", never de-levels because of the min). No 0x22.
5. Observers: **S2C 0x29** `{uid, 0x7FFFFFFF, x, y}`. The dying client gets only 0x3E.
6. The client's next C2S 0x0D carries action nibble 0xD (`state_lo` bits 12-15); log it as confirmation. A 0x0D with nibble 0xD while the server still has `hp > 0` means a client-side death (desync) → run steps 2-5 without 0x3E and immediately F8 step 8.
7. Player clicks any control on window 0x79 → **C2S 0x2E** (0 B).
8. Server: ignore if `not dead` or `reviving` (repeated clicks). `reviving = True`; target = `REVIVE_POINT[current_map]` (e.g. 102 → 101 at (1411, 714), the `102_26` portal target); `hp = max_hp`, `mp = max_mp` (policy).
9. Warp with `world-maptransfer`: **S2C 0x08** `{map_code, game_time_ms}` (closes 0x79 because the old map is not PvP), **0x03**, **0x07** (skills, no buffs, cur HP/MP), **0x28**, **0x44**, **0x1A** × monsters. Then `dead = reviving = False`.
- Refusals: C2S 0x2E while alive → ignore. PvP rooms → pvp group ("Revival in 20 seconds." relay path).

### F9. Consumables (owned by item group I-09; 0x25 semantics defined here)
1. **C2S 0x15** `{item_id}` (Type 0; no client lock).
2. Server checks bag qty and cooldown (`CT`), decrements, computes HP/MP.
3. **S2C 0x25** `{item_or_skill_id=item_id}` → client removes 1 from its bag (FUN_00423b70), stamps cooldown, applies `rec+0x158/+0x15C`, effect/sound 0x29. If the client bag lacks the item or is cooling down, the whole handler aborts (no heal).
4. **S2C 0x28/0x44** absolutes after 0x25. No 0x23/0x19 for the used unit.
5. Items with `Con` (buff potions): S2C 0x42 to self / 0x41 to observers (item group); expiry via F5.
6. Observers: **S2C 0x40** `{uid, has_hp, has_mp, hp, mp}` once per use.

### F10. Observer sync (multiplayer; `world-registry`)
Broadcast to other in-world sessions on the map, never back to the source: buff applied 0x3B / debuff 0x41; removed 0x43/0x3C; heal 0x40 (visual only, remote HP not written); skill learned 0x57; death 0x29; party HP 0x54/0x55. Cast animations reach others only through the P2P state or a relay (movement group, `cs-cast-anim-relay`).

### F11. Natural regen and received heals
1. Per-session ticker every 15 s (client constant): if alive and in world, `mp += regen_mp`; if idle (no damage taken or cast in the last 5 s) `hp += regen_hp`. Send 0x28/0x44 only on change. Values: port `+0x108/+0x10C` from FUN_0041ace0 (Q7); placeholder `max(1, max/30)`.
2. Heals received from others: **S2C 0x3D** `{has_hp, has_mp, hp, mp}` to the target (effect 0x37 on the HP branch; never per tick) plus 0x40 to observers. Regen uses plain 0x28/0x44 (no visuals).

---

## 3. Server state and data model

### 3.1 Session fields

| Field | Type | Purpose |
|---|---|---|
| `in_world` | bool | set after 0x07; cleared before 0x03/0x08. Guards 0x25-type-3, 0x3B-self, 0x3E, 0x5F |
| `hp`, `mp`, `max_hp`, `max_mp` | int | authoritative; max from the client formula (§3.5) |
| `dead`, `reviving` | bool | F8 |
| `skill_last_use` | {family_base: ms} | server CT |
| `buffs` | {(holder_uid, family): {id, expires, source, x, y}} | F3d/F3f/F3i, F5, 0x07 buff list |
| `traps` | [{id, x, y, expires}] | F3g/F7 |
| `pending_trap` | {id, t} | F7 step 4 (pairing with the next 0x0D) |
| `last_combat_ms` | int | regen idle check |
| `pos` | (x, y, facing, t) | written by the combat driver (`+0x11F8/+0x1288/+0x8BD`) or 0x0D tracking; today `_memory_melee` receives x,y as arguments and discards them, and `session['x']` is never set |
| `combat_lock` | threading.RLock | shared by the connection thread, combat driver thread and tickers (B18) |
| `monsters` | {uid: Monster} + `debuffs` per mob | existing; moves to per-map state with `world-shared-monsters` |

### 3.2 Persistent character fields (accounts.json → lc-data-model)

`skills` (list of u16, ≤ 30 families, exact level ids; passives included), `hp`, `mp` (current), `exp`, `level`, `class` (job1), `job2`, `map`, `x`, `y`. Buffs are session-only (retail unknown; they do survive map changes through the 0x07 list).

### 3.3 `skill_def(id)` and classification

```sql
SELECT idx, Type, HP, MP, mHP, mMP, Job, Job2, Attribute, Attri_Atk, Attri_Def,
       Skill_P_A, Skill_P_D, Skill_A_A, Skill_A_D, Lv, Buy, PMoney, CT, Con, Skill_Lv
FROM items WHERE idx = ? AND idx <= 4248
```
(extend or replace `_gamedef_item` :49-65, which reads only Type/Kind/Sprite/Spr_Num/HP/MP/Buy/Sell/W_Att/Def/name). Derived: `job = [int(t) for t in Job.split()]`, `family = idx - abs(Skill_Lv)`, `passive = Skill_Lv < 0`, `castable = Skill_Lv > 0`, `con = Con or 0`, `stat_mod = any(Skill_P_A, Skill_P_D, Skill_A_A, Skill_A_D, mHP, mMP)`, plus `caster_hp = FUN_004258d0(idx)`, `slot_on_3b = (con != 0 or FUN_004258a0(idx)) and FUN_004257a0(idx)`.

`kind`, first match wins (ranges from §1.2):
1. `passive` if `Skill_Lv < 0`; `base` if `Skill_Lv == 0`.
2. `trap`: 0xA31-0xA3A.
3. `party_heal`: 0xAD8-0xAE1.
4. `aura`: 0x8E7-0x912, 0xAE3-0xAF8.
5. `utility`: 0xC2, 0x876-0x88C.
6. `summon`: 0x9C3-0x9F9.
7. `debuff`: `con > 0` and FUN_004257a0 = 0.
8. `self_buff`: `con > 0`.
9. `target_heal`: `HP > 0` and not `caster_hp`.
10. `self_heal`: `HP > 0`.
11. `attack`.

Keep `SKILL_KIND_OVERRIDE = {family: kind}` for content fixes, and unit-test the classifier against the families named in §1.2.

### 3.4 Content tables to author (not in gamedef)

- `SKILL_HITBOX[family]` = (front px, back px, half-height px). Default 150/0/60; wide skills (Heaven Strike 0x8DB, Crescent Slash 0x8AF) 300/300/120. Better: sprite collision rects from `.hsi` (asset format cracked).
- `REVIVE_POINT[map] = (town_map, x, y)`, from `portals.json` / gamedef `maps` (Popola region → 101 (1411, 714)).
- `SKILL_SHOP[npc_id]` = skill ids per skill master (shop SS-4; EN text "Learn from: …").
- `STARTING_SKILLS[job1]` (Q11; PySlayer gives 80, 82, 86, 88, 90, 94, 194, 2175, 2186, 2188).
- `DOT_IDS`, `DETONATE_IDS`, `AURA_HEAL_IDS` (from §1.2).

### 3.5 Formulas

- **Max HP/MP**: port FUN_00427d40 / FUN_00427f40 (class tables, Tol/Int, equipment bonus). Interim: read the live client `+0x110C/+0x1110` in dev (driver) and store it; the level-1 client shows 110 while the server assumes 100.
- **Regen**: port the `+0x108/+0x10C` computation in FUN_0041ace0; placeholder `max(1, max_hp // 30)` / `max(1, max_mp // 20)` per 15 s.
- **Skill damage (placeholder)**: `atk = base_str + weapon W_Att + buff Skill_P_A`; `dmg = max(1, int(atk * (1 + (Skill_P_A_skill + Attri_Atk) / 100)) - mob.defense)`; DoT tick `max(1, abs(HP) * (1 + level/20))`.
- **Death penalty**: `min(exp_into_level, PENALTY(level))`; the float term in FUN_0041a230 needs asm (Q12); placeholder 5 % of the level span.

### 3.6 Building packets from the grammars

```python
from wsproto import Grammar
_SPECS = {s['key']: s for s in json.load(open(SPEC, encoding='utf-8'))['specs']}
_G = {}
def pkt(key, rec, assume=None):
    g = _G.setdefault(key, Grammar(_SPECS[key]['grammar'] or ''))
    return g.encode(rec, assume=assume)
def parse(key, payload):
    g = _G.setdefault(key, Grammar(_SPECS[key]['grammar'] or ''))
    return g.decode(payload, allow_trailing=True)
```
Share `party-spec-codec` / `lc-codec` if one lands first. Pitfalls (checked with `Grammar.encode` on this group):
- `0x29`, `0x3C`, `0x43`, `0x40`: client-state `stop` conditions encode as False, so every field is written. Correct.
- `0x57`: `if(entity_with_uid_in_scene)` → False drops `skill_item_id` (encodes 4 B). Pass `assume={'entity_with_uid_in_scene': True}` (6 B).
- `0x3B` trap ids: pass `assume={<the full condition string>: True}` or ground x,y are dropped (6 B instead of 10 B). Non-trap ids: no assume.
- `0x44C239/0x15`, `0x07` buff list, C2S 0x0D tails: **broken until world-codec-hexfix** (B17). With `allow_trailing=True` the trap x,y are silently lost.
- `wsdev.py sendspec` passes no `assume` → use raw `send` for 0x57 and trap 0x3B (until `pvp-wsproto-fixes` adds `--assume`).

---

## 4. Current implementation status and proven bugs

### 4.1 Status per opcode (windslayer_server.py)

| Opcode | Status |
|---|---|
| C2S 0x15 (skill) | `_dispatch` :624-626 → `_handle_use_item` :1160; returns at :1187-1191 for `type != 0` → **no reply** |
| C2S 0x2E | no branch; "Unhandled opcode" :659-667 |
| C2S 0x6C | no branch; unhandled |
| S2C 0x25 | `_handle_use_skill` :1458-1465 builds the right `<H` body but has no caller; never sent |
| S2C 0x28 / 0x44 | `_send_hp` :1615 / `_send_mp` :1619 correct; used at :781-782, :1229-1231, :1314-1315 |
| S2C 0x29 | `_build_opcode_29` :1986 / `_send_death` :1990: 16 B layout right, values wrong |
| S2C 0x40 | `_build_opcode_40` :1943-1970 wire-correct, never sent, docstring wrong |
| S2C 0x3B, 0x3C, 0x3D, 0x3E, 0x43, 0x57, 0x5F, 0x92 | no builder, never sent |
| C2S 0x25 / C2S 0x38 | misrouted to `_handle_attack` :1844 / `_handle_melee` :1634 |
| combat driver | `_combat_driver` :1737-1813, `_memory_melee` :1678-1698, `_resolve_hit` :1889-1909, `_kill_monster` :1911-1928, `_tick_respawns` :2023-2033 |

### 4.2 Bugs

| # | Severity | Opcode | Bug | Evidence | Fix |
|---|---|---|---|---|---|
| B1 | crash_or_desync | C2S 0x15 | A skill cast gets no reply. `scene+0x258` stays set: no more casts and every equip/unequip fails with "You can't equip or unequip while attacking." until a map load | spec `0x44C239/0x15` gates HAZARD; S2C 0x5F semantics; server :1187-1191 (`itype != 0 → return`); `data/items.json` has type 3 for 292/501/2188/2609 | interim: reply S2C 0x5F for Type 3 (cs-cast-unlock); then F3 |
| B2 | wrong_behavior | C2S 0x38 | 0x38 is BattlefieldInfoRequest (T key) but dispatches to `_handle_melee`, which damages the nearest monster within 130×90 px of the **account's saved x,y** (`session['x']` is never set, :1640-1641) and runs the respawn tick | spec `0x446628/0x38` hazard 4; LIVE_TEST_LOG rows :21 and bug 1 (:32); server :648-652, :1634-1653 | remove branch; hand to pvp group; delete `_handle_melee` |
| B3 | wrong_behavior | C2S 0x25 | 0x25 is TradeFinalConfirm (u64 gold + item lists) but dispatches to `_handle_attack`, which reads gold as `skill_id` and each item id as a target uid; `_resolve_hit` matches on the low 16 bits and mob uids are 0xF0000+i, so confirming a trade holding item ids 0..7 (e.g. 5 Herb, 7 Minor Healing Potion) damages monster #id | spec `0x469BA8/0x25` SERVER HAZARD; server :644-647, :1856-1881, :1893-1896 | remove branch (trade group owns 0x25); delete `_handle_attack` |
| B4 | wrong_behavior | S2C 0x29 | Monster death sends `respawn_tick=0, x=0, y=0`; the client revives the type-4 corpse at (0,0) with full HP right after the death animation and sets its leash home to (0,0) | spec 0x29 hazards; decomp FUN_00413920:962-983 (`+0x98 == 4`, `+0xE5C - clock < 0` → pos = `+0x1318/+0x1320`, HP = max); server :1914, :1986-1992; 0x03 seeds the clock at 1000 (:2043) so 0 is always in the past | `0x7FFFFFFF` + spawn x,y; 0x06 despawn (F6) |
| B5 | wrong_behavior | S2C 0x1A | `_tick_respawns` is only called from `_handle_melee` :1638 and `_handle_attack` :1847. Real kills go driver → `_memory_melee` → `_resolve_hit`, so killed monsters never respawn unless the player presses T or confirms a trade | call-site grep; LIVE_TEST_LOG :29 (kills via driver) | periodic ticker (F6 step 6) |
| B6 | wrong_behavior | S2C 0x1A | The respawn re-sends 0x1A for a uid whose entity still exists; the handler never de-duplicates, so a second entity appears (next to the B4 zombie) | spec 0x1A gates ("never looks for an existing entity with the same uid"); server :2030-2033 | S2C 0x06 before re-spawn (world-respawn-dedup) |
| B7 | wrong_behavior | S2C 0x07 | `buff_count` and `skill_count` hard-coded 0 (labelled "N1 inventory count"/"N2 skill count", :2364-2365). 0x03/0x08 destroy every entity, so skills learned via 0x18 vanish after any portal, and active buffs are lost | spec 0x07 grammar; spec 0x03 semantics (FUN_00422f40 destroys all entities); server :2364-2365, :1298-1304 | emit `char['skills']` + active buffs (F2) |
| B8 | wrong_behavior | C2S 0x0B / S2C 0x18 | Buying a Type-3 skill book runs `_inv_add` :1024, creating a phantom bag item; no learned-skill record and no level/class/duplicate/NPC-stock validation | spec 0x18 (Type 3 = learn); spec 0x0B gates; server :1002-1027 | SS-4 type-3 branch → F1 |
| B9 | wrong_behavior | S2C 0x28/0x44 | Map change full-heals (sends `max_hp/max_mp`, :1312-1315). `max_hp` is the stored char `hp` (100, :778-780) while the client computes 110 at level 1, so every server clamp is below the client max | server :777-783, :1310-1315; reference_combat_render.md "HP 0/110" | send current values; port max formula; add regen (F11) in the same change |
| B10 | wrong_behavior | S2C 0x25 | Consumables never get S2C 0x25: client bag count never drops, cooldown/effect never start (owned by item group B10/I-09) | spec 0x25 references; `0x44C2B3/0x15` expected_response; server :1160-1231 | F9 |
| B11 | cosmetic | — | `_handle_use_skill` :1458-1465 is dead code built on a non-existent inbound 0x25 UseSkill; `_send_hit_feedback` :1972-1984 is also uncalled | spec 0x25 references; grep | delete in cs-cleanup |
| B12 | cosmetic | S2C 0x40 | `_build_opcode_40` docstring (:1944-1962) and reference_combat_model.md call 0x40 a "got-hit react"; the binary plays heal effects (0x37 HP, 0x54 always, 0x58 MP on the **local** sprite) and writes HP only for the local uid | spec 0x40 semantics; effect ids equal the Self Heal visuals at 0x453CAA | fix docstring and memory note; damage uses 0x28 |
| B13 | cosmetic | S2C 0x29 | `_build_opcode_29(uid, anim, dx, dz)` misnames `respawn_tick/respawn_x/respawn_y`, which caused B4 | spec 0x29 references; server :1986-1992 | build from the grammar |
| B14 | wrong_behavior | (driver) | `_memory_melee` applies swings read from the one attached client (uid==1 walk :1773-1784) to the monsters of **every** session | server :1678-1698 | per-pid attach (party-dep-combat-multiclient) |
| B15 | cosmetic | S2C 0x21/0x22 | `_send_exp` :2010-2013 sends 0x22 on every level change: level-up effect plays twice (0x21 already levels client-side) and a negative death penalty would play a level-*up* effect | spec 0x21 hazards | lc-exp-persist (`grant_exp`, no owner 0x22) |
| B16 | cosmetic | S2C 0x1A | The monster spawn's "idle action" entry `u16 0xB3B, i32 0` (:1574-1576) is an effect-slot entry for **Mutation Lv1** (씨요변이, Skill_Lv 1, in FUN_004257a0's excluded range). Any slot clear (type-4 revive memset, 0x3C/0x43 with 0xB3B) removes it; live notes say the body is invisible without it | spec 0x1A references (d); gamedef idx 2875; en catalog 2875 "Mutatiion" | T-3C-B3B; `world-1a-defaults` decides |
| B17 | crash_or_desync | C2S 0x15, S2C 0x07, C2S 0x0D | `wsproto._eval` (:175-191) extracts identifiers with `[A-Za-z_]\w*`, so a literal like `0x0A31` yields the name `x0A31`, which is not in env → `ClientStateCondition` → False. Consequences: C2S 0x15 `31 0A B0 04 BC 02` raises "4 trailing byte(s)" (or loses x,y with `allow_trailing`); 0x07 buff entries for 0xA31..0xA3B encode without their 4 bytes (desyncs the rest of the record); C2S 0x0D `& 0xF` tails never decode (trap victim, death action) | reproduced: `_eval('skill_id >= 0x0A31', {'skill_id': 2609}, {})` raises; `Grammar(0x15 grammar).decode(bytes.fromhex('310ab004bc02'))` → GrammarError | strip hex literals before the name check (world-codec-hexfix) |
| B18 | wrong_behavior | 0x29/0x21/0x18 | No locking between the combat-driver thread and connection threads. `_memory_melee` iterates `mons.values()` (:1686) while `_spawn_map_monsters` fills the new dict (:1823-1841) → RuntimeError, swallowed by the driver's `except` (:1811-1813), which also drops the process handle; `_handle_melee` (connection thread) and the driver can both pass `mob.alive` in `_resolve_hit` (:1897-1909) and double-send 0x29/0x21/0x18 | code; threads started at :417 and :424 | `session['combat_lock']` around monster/HP mutation (cs-combat-lock) |
| B19 | wrong_behavior | — | No server regen, while the client does not regenerate in field mode: after B9 is fixed HP would never recover except via potions or level-up | decomp FUN_00417e10 regen block gated by `scene+0xF24 != 0` (lines 97-130); no regen code in server | F11 (cs-regen) |

---

## 5. Implementation plan

| Id | Title | Pri | Effort | Depends on | Opcodes | MP | UDP |
|---|---|---|---|---|---|---|---|
| cs-dispatch-fix | Remove C2S 0x38→melee and C2S 0x25→attack routing; add logged C2S 0x2E and 0x6C branches | P0 | S | — | 0x38, 0x25, 0x2E, 0x6C | no | no |
| cs-cast-unlock | Interim: Type-3 C2S 0x15 → S2C 0x5F (guarded by `in_world`) | P0 | S | — | 0x15, 0x5F | no | no |
| cs-monster-death | 0x29 with 0x7FFFFFFF + spawn x,y; periodic respawn ticker off the driver/timer; exactly-once kill; pairs with world-respawn-dedup (0x06 + 0x1A) | P0 | S | world-respawn-dedup | 0x29, 0x06, 0x1A, 0x21, 0x18 | no | no |
| cs-combat-lock | `combat_lock` RLock for monsters/hp/mp/buffs used by driver, handlers, tickers | P1 | S | — | — | no | no |
| cs-skill-defs | `skill_def()` + range helpers (§1.2) + classifier (§3.3) + unit tests | P1 | S | — | — | no | no |
| cs-hp-mp-model | Authoritative hp/mp/max (client formula), current values in 0x07/0x28/0x44, no map-change full heal, `in_world` flag | P1 | M | world-player-record, lc-data-model | 0x28, 0x44, 0x07 | no | no |
| cs-regen | 15 s regen ticker (idle HP, always MP), 0x28/0x44 on change | P1 | S | cs-hp-mp-model | 0x28, 0x44 | no | no |
| cs-skill-learn | `learn()` model, `char['skills']`, S2C 0x18 learn, 0x07 skill list, GM `/learn`, starting skills; SS-4 calls it | P1 | M | cs-skill-defs, world-player-record, SS-4 | 0x0B, 0x18, 0x07 | no | no |
| cs-skill-cast | `_handle_cast_skill`: decode, validation chain, server CT, costs, 0x25 / 0x3B-self / 0x5F, trailing 0x28/0x44 | P1 | M | cs-skill-defs, cs-hp-mp-model, cs-skill-learn, cs-dispatch-fix, world-codec-hexfix | 0x15, 0x25, 0x3B, 0x5F, 0x28, 0x44 | no | no |
| cs-buffs | Buff state, expiry scheduler (0x43/0x3C), 0x07 buff list, stat modifiers in damage/defense, Berserk tick | P1 | M | cs-skill-cast, cs-combat-lock | 0x3B, 0x43, 0x3C, 0x07, 0x28 | no | no |
| cs-skill-damage | Caster position/facing store, `SKILL_HITBOX`, `skill_damage()`, attack skills (F3a) → F6 | P1 | L | cs-skill-cast, cs-monster-death, cs-combat-lock | 0x15, 0x25, 0x29 | no | no |
| cs-player-death | `damage_player()`, 0x3E, penalty 0x21, 0x0D nibble-0xD watch, C2S 0x2E → `REVIVE_POINT` warp | P1 | M | cs-hp-mp-model, cs-dispatch-fix, world-maptransfer, world-codec-hexfix, lc-exp-persist | 0x3E, 0x2E, 0x0D, 0x21, 0x29, 0x08, 0x03, 0x07, 0x28, 0x44 | no | no |
| cs-heal-visuals | Self/target heal skills (F3b/F3c) with 0x3D to the healed player and 0x40 to observers | P2 | S | cs-skill-cast | 0x25, 0x3D, 0x40 | no | no |
| cs-traps | Booby Trap: 0x3B with echoed x,y, trap state, C2S 0x6C + 0x0D victim pairing, damage, 0x3C consume/expiry | P2 | M | cs-buffs, cs-skill-damage, world-codec-hexfix | 0x15, 0x3B, 0x6C, 0x0D, 0x3C | no | no |
| cs-debuffs | Targeted debuffs: target pick, 0x41 on the mob, DoT 990 ms ticks, Time Bomb/Cartilage Smash detonation via 0x3C, control effects hook | P2 | L | cs-buffs, cs-skill-damage, I-* 0x41 builder | 0x25, 0x41, 0x3C, 0x43, 0x29 | no | no |
| cs-party-skills | Group Heal (0x25 + 0x92 + 0x28/0x54), aura fan-out 0x3B/0x43, Healing Aura 5010 ms ticks | P2 | M | cs-buffs, party-skill-hooks, party-vitals-sync | 0x25, 0x92, 0x3B, 0x43, 0x28, 0x54 | yes | no |
| cs-observer-sync | Broadcast 0x3B/0x41/0x3C/0x43/0x40/0x29/0x57 to same-map sessions (never the source) | P3 | S | world-registry, world-uid-alloc, cs-buffs | 0x3B, 0x41, 0x3C, 0x43, 0x40, 0x29, 0x57 | yes | no |
| cs-utility-skills | 0xC2 Open Stall and 0x876..0x88C crafting skills: 0x25 + hand-off; summons 0x3B stub | P3 | S | cs-skill-cast | 0x25, 0x3B | no | no |
| cs-cast-anim-relay | Other players see cast animations (P2P state or server relay via 0x1B/0x2A) | P3 | M | cs-observer-sync, world-move-relay | 0x1B, 0x2A | yes | yes |
| cs-cleanup | Delete `_handle_use_skill`, `_handle_attack`, `_handle_melee`, `_send_hit_feedback`; fix 0x40/0x29 docstrings and reference_combat_model.md | P3 | S | cs-dispatch-fix | 0x40, 0x29 | no | no |

Order: cs-dispatch-fix → cs-cast-unlock → cs-monster-death (P0, about an hour each) → cs-combat-lock → cs-skill-defs → cs-hp-mp-model + cs-regen (same change) → cs-skill-learn → cs-skill-cast → cs-buffs → cs-player-death → cs-skill-damage → P2/P3.

---

## 6. Live test plan

Harness (run from `WindSlayer2Game/server`): `python wsdev.py up` (TestHero, class 0, map 101) or `python wsdev.py up --char 1` (drix, class 1). Map 102: walk onto the portal sparkle on the map-101 ledge (x≈1404, Down), per LIVE_TEST_LOG; Pupu uids 0xF0000..0xF0007 = 983040..983047 at `MAP_SPAWNS` (:131-136). `sendspec` builds from the grammar; `send` takes raw hex; injection reaches every live session. **One experiment per restart** (feedback_experiments.md). Log every result in LIVE_TEST_LOG.md.

Memory peek used below (PEEK):
```
python -c "import wsview as V,struct;p,_=V._open_inworld();r=V._reader(p);u=lambda a,n=4,f='<I':struct.unpack(f,r(a,n))[0];s=u(V.SCENE_PTR);e=u(s+0x970);print('258=%d 255=%d hp=%d mp=%d slot0=%d rem=%d' % (u(s+0x258,2,'<H'),r(s+0x255,1)[0],u(e+0x9C,4,'<i'),u(e+0xA0,4,'<i'),u(e+0xE84,2,'<H'),u(e+0xE90,4,'<i')))"
```

| Test | Opcode | How | Expected | Risk |
|---|---|---|---|---|
| T0 | — | `python wsview.py state` in world | TestHero row `alive` = **3** (decides F7 reachability and no local auto-revive) | safe |
| T-15-LOCK | C2S 0x15 | TestHero. (a) `python wsdev.py sendspec 18 '{"gold":100000,"victy":0,"item_id":2188,"count":1}'` (Reinforce: Job[0], Skill_Lv 1, MP 0, CT 0). (b) Open skill window (`key k`, `shot`), drag Reinforce to quick slot 1 by hand. (c) `python wsdev.py cap 3 key 1`. (d) PEEK. (e) `cap 3 key 1` again. (f) double-click a bag item to equip | (a) "You've learned skill(Reinforce)." (c) `C2S 0x15 payload=2B 8C 08`, log `[USE] item=2188 type=3 not consumable - ignoring`, no S2C. (d) `258=2188`. (e) no packet. (f) "You can't equip or unequip while attacking." Proves B1 | state_change |
| T-5F | S2C 0x5F | right after T-15-LOCK: `python wsdev.py send 5F`; PEEK; `cap 3 key 1` | `258=0`; a new C2S 0x15 is sent; equipping works again | safe |
| T-25-SKILL | S2C 0x25 | after a fresh lock: `python wsdev.py sendspec 25 '{"item_or_skill_id":2188}'`; PEEK | `258=0`, Reinforce UI opens (FUN_004687f0), quick slot shows the cooldown state. Crash risk if sent outside the world | state_change |
| T-CASTLOCK | S2C 0x25 | drix, in world, nothing pending: `sendspec 25 '{"item_or_skill_id":260}'` (Ice Spear Lv1, variant 1); PEEK at once and again after 3 s | cast animation, MP −6 on the globe; first PEEK `255=7`. After 3 s `255=0` means the UDP state loopback clears it (casting works); `255=7` still means any accepted variant cast blocks all casting until a map load → record and open a P1 item | state_change |
| T-25-ITEM | S2C 0x25 | `sendspec 18 '{"gold":100000,"victy":0,"item_id":5,"count":3}'` (Herb); `sendspec 28 '{"hp":30}'`; `sendspec 25 '{"item_or_skill_id":5}'` | Herb 3→2, HP 30→50, effect/sound 0x29. With no Herb in the client bag: nothing | state_change |
| T-28 | S2C 0x28 | `sendspec 28 '{"hp":10}'` | globe `10/<max>` + low-HP indicator; record `<max>` for B9 | state_change |
| T-44 | S2C 0x44 | `sendspec 44 '{"mp":0}'`; PEEK. Later, in T-CAST-E2E, press the MP-cost skill once with MP 0 | globe `0/<max>`, `mp=0`; the MP-cost cast shows "MP low." and `cap` shows no C2S 0x15 | safe |
| T-REGEN | S2C 0x28 | `sendspec 28 '{"hp":10}'`, `sendspec 44 '{"mp":1}'`, stand idle 60 s, PEEK | `hp=10 mp=1` unchanged → confirms no client regen in field mode (B19) | state_change |
| T-3B-SELF | S2C 0x3B | drix: `sendspec 3B '{"item_or_skill_id":292,"target_uid":1}'` (6 B); PEEK; `shot` | buff icon, effect 0x36 + sound, stat window attack changes, HP −5 MP −5, `slot0=292 rem=30000`, `258=0` | state_change |
| T-BUFF-NOEXPIRE | — | after T-3B-SELF wait 40 s; PEEK; `shot` | `slot0=292 rem=30000` still, icon and attack bonus remain → client never expires field buffs (server must send 0x43) | safe |
| T-43 | S2C 0x43 | then `sendspec 43 '{"buff_item_id":292,"target_uid":1}'`; PEEK | icon gone, stat window reverts, `slot0=0` | safe |
| T-3C | S2C 0x3C | repeat T-3B-SELF, then `sendspec 3C '{"buff_item_id":292,"target_uid":1}'` | icon gone, stat window **keeps** the bonus (no recompute) — confirms the 0x3C/0x43 split | safe |
| T-3B-REMOTE | S2C 0x3B | map 102: `sendspec 3B '{"item_or_skill_id":292,"target_uid":983040}'` | effect 0x36 on Pupu #0; player HP/MP unchanged | safe |
| T-41-DEBUFF | S2C 0x41 | map 102: `sendspec 41 '{"target_uid":983041,"source_uid":1,"item_id":2631}'` (Time Bomb Lv1); wait 6 s; then `sendspec 3C '{"buff_item_id":2631,"target_uid":983041}'` | 0x41: slot inserted on Pupu #1 (effect if any), nothing happens at 5 s (no countdown). 0x3C: Pupu #1 plays a hit/knockdown reaction (FUN_004256c0 type 9) → validates F3h detonation | state_change |
| T-3C-B3B | S2C 0x3C | map 102: `sendspec 3C '{"buff_item_id":2875,"target_uid":983042}'`; `shot` | records whether removing the 0x1A Mutation entry makes Pupu #2 invisible or changes its sprite (B16) | state_change |
| T-3D | S2C 0x3D | `sendspec 28 '{"hp":20}'`, then `sendspec 3D '{"has_hp":1,"has_mp":1,"hp":80,"mp":40}'` (6 B) | HP 80, MP 40, one heal effect 0x37 + sound; nothing extra for MP | safe |
| T-40 | S2C 0x40 | send each once: (a) `sendspec 40 '{"uid":1,"has_hp":1,"has_mp":0,"hp":60}'`; (b) map 102 `sendspec 40 '{"uid":983040,"has_hp":1,"has_mp":0,"hp":5}'`; (c) `sendspec 40 '{"uid":983040,"has_hp":0,"has_mp":1,"mp":5}'` | (a) HP 60 + heal sparkle 0x37/0x54 (not a flinch, B12). (b) effects on the Pupu, its HP unchanged (kill still takes 3 hits). (c) MP effect 0x58 lands on the **player** | safe |
| T-29-MOB | S2C 0x29 | map 102, `wsview.py state` first. (a) `sendspec 29 '{"uid":983040,"respawn_tick":2147483647,"respawn_x":1239,"respawn_y":411}'`. (b) `sendspec 29 '{"uid":983041,"respawn_tick":0,"respawn_x":0,"respawn_y":0}'`. Wait 3 s, `state` + `shot` | (a) Pupu #0 dies and stays a corpse. (b) Pupu #1 dies then shows alive at (0,0) in `state` → confirms B4 | state_change |
| T-06 | S2C 0x06 | after T-29-MOB (a): `sendspec 06 '{"uid":983040}'`; then re-spawn it with `sendspec 1A` (fields via `sendspec 1A ?`, uid 983040, pos 1239,411, cur_hp 24) | corpse removed; `state` lists uid 0x000F0000 exactly once, alive | state_change |
| T-KILL-RESPAWN | S2C 0x29/0x1A | map 102: `hold s 450` ×3 next to a Pupu; wait 20 s; `state` | today: the dead Pupu never respawns (B5) and a (0,0) twin appears (B4). After cs-monster-death + world-respawn-dedup: corpse, 0x06, one live Pupu at spawn after 15 s | state_change |
| T-29-LOCAL | S2C 0x29 | after T0: `sendspec 29 '{"uid":1,"respawn_tick":0,"respawn_x":1411,"respawn_y":714}'` | type 3: death animation, player stays dead (no auto-revive). Recover with `wsdev.py restart` | disruptive |
| T-3E | S2C 0x3E | in world: `python wsdev.py send 3E`; immediately `python wsdev.py logs 30`; `shot` | death animation, HP 0, window 0x79 opens (record title/buttons); log should show a C2S 0x0D with `state_lo` bits 12-15 = 0xD (plus the u32 `event_source_uid` tail). If no such 0x0D appears, case 0xD cleared `+0x94C` before the packer ran and F8 step 6 detection cannot be used | disruptive |
| T-2E | C2S 0x2E | window 0x79 open: `python wsdev.py cap 3 click <button x> <y>` for each button from the T-3E screenshot | `C2S 0x2E payload=0B` for every button; today "Unhandled opcode 0x2E". After cs-player-death: warp to 101 (1411,714), window closed, HP full, skills still in K | disruptive |
| T-6C | C2S 0x6C | map 102, T0 done, drix or TestHero. `state` → live position of Pupu #2 (spawn 1494,702). `python wsdev.py send 3B 31 0A 01 00 00 00 D6 05 BE 02` (trap 0xA31 on uid 1 at x=0x05D6, y=0x02BE; adjust to the live position); `python wsdev.py logs 60` within 5 s | MP −16. If the owner gate passes: `Pkt: opcode=0x6C … payload=2B` (`31 0A`) followed by a C2S 0x0D of at least 63 B (18 + 43 + 2; +4 more if the action nibble is also set) whose interact tail starts with `02 00 0F 00` (victim uid 0xF0002) and ends `31 0A`. No 0x6C → try ±20 px or record that the spec's type-4 hazard holds | state_change |
| T-57 | S2C 0x57 | `python wsdev.py send 57 01 00 00 00 24 01` (uid 1, skill 292; not `sendspec`, §3.6); close/reopen K | no message; record whether Increase Attack Power appears in the skill list (local uid is in the scene list, so it should) | safe |
| T-92 | S2C 0x92 | `sendspec 28 '{"hp":20}'`, then `sendspec 92 '{"skill_id":2776}'` (no party) | effect + sound 0x10E on the player, HP 20→72 (+52). With a party (two clients, after party-*): member frames rise too | safe |
| T-38-REGRESS | C2S 0x38 | map 102 before cs-dispatch-fix: `cap 2 key t` | `C2S 0x38 payload=0B` then `[ATK]`/`[MOB] respawn` lines (B2). After the fix: Battlefield window only, no `[ATK]` | safe |
| T-CAST-E2E | C2S 0x15 → 0x3B/0x5F/0x43 | after cs-skill-cast + cs-buffs: drix level ≥ 12 with a class weapon (`sendspec 21 '{"exp_delta":500}'` jumps to about Lv 13 because of the 30000 exp baseline, lc B2; then 0x18 item 611 Bamboo Sword, equip it so `entity+0x112` = 1), learn 292, put it on slot 1, `cap 3 key 1` twice within 10 s, wait 31 s, portal | 1st: S2C 0x3B 6 B + 0x28/0x44, icon. 2nd: no C2S (client CT) or S2C 0x5F. After 30 s: S2C 0x43, icon gone. After the portal: 292 still in K (B7 fixed) | state_change |

---

## 7. Open questions

1. Is the local player really `+0x98 == 3` (T0)? It decides F7 reachability and the absence of local auto-revive.
2. Does the UDP P2P state loopback (FUN_00423260 → 127.0.0.1:`scene+0x244` → FUN_00458640) run on our patched client, releasing `scene+0x255 == 7` after each accepted variant cast (T-CASTLOCK)? The receive chain FUN_00478850 → FUN_00458640 is not traced.
3. How did retail learn which monsters an attack skill or basic swing hit? The client computes hits (FUN_00418ac0) but in field mode only feeds display counters; the only TCP candidates are the C2S 0x0D action/interact tails. Run `cap --all` during real casts once skills are castable and check for 0x0D packets longer than 18 B.
4. Heal-type skills with FUN_004258d0 = 0 (Heal, Remote Heal, Breath of Life, Vampiric Attack): what selects the target when no target is sent?
5. Party auras and Group Heal: one 0x3B/0x92 per member client, or caster-only? Does `scene+0x25C..+0x26C` include the local uid (double heal via 0x92)?
6. Is S2C 0x41 the retail way to show a debuff on a monster, and does 0x3C on a Time Bomb/Cartilage Smash slot play the detonation (T-41-DEBUFF)?
7. The regen amounts `+0x108/+0x10C` and max HP/MP formulas in FUN_0041ace0/FUN_00427d40/FUN_00427f40 need porting; what did retail regen per 15 s?
8. Damage formulas: skill `Skill_P_A` on attack skills, `Attri_Atk` vs monster `Attri_Def`, element `Attribute`, `Weak_Atk/Strong_Atk`; per-family hitboxes (sprite collision rects in `.hsi`).
9. Summon skills (0x9C3-0x9F9): what does the 60 s slot drive client-side, and does the pet need server entities?
10. Passive effects: Improve Mana Recovery (MP +34..+46), Strong Attack, Guard, Dash, Double Jump — which are purely client-side and which need server stats?
11. Starting skills per class and which NPC teaches what (SKILL_SHOP content).
12. Exact death penalty float term in FUN_0041a230 (needs asm around 0x41A2B0), and whether retail applied it server-side.
13. Death dialog 0x79 buttons: the "Nearest Village"/"Popola Village"/"Ozzi Village" strings (0x6F1D3C..) are table-referenced and may belong to village transfer window 0x237 instead. Should town deaths revive in place?
14. Does 0x06 on a monster uid remove a corpse cleanly (T-06), and does the 0x1A Mutation effect entry matter for rendering (T-3C-B3B)?
15. `mHP/mMP` buffs (Breath of Vitality): does 0x43 restore max HP (the recompute gate only checks +0x1B8..+0x1C4)?
16. Are KR gamedef skill rows identical to the EN hii for every family (Type/HP/MP/CT/Con/Skill_Lv/Job)? Extract `windslayer.hii` (cipher cracked) and diff before trusting costs/cooldowns server-side.
