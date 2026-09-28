# Equipment appearance: authoritative server-fix spec (EN 2009 primary, EN 2008 noted)

## 1. Verdict

The server sends the wrong appearance words. The client never builds a player's look from the equipped item ids. It draws the appearance array exactly as the last S2C 0x02, 0x07, 0x04 or 0x05 sent it.

`records.appearance()` sends `char['look']`, which is still the creation outfit, with only `char['weapon']` merged into slot 11. So every enter-world, portal, relog, select screen and observer spawn shows the creation clothes and the weapon.

S2C 0x1D, 0x1E and 0x24 do make the client recompose, but only one layer each (the Kind of that item). The next 0x07 then overwrites that layer again.

The log line "weapon sprite 5" is not a bug. It prints `char['weapon']`, which is the Spr_Num of the worn Wooden Sword (item 9), whatever item was just equipped.

Everything below was checked by me against the decomp and asm of both exes, the hii files and the hs folders.

## 2. Client model (verified)

### 2.1 Storage and drawing

| | 2009 | 2008 |
|---|---|---|
| Appearance array | entity+0x12E, u16[17] | entity+0x120, u16[14] |
| Equip grid (ids) | +0x150, u16[25] | +0x13C, u16[25] |
| 12-byte blocks | +0x182 | +0x16E |
| Gender byte | +0x11B | +0x113 |
| Item table | scene+0xFB0 (index = id-1) | scene+0xF84 |

- **Gender:** 0 = female, which is what (F) items with Gender 2 require. 1 = male.
- **Draw path:** FUN_004332f0 calls FUN_00401e90 with `parts = entity+0x12E` (0x433D26 / 0x433D37).
  - Loop at 0x401F2A: layer = the frame's part-list entry (skipped if >= 0x11). `v = word parts[layer]`, and `v == 0` means the layer is not drawn.
  - Sprites are cached per (layer, v).
- **Layer names** (0x526008, stride 0x80): 0 cloak, 1 body, 2 head, 3 eye, 4 hair, 5 pant, 6 shirt, 7 face, 8 glove, 9 shoes, 10 helm, 11 weapon, 12 shield, 13 bhair, 14 pet/pet, 15 pet/pet_helm, 16 pet/pet_wear. 2008 uses the first 14.
- **File names:** most layers load `%s/%s/%s%03d_a1.hsc` (0x5268F0) with the index `%s%s001.hsi` (0x526904). The weapon layer (11) loads `%s%s%03d.hsi` (0x5268D0).
- **So an appearance word is a sprite file number, and for an item it is that item's hii Spr_Num.** The gender is already built into Spr_Num: (F) items use 0xx, (M) items use 1xx.

### 2.2 The one composition routine

2009 FUN_004282c0 (0x4282C0–0x42856B) and 2008 FUN_00426d50 (0x426D50–0x426FFB) are byte-for-byte the same logic, apart from the Kind limit and the item-table offset. Every 0x1D/0x1E/0x24 call site passes `param_8 = 0`. Ported from the decomp:

