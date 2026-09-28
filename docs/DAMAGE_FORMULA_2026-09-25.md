# Reconciled damage spec for the EN 2009 client (Build 14): FUN_004194f0 and FUN_0041b830

## 0. Verdict

The two derivations use the same formulas. They agree on every worked example they share. They differ only in how much they cover, in a few wrong statements, and in the precision model. Section 1 settles each difference.

How the settled spec was checked:
- **Asm re-read.** I re-read FUN_004194f0, FUN_0041b830, FUN_004281b0 and the hit-detection event writes in FUN_00416ab0 from the 2009 asm. I dumped every jump table from the exe bytes.
- **Independent model.** I wrote my own model from that reading: `ref_model.py` (listed at the end). It is pure Python and uses `struct` for float32, so the server can use it as is.
- **Cross-check against both derivations.** There were **0 mismatches** with either model on:
  - 20,000 random player derivations (class 0–6, level 1–99, stats 0–150, real weapons and armour), each run in both precision modes;
  - 200,000 random level-scale inputs.
- **Check against the exe.** Every value in section 7 comes from running the original exe bytes under Unicorn (derivation 1's harness), with control word 0x007F and again with 0x027F. The only exceptions are rows marked *model*.

## 1. Where the derivations differed, and how each point was settled

| # | Point | Derivation 1 | Derivation 2 | Settled |
|---|---|---|---|---|
| 1 | Skill slots that return with no damage (events 4/9) | p5=2 for classes 2 and 4; p5=5 for classes 2 and 3; p5=9 for class 6 job 1; p5=10 and 11 for class 6 job 2 | only p5=2 for classes 2 and 4 | **Derivation 1 is right.** See the tables at 0x41A9B8 and 0x41A9F8 (entries 0x419CCE), 0x419C9F→0x419D44, 0x419CC5 and 0x419D34. Emulation: class 2 with p5=2 gives digit 0. |
| 2 | Argument names | p5..p9 | arg4..arg8 | They are the same arguments. arg5 ("p6" in derivation 1) is **not a heal**. It is a record id whose `-HP` becomes B. It is used for thrown items (FUN_004185b0) and by FUN_0042e790 (summon/special skills). A record with positive HP therefore gives B<0, which is clamped to 1. An arg5 hit also skips the weapon element and the reflect step. It can never kill: after the cap, `dmg == HP` becomes `HP-1` (0x41A699). |
| 3 | Precision | Derivation at PC_53 (socket thread), damage at PC_24 | PC_24 everywhere | **Derivation 1's split is the best-supported model.** The smooth-patch measurement shows that Fireway's TCP OnReceive runs on its own socket thread and takes the frame lock there. So derivations triggered by S2C packets (0x07, 0x1A, equip, stat-up, the level-up via 0x21) run at the thread default, PC_53. The main-thread callers (FUN_00412c60 at 0x413E69/0x4140DA, FUN_0042d110 at 0x42D5C9/0x42D866) re-derive only when a buff record with Skill_P_A/P_D/A_A/A_D is applied or expires. This was not measured live. Effect: at most ±1, in 0.74% of random builds (derivation) and 0.0015% of level-scale inputs. None of the worked examples is affected. |
| 4 | Order of the share ratios and the % adds | ratios first, then % | model applies P_A/P_D before the ratios | **Ratios first** (0x41CA45), then the % adds (0x41CF30). This only matters when P_A ≠ 0. |
| 5 | Element numbering | not named | "1 Fire, 2 Water, 3 Wind, 4 Earth" | From the skill data: **1 Fire, 2 Ice/Water, 3 Earth, 4 Wind**. Stone Guard, Earthquake and Meteor are 3; Wind Cutter, Wind Arrow, Wind Bolt and Nova are 4. The math does not depend on the names. |
| 6 | Fire marker | "victim+0x95D==1 on events 4/9" | "special handling" | The check at 0x41A33A runs on every event. But +0x95D is cleared at the start of every normal hit (0x419600, when arg7==0), and only an event-9 skill record sets it again (0x419F50). In practice it only fires for **event-9 fire skills (Attribute 1)**. Attacker +0xE14/+0xE1C form a 720 ms window (FUN_00416ab0, 0x417D56..0x418243). The first victim takes full damage. Later hits in that window get **B=0 and E=trunc(E/3)**. I read this as a splash rule; that reading is inferred. The formula itself is certain. |
| 7 | p5 for plain swings | "0 for basic swings" | "the latched input key" | Both are true. The switch index is p5-1 with an unsigned `JA` (0x419808), so p5=0 falls through to "no skill term". The server never sees p5 (section 2). |
| 8 | FUN_004281b0 | "first learned id in family..family+9" | same | It returns the **lowest learned rank** (rank is the outer loop). It returns a constant 1 when gs+0xF18 is 4 or 5. FUN_00461ad0 sets 4 when it loads `preview99_01.hmi`, the character-preview scene. The field value is 6, so this case does not apply to the server. |
| 9 | Retail check | "16 needs Weak_Atk ≈ 37 at Lv11" | — | **This was an arithmetic slip.** With La=11, Lv=9, D=30: A=30 gives 16 and A=29 gives 15. |
| 10 | Novice stats | 3/2/1/3 | 3/2/2/2 | Both kinds of record exist in accounts.json. The results are identical: only STR matters for class 0 without armour. |
| 11 | Reflect constants | 0.05f / 0.3f | doubles | They are doubles that hold float-rounded values (0.05000000074505806 and 0.30000001192092896). The factor is stored with FSTP float. |

**A server bug confirmed while settling these (not a disagreement between the derivations).** `MOB_HIT_STATS[5] = None` is wrong. Event 5 is remapped to 4 (0x419549): Strong_Atk against Def + the victim's shield guard Def. Emulation: a Monkey Soldier's event 5 on TestHero gives **17**. Only events 2 and 3 do no damage.

## 2. From the C2S 0x0D report to the damage event

- **The player hits a monster.** state_lo bits 16-19 hold the attacker's +0x9E0 (FUN_00416ab0):
  - **7**: weak (basic) swing landed. The victim event is 7 (or 8 if the victim is airborne), so use **event 7**.
  - **2**: weak swing into a guard. The victim event is 2 or 3, which does **no damage**.
  - **9**: strong swing or skill. The victim event is 9 or 10, so use **event 9**.
  - **4**: strong swing or skill into a guard. The victim event is 4 or 5, so use **event 4**. For monsters this equals event 9, because their GuardDef is 0.
  - **0xC**: thrown-item path (arg5; see row 2 of section 1).
- **A monster hits the player.** state_lo bits 12-15 hold the victim's own +0x9DC (1..10), and event_source_uid is the monster.
  - Remap **5→4, 8→7, 10→9**.
  - Events **2 and 3 do 0 damage**.
- **Skill slot.** p5 is not on the wire. Resolve it from the fresh cast:
  - find the section 3.2 cell for the caster's class/job whose family `f` satisfies `f ≤ cast_id ≤ f+9`;
  - take `rec = hii[lowest learned id in f..f+9]` (normally the cast id);
  - if no cell matches, there is no skill term: it is a plain strong hit.

## 3. Damage: FUN_004194f0(victim, attacker, event, p5, arg5=0, arg6=0, arg7=0.0f, arg8=0)

Early exits with no digit: the victim or attacker is NULL; the victim's HP (+0xA0) is ≤ 0; the victim is a hidden GM (+0x12 == 1 and +0x18 != 0).

### 3.1 Physical term B

```
level_scale(A, D, La, Lv):            # La, Lv = attacker, victim level byte +0x9D
    if A == 0 or La == 0: return 0
    a = f32(A)                        # FILD / FSTP float / FLD
    t = r(a / (A + D))                # FIDIV int
    t = r(t * 2.0)                    # FMUL ST2; double 2.0 at 0x52EEE8
    t = r(t * La)                     # FIMUL
    t = r(t / (La + Lv))              # FIDIV
    return trunc(r(a * t))            # FMULP; _ftol2 0x4C1EF0 truncates toward 0
# ≈ trunc(2·A²·La / ((A+D)·(La+Lv)))   r() = the precision rounding (section 5)
```

| event (after remap) | A (attacker) | D (victim) | skill term |
|---|---|---|---|
| 6 contact | Body_Atk +0x123C | Def +0x11E0 | no |
| 1 contact into a guard | Body_Atk | Def + GuardDef +0x122C | no |
| 7 weak (also 8) | Weak_Atk +0x11D4 | Def | no |
| 9 strong or skill (also 10) | Strong_Atk +0x11D0 | Def | yes |
| 4 strong or skill into a guard (also 5) | Strong_Atk | Def + GuardDef | yes |
| 2, 3 | — | — | returns; 0 damage, no digit |

Players have Body_Atk 0, so events 1 and 6 only happen when a monster hits a player.

### 3.2 Skill term (events 4 and 9 only; switch 0x41980E, tables 0x41A978..0x41AA4C, all verified from the bytes)

1. Pick the family from the table below using (p5, class +0x118, job +0x119). If attacker +0x960 != 0, the family is 0xA31 (Booby Trap) instead.
2. **NODMG** cells return with no digit.
3. **÷3 / ÷6** cells set `B = trunc(B/3)` or `trunc(B/6)` when B ≠ 0. This happens even if the skill is not learned.
4. Look up `rec` as in section 2. If none is learned, there is no term.
5. If `1 ≤ rec.Attribute ≤ 4`: `elem[Attribute] += rec.Attri_Atk`.
6. If `rec.Skill_P_A`: `B += trunc(B·SPA/100)` (C division, truncating).

In the table, j1/j2 is the job; "else" means any other job. The weapon type each class wields comes from the weapon Job flags: 1 sword, 2 fist, 3 bow, 4 dagger, 5 staff, 6 wand.

| p5 | class 1 | class 2 | class 3 | class 4 | class 5 | class 6 |
|---|---|---|---|---|---|---|
| 1 | 0x104 Ice Spear | 0x22C Blazing Kick | 0x150 Ice Arrow | – | 0x200 Ice Bolt | – |
| 2 | 0x10F Fire Beat | NODMG | 0x15B Fire Arrow | NODMG | 0x20B Fire Bolt | – |
| 3 | 0x13A Wind Cutter | – (follow-up 0x237) | 0x166 Wind Arrow | 0x7A1 Blazing Blade | 0x216 Wind Bolt | 0x1DF Holy Strike |
| 4 | 0x76A Double Attack | 0x7D8 Flying Kick | – (follow-up 0x780) | 0x796 Shuriken | 0x7C2 Stone Guard | – |
| 5 | 0x775 Battle Charge | NODMG | NODMG | j1: 0x9FA Assassination | – | – |
| 6 | 0x8B0 Crescent Slash | j1: 0x91E Triple Kick **÷3**; j2: 0x955 | j1: 0x98C | j2: 0xA47 Time Bomb (delayed) | j1: 0xA68; j2: fairy branch | 0x7AC |
| 7 | – | j1: 0x929; j2: 0x960 | j2: 0x9CE | j1: 0xA1B | j1: 0xA73 Nova (B=0); else 0xACB | 0x7B7 |
| 8 | 0x8D1 Sonic Slash | j1: 0x93F Merciless Strike **÷6**; j2: 0x976 | j1: 0x9A2 | j1: 0xA26 | j1: 0xA7E | j1: 0xB04 |
| 9 | 0x8DC Heaven Strike | j1: 0x94A; j2: 0x981 (delayed) | j1: 0x9AD; else 0x9E4 | – | j1: 0xA89 | j1: NODMG; j2: – (Vampiric Attack) |
| 10 | – | – | j1: 0x9B8; else 0x9EF | – | j1: 0xA94 | j2: NODMG |
| 11 | – | – | – | – | – | j2: NODMG |

### 3.3 Element term E (0x419FD6..0x41A210)

```
for i in 1..4 with elem[i] != 0:                     # skill elements
    if pctA[i] (+0x1244+4(i-1)): elem[i] += trunc((X + pctA[i])·elem[i]/100)   # X=10 only in the class-5 job-2 fairy case
    z = r(r(r(kINT[c]·I) + bINT[c]) + 100.0)         # I = max(0, INT u16 +0xF4 + bonus +0x100)
    elem[i] = trunc(r(r(z·elem[i]) / 100.0))
w = attacker +0x11F0 (1..4), only when arg5 == 0:   # weapon or monster element
    if all elem == 0: elem[w] += trunc(r(EA · ratio))   # EA +0x11F4; ratio = strong share +0x11D8 for events 4/9, else weak share +0x11DC
    else:             elem[w] += EA; elem[opp(w)] = max(0, elem[opp(w)] - EA)   # opposites 1<->2, 3<->4
E = Σ_i level_scale(elem[i], R_i, La, Lv)            # R_i = victim resist +0x1200+4(i-1),
                                                     #   + victim +0x1234 if event 4 and victim +0x11F1 == i
                                                     # the whole block is skipped if La == 0
```

### 3.4 Total

```
total = B + E                          # 0x41A487
total = max(1, total)                  # 0x41A55A
victim buffs (+0xF24, 21 slots, stride 0x18):
    0x913..0x91D Holy Protection    -> 0, no digit
    0x1D4..0x1DE Magic Shield       -> the hit goes to MP (capped at MP) if MP > 0
    0x9C3..0x9F9 summons cast by the victim itself -> absorbed into the summon's negative pool
total = min(total, victim HP)          # the client uses its own copy of the HP (monsters: spawn HP);
                                       # the server must use its own current HP
```

Special branches, in execution order. All are determined; implement them as needed:
- **Fire splash:** see row 6 of section 1.
- **Follow-up hits:**
  - class 2 with p5 3 uses family 0x237 (Double Kick); class 3 with p5 4 uses family 0x780 (Double Arrow);
  - the extra hit is `max(1, trunc(rec.SPA·B/100) + E)`;
  - it is dealt as an arg6 hit, which re-adds the weapon element.
- **Reflect (Wicked Protection 0xB1A..0xB24 on the victim):**
  - only when arg5 = arg6 = arg7 = 0, and not for (p5 9, class 2, job 2) or (p5 6, class 4, job 2);
  - `f = f32((id-0xB1A)·0.05 + 0.3)` and `r = trunc(f·B)`;
  - r is dealt back to the attacker, and `B -= r`.
- **Vampiric Attack (0xB25):** on event 9 with p5 9 and attacker job 2, if the skill is learned, the attacker heals rec.HP and `total = 2·rec.HP`.
- **Wind and Nova:** a wind (4) skill sets victim +0x13E8 = 1.0. Nova (class 5, job 1, p5 7) instead sets 2.5 and makes **B = 0**.
- **Delayed hits:**
  - (p5 9, class 2, job 2) and (p5 6, class 4, job 2) store the clamped total at victim +0xE20 and show no digit;
  - FUN_00426b60 later releases it as an event-9 arg6 hit, which adds the weapon element again.

There is **no randomness** anywhere in this function.

## 4. Stat derivation: FUN_0041b830 (field mode, scene+0xF88 == 0)

### 4.1 Monster (+0x9C == 4)

- Values are copied from the hni template with no scaling:
  - Body_Atk, Weak_Atk, Strong_Atk and Def are used as they are;
  - level = Lv;
  - GuardDef = 0 and class = 0.
- Element: hni **type** 4..7 gives element e = type-3 (table 0x41D064). Then +0x11F4 = Attri_Atk and resist[e] = Attri_Def. Other types have no element.
- Share ratios:
  - strong = f32(S / f32(S+W)) and weak = f32(W / f32(S+W));
  - if S+W == 0 they are 0.7f and 0.3f (0x52EEB8 and 0x52EEB4).
- The buff % terms (section 4.2, step 7) also apply to monsters, but only when they have buffs.

### 4.2 Player (+0x9C == 3)

**Inputs:**
- class +0x118 (`char['class']`), job +0x119 (`char['job2']`);
- level +0x9D (from exp);
- STR/DEX/INT/SPR as u16 at +0xF0..+0xF6 (`spr` is sent as stat_tol);
- the 15 equip ids at +0x150;
- the learned skills at +0x2AE and the buffs at +0xF24.

**Equipment loop.** For every item, of any Kind:
- `STR/DEX/INT/SPR bonus += Str/Dex/Int/Tol`;
- `P_A += Skill_P_A`; `P_D += Skill_P_D`;
- Skill_A_A goes into pctA[Attribute] and Skill_A_D into pctD[Attribute]. Attribute 0 means all four elements;
- `SA += S_Att`; `WA += W_Att`.

Then by Kind:
- **12 (shield):** `GuardDef += Def`. The shield element goes to +0x11F1/+0x1234.
- **11 (weapon):** `wc` = the index of the **last** non-zero Job flag. Its Attri_Atk goes to EA with element = Attribute.
- **3 and 7:** FUN_0041d640 (not modelled; see section 6).
- **Any other Kind:** `DefSum += Def`, and Attri_Def goes to resist[Attribute].

**Stats and M.** Clamp and store as float32: `S, D, I, T = f32(max(0, base + bonus))`. With c = class and L = level, M is stored as float32:

| wc (weapon Job index) | M |
|---|---|
| 0 (Novice weapon, or bare hands) | (kSTR·S+bSTR)/10 + L/10 + 1 |
| 1 (sword) | (kSTR·S+bSTR)/7 + L/10 + 1 |
| 2 (fist) | (kSTR·S+bSTR)/6 + L/10 + 1 |
| 3, 4 (bow, dagger) | (kDEX·D+bDEX)·0.125 + L/10 + 1 |
| 5, 6 (staff, wand) | (kINT·I+bINT)/10 + L/10 + 1 |

Class tables (float32, class 0..6):

| stat | k values | k address | b values | b address |
|---|---|---|---|---|
| STR | 1, 1, 1, 1.4, 1.4, 1.4, 1.3 | 0x525F44 | 10, 20, 25, 15, 20, 15, 20 | 0x525EAC |
| DEX | 1, 1.4, 1.3, 1, 1, 1.2, 1.2 | 0x525F60 | 10, 10, 15, 25, 20, 20, 15 | 0x525EF0 |
| INT | 1, 1.3, 1.4, 1.3, 1.2, 1, 1 | 0x525F7C | 10, 15, 10, 15, 15, 25, 25 | 0x525F0C |
| SPR | 1, 1.2, 1.2, 1.2, 1.3, 1.3, 1.4 | 0x525F98 | 10, 25, 20, 15, 15, 10, 10 | 0x525F28 |

**Final values:**

```
base   = kDEX·D+bDEX if wc in (3,4) else kSTR·S+bSTR     # STR even for staves and wands
Strong = SA ? trunc(r(r(r(r(base+100)·SA)/100) + M)) : trunc(M)
Weak   = WA ? trunc(r(r(r(r(base+100)·WA)/100) + M)) : trunc(M)
Def    = DefSum ? trunc(r(r(r(r(kSPR·T)+bSPR)+100)·DefSum)/100) : 0
EA     = trunc((kINT·I+bINT+100)·EA/100)
resist[e] = trunc((kDEX·D+bDEX+100)·R/100)
```

Then, in this order:
1. Raw option-stone sums are added. They are 0 today.
2. The **share ratios** are computed as for monsters.
3. The % adds:
   - Strong: if Strong ≠ 0, `q = trunc(Strong·P_A/100)`, then `Strong += q if q ≠ 0 else 1`;
   - Weak: `Weak += trunc(Weak·P_A/100)`;
   - Def: if Def ≠ 0, `Def += trunc(Def·P_D/100)`;
   - EA and resists get the same "q, or 1 when q is 0" rule, using pctA/pctD.

**P_A and P_D sources:**
- equipment;
- active +0xF24 buffs, excluding:
  - summons 0x9CE–0x9D8, 0x9E4–0x9EE and 0x9EF–0x9F9;
  - self-cast 0x9D9–0x9E3;
  - auras 0x8E7–0x8F1 and 0x8FD–0x907;
- the six slots at +0x111C;
- for class 3 or 4 only: the learned rank of Evasion (0x171 or 0x19D), which adds Skill_P_D (+2).

## 5. Precision (implementation)

- Use `f32(x) = struct.unpack('<f', struct.pack('<f', x))[0]`. `_ftol2` is truncation.
- Every FSTP float store rounds to float32.
- **Recommended:** derivation with `r` = identity (plain Python double, PC_53), and damage with `r = f32` on every operation (PC_24).
  - Double rounding through Python doubles exactly reproduces x87 PC_24 here, because the operands are ≤ 24 bits and 53 ≥ 2·24+2.
- The two modes differ by at most ±1:
  - in 0.74% of random builds (Weak, Strong or Def), for example class 0, Lv1, STR 0 with a W_Att-9 wc-0 weapon gives Weak 12 at PC_24 and 11 at PC_53;
  - in 0.0015% of level-scale inputs.
- Because the server is authoritative and the displayed digit carries the random grade roll, either mode is acceptable.
- A D3D wrapper such as dgVoodoo2 could change the main thread's precision (unverified).

## 6. Out of scope, with the safest approximation

| Not modelled | Safest approximation |
|---|---|
| Option-stone words (+0x182+12·slot) | Ignore them; every live record's `w` is all zeros. If they are added later: stone Str/Dex/Int/Tol go into the stat bonuses before scaling; stone S_Att/W_Att/Def are added raw after scaling. |
| Kind 3/7 option byte (FUN_0041d640, table 0x525D60) and the 7 avatar slots at +0x170 | Ignore them. Their Str..Tol, W_Att/S_Att and P_A/P_D still count, because those adds happen before the Kind switch. |
| Arena/room mode (scene+0xF88: stats 55, Lv 49, fixed armour) | Not a field concern. |
| Buffs and passives | Use the sums from section 4.2 for the buffs the server tracks. If a buff is unknown, use P_A = P_D = 0. |
| Special branches (section 3.4: fire splash, follow-ups, Nova, reflect, Vampiric Attack, delayed hits, fairy branch) | If not implemented, treat them as a normal event-9 skill hit. That over-estimates splash victims and Nova (the client shows 1 for Nova) and under-estimates follow-ups. Always do: Holy Protection → 0; Magic Shield → damage goes to MP. |
| arg5 (thrown item) path | `max(1, -rec.HP)`, capped at HP-1. No weapon element and no reflect. |
| DoT | Not part of FUN_004194f0. Keep the server's current dot_damage. |

## 7. Expected values (base digit, before the grade roll)

### Derived stats

All rows were checked on the exe under Unicorn, with identical results at both control words.

| Entity | M | wc | Weak | Strong | Def |
|---|---|---|---|---|---|
| TestHero: cls 1 job 1 Lv14, 12/6/6/10, Wooden Blade 70 + items 19, 21 | 6.9714 | 1 | **18** | **37** | **2** |
| TestHero with all 61 points spent (STR 39) | 10.8286 | 1 | 25 | 47 | 2 |
| Lv1 Novice 3/2/1/3, bare hands | 2.4 | 0 | 2 | 2 | 0 |
| … + Wooden Stick 179 | 2.4 | 0 | 3 | 9 | 0 |
| … + stick + items 19, 21 | 2.4 | 0 | 3 | 9 | 2 |
| Lv1 Novice STR 9 + stick | 3.0 | 0 | 4 | 10 | 0 |
| Lv3 Novice 3/2/2/2 + stick | 2.6 | 0 | 3 | 9 | 0 |
| Lv3 Novice 7/4/2/4 + stick | 3.0 | 0 | 4 | 10 | 0 |
| Lv3 Novice 15/2/0/0 + stick | 3.8 | 0 | 5 | 11 | 0 |
| Lv3 Novice 7/4/2/4 + Crude Wooden Club 121 | 3.0 | 0 | 6 | 12 | 0 |
| Lv11 Novice 30/8/5/6 + Wooden Sword 9 + Light Leather 64/65/68 | 6.1 | 0 | 14 | 29 | 25 |
| Lv11 Novice 25/8/6/10 + Wooden Sword, no armour | 5.6 | 0 | 13 | 28 | 0 |
| Lv11 cls 1, 25/10/4/10 + Short Sword 71 + 158/159/68 | 8.5286 | 1 | 28 | 62 | 30 |
| Lv11 cls 2, same stats + Iron Fist 258 | 10.4333 | 2 | 23 | 41 | 29 |

Monsters:
- **Ssiyo:** Lv1, HP 5, Body 3, Weak 4, Strong 7, Def 2, no element.
- **Koring:** Lv2, HP 10, Body 4, Weak 6, Strong 9, Def 5, no element.
- **Monkey Soldier:** Lv9, HP 104, Body 14, Weak 13, Strong 21, Def 30. Type 6 gives Earth (3), EA 5, resist[3] 9, shares 0.61765 / 0.38235.

### Hits

| Hit | Event | B | E | **Digit** | Server today |
|---|---|---|---|---|---|
| TestHero weak → Monkey Soldier | 7 | 8 | 0 | **8** | 1 |
| TestHero strong → Monkey Soldier | 9 | 24 | 0 | **24** | |
| TestHero Ice Spear r1 (260, p5 1) → Monkey Soldier | 9 or 4 | 24 | 13 | **37** | |
| TestHero Fire Beat r1 (271) / Wind Cutter r1 (314) / Double Attack r1 (1898) | 9 | 24 / 24 / 27 | 14 / 10 / 0 | **38 / 34 / 27** | |
| Monkey Soldier contact → TestHero | 6 or 1 | 9 | 0 | **9** | 12 |
| Monkey Soldier attack A → TestHero | 7 or 8 | 8 | 0 | **8** | 11 |
| Monkey Soldier attack B → TestHero | 9, 10, 4 or 5 | 15 | 2 | **17** | 19; event 5 is currently 0 (bug) |
| Monkey Soldier weak into a guard | 2 or 3 | — | — | **0** | |
| TestHero (STR 39) weak / strong → Monkey Soldier | 7 / 9 | | | **13 / 34** | |
| Lv1 Novice, bare, weak / strong → Ssiyo | 7 / 9 | | | **1 / 1** (5 hits to kill) | |
| Lv1 Novice + stick, weak / strong → Ssiyo | 7 / 9 | 1 / 7 | | **1 / 5** (strong capped at HP 5; one hit) | |
| Lv1 Novice STR 9 + stick, weak → Ssiyo | 7 | | | **2** | |
| Ssiyo contact / weak / strong → bare Novice | 6 / 7 / 9 | | | **3 / 4 / 7** | |
| … → Novice wearing items 19 + 21 (Def 2) | 6 / 7 / 9 | | | **1 / 2 / 5** | |
| Lv2 Novice + stick, weak → Ssiyo (STR 3 / STR 7), *model* | 7 | | | **2 / 3** | |
| Lv3 Novice weak / strong → Koring: 3/2/2/2 stick; 7/4/2/4 stick; STR 15 stick; club STR 7; club STR 11 | 7 / 9 | | | **1/6; 2/8; 3/9; 3/10; 4/10 (cap)** | |
| Lv11 weak / strong → Monkey Soldier: Novice with Wooden Sword (STR 30 / STR 25); cls 1 with Short Sword; cls 2 with Iron Fist | 7 / 9 | | | **4/15; 4/14; 14/45; 10/26** | |
| Monkey Soldier weak / contact / strong → Lv11 Novice in Light Leather (Def 25); → Novice with no armour; → cls 1 (Def 30) | 7 / 6 / 9 | | | **4/4/10; 11/12/20; 3/4/9** | |
| cls 2 job 1 Lv30 (60/20/10/20, Crushing Fist) → Monkey Soldier, event 9: plain; p5 1 Blazing Kick; p5 2; p5 4 Flying Kick; p5 6 Triple Kick; p5 8 Merciless Strike | 9 | | | **67; 91; 0 (no digit); 87; 28 (÷3); 22 (÷6)** | |

## 8. Retail sanity check (everything consistent)

- **Lv1 Novice hits Ssiyo for 1.** Every bare-handed build gives 1, so 5 hits kill. With the stick the weak hit is 1–2 at Lv1 and 2–3 at Lv2. That matches the "stick 2–3" footage. Ssiyo contact on a Novice in starter clothes is 1, which matches the retail red "1".
- **Koring at Lv3: BAD 2 / GOOD 3 / CRIT 4.** The base is an integer, so "2.4–2.7" is not possible.
  - A base of 3 fits exactly: 3·0.75 → 2, 3·1.0 or 1.25 → 3, 3·1.5 → 4.
  - Base 3 is a Lv3 Novice's weak hit with the Crude Wooden Club (STR 7), or with the stick at STR ≥ 15.
  - The stick at STR 7–11 gives base 2 instead.
- **~16 on Monkey Soldiers at Lv11.**
  - A Novice with the Wooden Sword and STR 30 gets a strong hit of 15; STR 25 gives 14.
  - A class-1 player's weak hit with a Short Sword is 14.
  - An exact weak 16 needs Weak_Atk 30. Monkey packs hitting a Lv11 player in Light Leather do 3–4 (weak or contact) and 9–10 (strong), matching retail's red "2" / "4".
- **TestHero is low but correct.**
  - He has spent only 34 of his 61 stat points and wields the weakest class-1 sword, so he deals 8 / 24.
  - The live client digit of 7–10 is 8 × the grade roll.
  - The monkey swings the client showed as 8–9 are 8 (attack A) and 9 (contact); the server currently applies 11.

## 9. Changes this implies for the server

1. Replace `_compute_damage`, `combat.skill_damage` and `combat.body_damage` with `derive_*` plus `damage()`, applied in both directions.
2. When a monster hits a player, use the player's **derived** Def (scaled by SPR) plus the raw GuardDef for events 1 and 4/5. Remap 5→4, 8→7, 10→9, and give 0 only for events 2 and 3.
3. Cap damage at the server's own current HP.
4. Skill hits: resolve the skill from the fresh cast (section 2). Apply ÷3/÷6 and NODMG as in the section 3.2 table.
5. Apply the grade roll only to the display; the HP loss is the base value.

Reference implementation and checks, in `<local scratch>\dmg\final\`:
- `ref_model.py`: pure Python. It provides `derive_monster`, `derive_player`, `level_scale`, `damage`, `SKILL_SLOT`, `skill_cell`, `cell_for_skill` and `learned_rank`.
- `xcheck3.py`: the comparison against both derivations' models.
- `emu_check.py` and `emu_check2.py`: the Unicorn runs on the pristine exe.
- `examples3.py`: the example table.