```python
# Kind -> (regular grid slot, cash grid slot) it READS (jump table 0x42856C / 2008 0x426FFC)
READ = {5: (8, 20), 6: (4, 17), 8: (9, 21), 9: (10, 22), 10: (0, 16), 11: (5, 18), 12: (6, 19)}
CLEAR_HAT = 0xD5C                    # item 3420, cash Kind 10: the hat layer shows the hair
KIND_LIMIT = 17 (2009: "0x10 < Kind -> return") / 14 (2008: "0xd < Kind")

def compose(look, grid, kind, cash, spr, gender, job0):      # grid = state AFTER the change
    if kind >= KIND_LIMIT: return
    n = c = 0; bare = False
    if kind in READ:
        n, c = grid[READ[kind][0]], grid[READ[kind][1]]
        bare = kind in (11, 12) and n == 0
    if bare and cash: return                          # cash weapon/shield skin, no real one: no change
    C = hii(c) if c else None; N = hii(n) if n else None
    if N is None:
        if C and not bare:
            if C.id == CLEAR_HAT: look[10] = look[4]
            else: look[kind] = C.spr_num
            return
    else:
        if C:
            if kind == 11 and N.job[0] != C.job[0] and (N.job[1] == 0 or C.job[1] == 0):
                look[11] = N.spr_num; return          # class-mismatched weapon skin: show the real one
            if C.id == CLEAR_HAT: look[10] = look[4]
            else: look[kind] = C.spr_num              # costume beats regular
            return
        if spr == 0:
            if cash: look[kind] = N.spr_num; return   # costume taken off, the regular one shows again
        elif cash and job0 != N.job[0]:
            return                                    # (unreachable from 0x1D; kept for fidelity)
    if kind == 4:                                     # hair: also the back hair, and helm if it showed hair
        if look[10] == look[4]: look[10] = spr
        look[13] = spr; look[4] = spr
    else:
        look[kind] = spr
    if spr == 0:
        if kind in (5, 6): look[kind] = 101 if gender else 1    # underwear
        elif kind == 10:   look[10] = look[4]                    # bare head shows the hair
```

Notes on the port:
- `def+0x168` is Job[0] and `def+0x16C` is Job[1]. This is consistent with the client's equip gate, which reads `def+0x168 + class*4`.
- `def+0x1CC` is Kind, `+0x1D0` Spr_Num, `+0x1F0` Cash, `+0x144` id.
- The FUN_00427af0 (2009) and FUN_00426680 (2008) slot tables in `inventory.py` place Kinds 5, 6 and 8–12 exactly in the slots this routine reads.

### 2.3 Which packets change the look

| S2C | 2009 handler | Effect on the appearance array |
|---|---|---|
| 0x07 / 0x04 | 0x453588 | Gender goes to +0x11B (0x45378F / 0x45379C). 17 words are copied verbatim (loop 0x4537A2–0x4537D6). The grid ids are stored only. **No compose.** |
| 0x05 | 0x4545B4 | Same, verbatim. |
| 0x02 | 0x451CF0 | 17 words verbatim into the select entity (0x452459). There are no grid ids. |
| 0x1C | — | Copies the creation-window words for parts 1, 4, 5, 6, 9, 10 and 13. |
| 0x1D | 0x45804E | 1. Item def (FUN_00404750) must be Type 1 (0x458142).<br>2. Entity is looked up by uid (FUN_00419450, 0x458161).<br>3. FUN_00427af0 writes the grid (0x4581C5).<br>4. **FUN_004282c0 runs (0x458215)** with that Kind, Cash, Spr_Num and Job[0], and the entity's own gender.<br>5. Stats are recalculated.<br>6. Only then comes the `uid == scene+0x224` check (0x458246) and the local bag and window work. |
| 0x1E / 0x24 | 0x458E07 | FUN_00427f00 clears the grid slot (0x458F45; its result is ignored). **FUN_004282c0 then runs with Spr = 0** (PUSH 0 at 0x458F68, call at 0x458F80). Stats are recalculated. For the local player only, 0x1E adds the item to the bag. |

- In 2009 FUN_00451960 calls FUN_004282c0 from exactly four places: 0x4577F9 (0xAB pet equip), 0x457AA0 (0xAC), 0x458215 (0x1D) and 0x458F80 (0x1E/0x24).
- Its other callers are FUN_004289a0 (cash hair or costume use, via FUN_0046a2e0), FUN_00462f20 and FUN_0046a2e0. None of them is on the 0x07 path, and neither is FUN_00422fc0 (registration).
- **2008:**
  - 0x07: gender goes to +0x113 (0x44F120) and 14 words to +0x120 (0x44F133).
  - 0x1D: FUN_00426680 at 0x452AFC, then FUN_00426d50 at 0x452B48 (look +0x120, grid +0x13C, gender +0x113).
  - 0x1E/0x24: FUN_004269a0 at 0x4541CA, then FUN_00426d50 at 0x454205 with PUSH 0 (Spr) at 0x4541ED.

## 3. Disagreements between the two RE answers, resolved

| # | Point | Resolution (from asm or data) |
|---|---|---|
| D1 | Which layers can never draw | Checked `hs/` in both builds. There is no `head/` and no `bhair/` folder, so layers 2 and 13 never draw. `eye/` does have `_a1` files, for 012, 015 and 017. A1's "send 0 for part 3" is **wrong**: part 3 is written by cash Kind-3 glasses and must be sent as composed. Part 13 must still carry the hair word, because the client itself writes it. The server mirrors the array and never filters by asset. |
| D2 | Creation look for gender 1 | A1 is right and matches `store.DEFAULT_LOOK_SLOTS[1]`: body and hair 101, pant and shirt 102, shoes 2. A2's "the 101 set" is imprecise. This does not change the fix. |
| D3 | Weapon costume rule | Both answers mean the same thing. The costume shows unless Kind is 11, Job[0] differs, and at least one of the two items has Job[1] == 0; then the regular weapon shows. |
| D4 | Extra branches that only A2 listed | Confirmed in the decomp: when a costume comes off and a regular item stays, the regular Spr_Num is restored. The "cash item with Spr and a mismatched Job[0] makes no change" branch is also real, but it cannot be reached from 0x1D (a cash item always lands in the cash slot, so C is never null there). Keep it in the port anyway. |
| D5 | Kinds 14–16 | FUN_004282c0 accepts Kind ≤ 16 in 2009. Regular Kinds 14, 15 and 16 have Spr_Num 0 in the hii and no grid slot; the server refuses them, so no 0x1D is ever sent for them. Cash Kinds 15 and 16 (pet hat and pet wear, Spr 101–302, grid slots 23 and 24) do write look[15] and look[16]. 2008 ignores every Kind ≥ 14. |
| D6 | Order of the migration | Order does not matter, and replaying is idempotent. I tested this with 3000 random sequences of 40 equip and unequip steps on the 2009 hii: replaying every equipped item over the current look, with the final grid, always gives the look back (0 mismatches; script at `scratchpad\appearance\spec\idem.py`). |
| D7 | 2008 grid | 16 regular and 9 cash slots at +0x13C. The routine reads the same slot pairs as 2009. |
| D8 | Whether an in-session equip already showed | Yes, statically. The same entity lookup (JZ at 0x45816A) gates both the compose and the bag removal. If the item left the bag on equip, the layer was recomposed. So the user sees the revert at 0x07 or 0x02 time. |
| D9 | Whether the "stick" is weapon005 or weapon001 | Not settled without a live look. The server sends 5 today (the log shows `char['weapon'] = 5`). weapon005 is a plain wooden sword with a small guard, which looks stick-like at game scale. If a capture shows slot 11 = 1, the worn item is the Wooden Stick (179, Spr 1). |

## 4. Server fix (worktree `server_fix`, branch `flea-return`)

### 4.1 State model
- `char['look']` (words 0–13) plus `char['look_ext']` (words 14–16, 2009 only) become the **stateful, client-mirrored** appearance.
- Nothing else feeds the appearance words. `char['weapon']` stops being an input. It may stay as a derived `= look[11]` for logs and old tests.

### 4.2 `inventory.py`
- Add the `compose()` port from §2.2.
  - `KIND_LIMIT` = 17 when `catalog.client_build == '2009'`, otherwise 14.
  - The grid is the id map from `char['equipped']` (slot → `entry['id']`, 0 if empty).
  - Defs come from `self.catalog`, using `spr_num`, `kind`, `cash`, `job[0]`, `job[1]` and `id`.
- Add `Inventory.recompose(item_id, spr, gender)`:
  - build `words = look + look_ext` (17 words for 2009) or `look` (14 for 2008);
  - run `compose`;
  - write back `look[:14]` and `look_ext[14:17]`.
- Add `Inventory.compose_all(gender)`. It replays every equipped instance with its own Spr_Num. This is the idempotent migration (D6).
- Call sites. Each one runs **after** the grid change, as the client does:
  - `wear()`: after `self.equip(...)`, call `recompose(item_id, d.spr_num, gender)`.
  - `take_off()`: after `unequip_item(...)`, call `recompose(item_id, 0, gender)`.
  - The 0x24 drop-worn path (`windslayer_server.py` ~6428): after `bag.unequip_item(...)`, call `recompose(item, 0, gender)`. This replaces `bag.refresh_appearance()`.
  - These three are the only grid writers today (checked by grep). Any future grid writer must call it too: STARTER_WEAPON, trades, GM gear, pet 0xAB/0xAC, and cash hair or costume use via the 0x72 sub-dispatcher.
- `refresh_appearance()` must no longer overwrite slot 11 from `weapon_sprite()`. That would break costume weapons. Either drop it or reduce it to `char['weapon'] = look[11]`.

### 4.3 Gender source
Pass the **exact bool the build's records send**:
- 2009: `records.char_gender(char, account)`, the same value as `_record_2009` and the 0x02 row.
- 2008: `1 if account['gender'] else 0`, as the 2008 `player_record` sends it.

Add one helper, `records.record_gender(char, account, build)`, and use it everywhere. Observers compose with the gender from the record they received, so these values must agree.

### 4.4 `records.py`
- `appearance(char)` returns the stored `look` verbatim, with **no slot-11 merge**. Retire `APPEARANCE_WEAPON_SLOT`.
- The fallback for a malformed `look` should use `default_look(record_gender)` instead of `default_look(0)`.
- `appearance_2009` stays `appearance + look_ext`.
- This one change fixes 0x02, 0x07, 0x04 and 0x05 in both builds. `presence.py` already builds its rows through `player_record` and `to_0x05`.

### 4.5 Migration
- Run `compose_all(gender)` once per character before the first 0x02 of a session is built, or at store load if the catalog is available there.
- It is idempotent, so running it again is harmless.
- Slots that had nothing equipped keep the stored creation clothes.

### 4.6 STARTER_WEAPON (`windslayer_server.py:8247`)
- Today it writes an **item id** into `char['weapon']`. It is dormant while the setting is 0.
- If it is ever enabled, it must put the item in grid slot 5 and call `recompose`.

### 4.7 Tests to update
These encode the old merge (and, in `test_records`/`test_handlers`, an item id 0xB3 in slot 11):
- `test_records.py:156`
- `test_handlers.py:1747` and `:1895`
- `test_broadcasts.py:358–461`
- `test_inventory.py:253–305`
- `test_items.py:156` and `:285`
- `test_ground.py:524–548`

New tests to add:
- TestHero's words (§5).
- Each unequip default (§5.3), female and male.
- Costume over regular, and costume removal restoring the regular item.
- Clear Hat (3420).
- The weapon Job rule.
- A 2008 catalog ignoring Kind ≥ 14.
- Replay idempotence.

## 5. TestHero, concrete values (class 1, female, gender bool **0**)

### 5.1 Words for each layer

| Index | Layer | Today | Correct | Source |
|---|---|---|---|---|
| 0 | cloak | 0 | 0 | none |
| 1 | body | 1 | 1 | creation |
| 2 | head | 0 | 0 | |
| 3 | eye | 0 | 0 | |
| 4 | hair | 1 | 1 | creation |
| 5 | pant | 2 | **7** | Light Leather Leggings (F), item 65: Kind 5, Spr 7 |
| 6 | shirt | 2 | **7** | Light Leather Vest (F), item 64: Kind 6, Spr 7 |
| 7 | face | 0 | 0 | |
| 8 | glove | 0 | **7** | Light Leather Gloves, item 106: Kind 8, Spr 7 |
| 9 | shoes | 2 | **7** | Light Leather Sandal, item 68: Kind 9, Spr 7 |
| 10 | helm | 1 | **360** | Blue Novice Hat, item 2068: Kind 10, Spr 360 |
| 11 | weapon | 5 | 5 | Wooden Sword, item 9: Kind 11, Spr 5 |
| 12 | shield | 0 | 0 | |
| 13 | bhair | 1 | 1 | hair |
| 14–16 | pet layers (2009 only) | 0 | 0 | no pet |

All six sprite files exist in both builds' `hs` folders: helm360, shirt007, pant007, glove007, shoes007 and weapon005.

### 5.2 What each packet must carry

**2009:**
- **S2C 0x02 record:** `gender = 0`, `appearance[17] = [0,1,0,0,1,7,7,0,7,7,360,5,0,1,0,0,0]`.
- **S2C 0x07 (own record at enter world, map change and relog) and 0x04/0x05 (to others):**
  - `gender = 0`;
  - `appearance_part[17]` = the same 17 words;
  - `equip_item_id[15]` = `[2068,0,0,0,64,9,0,0,65,106,68,0,0,0,0]`, each with its stored 6 option words verbatim;
  - cash rows 15–24 = 10 × (id 0, attr0 0).

**2008** (same character on the 2008 build; account `test` gender 0):
- 0x02: 14 words `[0,1,0,0,1,7,7,0,7,7,360,5,0,1]`.
- 0x07/0x04/0x05:
  - `gender = 0`;
  - the same 14 words;
  - 16 equip ids `[2068,0,0,0,64,9,0,0,65,106,68,0,0,0,0,0]`;
  - 9 cash ids, all 0.

### 5.3 Unequip results (female; what the client shows and what must be stored)

| Removed item | Stored change |
|---|---|
| Vest | look[6] = 1 |
| Leggings | look[5] = 1 |
| Hat | look[10] = look[4] = 1 |
| Sword | look[11] = 0 |
| Gloves | look[8] = 0 |
| Sandal | look[9] = 0 |

- For a male character, the shirt and pant default is 101.
- Equipping the item again sets its layer back to Spr_Num (7, or 360 for the hat, or 5 for the sword).
- The creation clothes (2 / 102) never come back after an unequip. The client does the same, because they are not items.

## 6. Equip and unequip replies, and observers

1. **Own avatar:** the client recomposes by itself on 0x1D (equip) and on 0x1E/0x24 (unequip). It does this before the local-player check, so it works for the local and remote entity alike.
   - Keep 0x1D, 0x1E and 0x24 exactly as they are now: echo `uid`, item and the request's 12-byte block.
   - Send **no** follow-up packet. In particular, do not send a refresh 0x07: that recreates the entity and resets its state.
   - The server's only new job is to apply the same `compose` to the stored look, so the next 0x07 or 0x02 matches what is on screen.
2. **Observers:**
   - On spawn, 0x04/0x05 must carry the composed words. It must also carry the grid ids and the same gender bool, because their later 0x1D/0x1E compose reads their own grid copy and gender.
   - Live changes come from the 0x1D/0x1E/0x24 broadcast to holders that `presence.to_holders` already sends, using the model's block. Nothing else is needed.
   - The 2009 cash rows carry only id + attr0. That is enough, because the compose reads ids only.
3. **Select screen:** 0x02 carries the composed words.

## 7. Residual items for a live check
- **"Stick":** a capture of 0x07 slot 11 settles whether it is weapon005 (the Wooden Sword, which is what the server sends today) or weapon001.
- **If an equip in the same session showed no change on screen,** while the item still left the bag, that contradicts D8. Capture the 0x1D `uid` and compare it with the 0x07 `uid`.
- **Future features that also rewrite appearance words client-side:** the pet opcodes 0xAB/0xAC (layer 14) and cash hair or costume use (2009 FUN_004289a0 via the 0x72 sub-dispatcher; 2008 FUN_00427430 via SubHandler4). When implemented, each must also apply the same compose to the stored look.

Scratch evidence:
- `<local scratch>\appearance\spec\idem.py` (the idempotence test; it also prints TestHero's 17 words)
- `...\appearance\re\compose_sim.py` (the Python port of the routine)