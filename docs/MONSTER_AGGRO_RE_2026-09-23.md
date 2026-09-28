## mechanism

Addresses are EN 2009 first, 2008 in [brackets]. Everything in this field was re-read in the asm.

1. Per-tick order. FUN_0042c920 [FUN_0042b260] runs every 30 ms, only while room mode (scene+0xFB4)+0x7C == 0 [scene+0xF88+0x70]:
   - AI FUN_004185b0 [FUN_00417e10]
   - C2S 0x0D builder
   - command queue FUN_004129f0 [FUN_00412100]
   - hit resolution FUN_00412c60 [FUN_00412360]
   - state machine FUN_00414210 [FUN_00413920]
   - movement FUN_00415f80 [FUN_004158d0]
   - hit detection FUN_00416ab0 [FUN_00416380]

2. When the client AI runs a monster (0x418B22..0x418B66 [0x41834B..0x418386]). All of these must hold:
   - entity type +0x9C [+0x98] == 4
   - hni type +0xF00 [+0xE64] >= 3 (or pet owner +0x163C)
   - server_controlled +0x971 [+0x8E4] == 0
   - queue count +0x13FC [+0x135C] == 0
   - command hold +0xEE4 [+0xE48] == 0
   
   Each pass sets the "ticked" flag +0x96D [+0x8E1] = 1 and clears the inputs +0x940/+0x941/+0x942.

3. Wander or chase (0x418C86 [0x4184A4]).
   - It WANDERS when the aggro target +0xE44 [+0xDB4] == 0, or when the stationary flag +0xE74 [+0xDE4] != 0.
   - Otherwise it CHASES (0x418EA0..0x41919A [0x4184BE..0x4187AB]):
     - Target lookup: FUN_00419450 [FUN_004189f0]. If the target is missing or in state 0x10/0x16, +0xE44 = 0 (0x4191A6 [0x4187B7]).
     - Facing: if |dx| > 65, +0x93F = 6 (right) when mob.x < target.x, else 2 (left) (0x418EFE/0x418F07 [0x41851C/0x418525]). Within 65 px the old direction is kept, so the mob walks through the target and turns back later.
     - Jump: +0x940 = 3 when the target is higher and the no-jump flag +0xE6C [+0xDDC] == 0 (0x418F4D).
     - Drop: +0x940 = 4 when the target is more than 140 px lower and |dx| < 200, or more than 20 px lower and |dx| < 40 (0x418F88).
     - The distance constants were read from the exe: 65 @0x52EFD8, 140 @0x52EFD0, 200 @0x52EE08, 20 @0x52ED20, 40 @0x52EFC8.
     - Attack, only when +0xE50 (attack A) or +0xE70 (attack B) is set: the AI sets its own state to 4 or 1 for a moment and tests its attack rectangle against the target's body with FUN_0041dc70. On overlap +0x940 = 1 (attack A) or 5 (attack B) (0x41916E [0x41877E]). It picks A when +0xE50 is set and (rng bit 0 is set or +0xE70 is 0).
     - Skill: +0xE5C set → +0x940 = 6 (dash).
     - The chase has no leash and no timeout.

4. Template flags, and the Ssiyo/Pupu values.
   - The hni `AI:` line has 13 ints in 2009 and 10 in 2008. The loader FUN_00409150 stores them at template+0x254 [+0x250]. The 0x1A handler copies them to the entity: REP MOVSD of 13 dwords to +0xE50 at 0x456C99 [10 dwords to +0xDC0 at 0x451F67].
   - Flag map:
     - AI[0] → +0xE50, attack A
     - AI[3] → +0xE5C, skill/dash
     - AI[5] → +0xE64, proximity aggro (2009 only)
     - AI[6] → +0xE68, counter-jump
     - AI[7] → +0xE6C, cannot jump
     - AI[8] → +0xE70, attack B
     - AI[9] → +0xE74, stationary
   - Stats copied by 0x1A (0x456B9E..0x456C10): +0xF00 = type, +0xF04 = AtkType, +0x123C = Body_Atk, +0x11D4 = Weak_Atk, +0x11D0 = Strong_Atk, +0x11E0 = Def.
   - Ssiyo (2009) is Pupu (2008), template 1. Its AI line is 0 0 0 0 0 0 0 1 0 0 0 0 0, which means:
     - It can chase and drop down, but it never jumps and never swings.
     - Its only way to hurt the player is body contact (Body_Atk 3).
   - Koring, Ororing, Pponyang and Kamikaze Rat have all flags 0: they chase and jump, contact damage only.
   - Rynx (12) has attack A, attack B and counter-jump.

5. Every writer of +0xE44 [+0xDB4], from an exhaustive asm grep:
   - Hit apply FUN_004194f0 at 0x41A815 [FUN_00418ac0 at 0x419DE1]. It keeps a damage list +0x1410/+0x1420 and sets aggro = attacker when the attacker is the top damager or the old top damager is gone or dead.
   - The 2009 proximity scan at 0x41930C (flag AI[5], any type-3 player within 200x200 px).
   - Clears: 0x4191A6 and the mob's own death 0x4155D4 [0x4187B7, 0x414CB4].
   - 0x487BD5 writes to a settings-dialog object (called from UI code), not an entity.
   - No S2C handler writes it.

6. The host-only engine. Everything below runs only when scene+0xF40 [+0xF24] != 0:
   - both aggro setters (gates 0x41A6C8/0x41A70E and 0x41927A [0x419CA5/0x419CEB])
   - local HP subtraction (0x41A6C2)
   - the post-hit block 0x413B1B..0x413BAA: knockback, reaction +0x976, attacker +0xE10, +0x96F = 1, and an immediate re-run of the AI
   - a 990 ms decision cadence 0x418B6C..0x418BAE [0x41838C..0x4183C0]
   - a 17-slot position/state history ring 0x416B8C..0x416BEB
   
   The only store to scene+0xF40 [+0xF24] is a 0 in the CGameMain constructor: 0x4128B3 in FUN_00412840 [0x411FC3 in FUN_00411f50].

7. The server command path. 0x2A, 0x9E and 0x9F share handler 0x459C3D [0x454E6D]; the dispatch table confirms this.
   - At receipt it sets +0x971 = (opcode != 0x9E) (0x459C89 [0x454EB3]).
   - It builds a 0x58-byte node with hold 0x3DE (990 ms) and decodes the blob into it:
     - node+0x4A direction, from lo bits 0-1 (1 → 2 left, 2 → 6 right, else 8)
     - node+0x4B motion, from lo bits 2-4
     - node+0x4F action, from bits 12-15
     - node+0x4E reaction, from bits 16-19
     - node+0x44, from hi & 0xFFF
     - node+0x50 facing2, from bits 20-21
   - 0x2A only:
     - Reads a u32 target.
     - If the mob state != 0x10 and target != local uid (scene+0x224 [+0x220]), it also reads f64 x, f64 y and u8 airborne into +0x1298/+0x1328/+0x966, applied at once.
     - It flushes the queue, sets +0xEE4 = 0, and sets the node hold to 960.
     - If target == local uid: +0x96E = 0 (0x459F04 [0x455136]). Action, reaction, param, target and facing2 are cleared from the node, but direction and motion are kept. The hold is 960 - min(+0xE3C, hi12), which is 960 when hi = 0.
   - 0x9E: hold = one tick (30 ms), no flush. 0x9F: hold 990, no flush.
   - 0x1B (0x45712B [0x4523F9]) only appends a node. It never touches +0x971 or +0x96E.
   - Consumer 0x4129F0 [0x412100]:
     - It counts +0xEE4 down each tick.
     - It zeroes the hold at once for a fully idle mob: state 8, +0x93F 8, inputs 0, +0x9DC and +0x9E0 0 (0x412ABC..0x412AF6).
     - When the hold reaches 0 it pops the next node. For type 4: +0xEE4 = hold, +0x940 = motion, +0x93F = direction, +0x9DC = action, +0xE10 = target (0x412B27..0x412C29). The node's x/y is ignored for type 4.
   - Tick gate: the state machine, movement and hit detection (contact and attack checks included) run for a type-4 entity only while (+0xEE4 && +0x971) || +0x96D (0x4142B8..0x4142D1, 0x41602B..0x41603D, 0x416B50..0x416B69). +0x96D is cleared every tick at 0x416B79.

8. Monster hits on the player.
   - Only the victim's own client detects them: a type-4 attacker is checked only against victim uid == local uid (0x416EFF..0x416F20).
   - Attack A (mob state 4/0xE): victim +0x9DC = 7, or 8 (0x4172ED/0x4172F5); victim +0xE10 = the mob (0x417241).
   - Attack B (mob state 1/0xF): event 9 or 10.
   - Contact: event 6 (0x416DD6), or 1 while guarding. It needs +0xF00 >= 3 and (+0xF04 & 0x10) == 0 (0x416BFF..0x416C13).
   - Guarded hits give 2..5.
   - All of these leave in C2S 0x0D as action bits 12-15 plus event_source_uid.
   - The field client never subtracts HP itself. It only shows its own damage estimate (switch 0x419619, table 0x41A954: events 1/6 use Body_Atk, 7 uses Weak_Atk, 4/9 use Strong_Atk) and gives 270 ms of i-frames (+0xE8C = 0x10E at 0x41531A).
   - Players are never hit-locked: every site that sets +0x96E = 1 first checks victim type 4 and F40 == 0.

## why_no_aggro_now

Three gates. Gate A alone is enough.

**Gate A, the root cause (PROVEN): the field client never gives a monster a target.**
- The chase branch 0x418C86 [0x4184A4] needs +0xE44 [+0xDB4] != 0.
- Both setters sit behind the host flag scene+0xF40 [+0xF24]:
  - hit apply: gates 0x41A6C8/0x41A70E, write 0x41A815 [gates 0x419CA5/0x419CEB, write 0x419DE1]
  - proximity scan: gate 0x41927A, write 0x41930C (2009 only)
- That flag is only ever stored as 0, in the constructor (0x4128B3 [0x411FC3]); every other reference is a CMP or a load.
- No S2C opcode writes +0xE44.
- So hitting a Ssiyo only updates the client's damage-number estimate. Even untouched by us, the client AI would keep wandering.

**Gate B, our own packet (PROVEN statically; the visible freeze needs a live check): `_release_hit_lock` switches the mob off.**
- The 16-byte S2C 0x2A {mob, 8 zero bytes, target = receiver} sets +0x971 = 1 (0x459C89 [0x454EB3]).
- The AI gate 0x418B45 [0x418365] then skips that mob for good. Only 0x9E, 0x29 or a new 0x1A clear the flag again (0x459C89, 0x459B51, 0x457081).
- Its node is neutral (direction 8, motion 0, hold 960):
  - The flinch plays inside that window.
  - Once the mob is idle, the consumer's idle check zeroes the hold (0x412ABC..0x412AF6).
  - (+0xEE4 && +0x971) || +0x96D is then false, so the state machine, movement and hit detection stop ticking it.
- Result: after the first surviving hit the mob stands frozen in its idle pose. It does not wander or animate, and it cannot even do contact damage, because contact detection is inside the same tick gate.
- It can still be hit, but each later hit only buys another neutral 960 ms window.

**Gate C, the server (PROVEN from the server code):**
- Nothing drives the mob after the release: no chase or attack commands, and no 0x9E hand-back.
- MOB_CONTACT_DAMAGE is false by default, so `_on_move_state` drops every monster-hit report (C2S 0x0D action != 0 with event_source_uid = a mob). The client never lowers HP itself (0x41A6C2), so the player takes no damage at all.

**Expectation note (PROVEN from hni and the flag map):** Ssiyo/Pupu (template 1) has no attack A or B flag and has the no-jump flag. Even the retail AI never makes it swing: an aggroed Ssiyo chases, drops down ledges and bumps into you (contact, Body_Atk). Monsters that swing are ones like Rynx (AI[0], AI[8]).

## retail_server_behaviour

This section is INFERRED overall. Every client fact it rests on is PROVEN above, and there is no retail capture.

**Design evidence**
- The client ships the complete host version of the monster engine, switched off by scene+0xF40 == 0:
  - the hate list and aggro write
  - the post-hit block (0x413B1B..0x413BAA): knockback x/y, reaction +0x976, attacker +0xE10, the flag "+0x96F = 1, sync needed", an immediate AI re-run
  - the 990 ms decision cadence (0x418B6C..0x418BAE: +0xE38 += tick, and at 0x3DE it sets +0x973 = 1)
  - a 17-slot history ring (0x416B8C..0x416BEB)
- +0x972 [+0x8E5] has no writer in the client, and +0x96F/+0x970/+0x973 have no reader that sends anything. So the code that turned those flags into packets was the server's.
- The wire formats match that output:
  - S2C 0x2A {uid, blob with reaction/direction/motion, target = attacker, f64 x, f64 y, u8 airborne} carries exactly the fields the post-hit block produces.
  - The hold of 0x2A/0x9E/0x9F is 0x3DE, which equals the 990 ms decision period.
- So retail field monsters were owned by a server-side AI and replayed by each client under +0x971 = 1.

**Retail flow**
1. **Spawn.** 0x1A with server_controlled 0. Each client runs the wander AI locally.
2. **Player hits a mob.**
   - The attacker's client detects the hit, flinches the mob and shows the number, then reports it (the 61-byte C2S 0x0D interact).
   - The server subtracts HP and updates the hate list. It sets aggro to the attacker when he is the top damager or the current target is gone.
   - It sends 0x2A {mob, blob (current AI direction/motion plus reaction), target = attacker, x, y, airborne} to the viewers.
   - The attacker's client stops reading after target (target == own uid): the hit-lock clears, the reaction is dropped, direction and motion are kept, and it takes control.
   - Other clients read the position and play the reaction.
   - A kill sends 0x29 instead.
3. **Chase and attack.** The server AI re-decides about every 990 ms with the same rules as the client chase branch, and ships each decision as a 0x9F {mob, blob} node (hold 990, queued, +0x971 = 1), or as a 0x2A when it must take effect at once. The 2009 aggressive monsters (AI[5]) are taken over the same way when a player comes within 200 px.
4. **Monster hits a player.**
   - The victim's client detects it and reports C2S 0x0D with action 1..10 and event_source_uid = the mob.
   - The server computes the damage: Body_Atk for events 1/6, Weak_Atk for 7/8, Strong_Atk for 4/9/10.
   - It sends S2C 0x28 {u16 absolute HP} to the victim, or at 0 HP runs the death flow (S2C 0x3E).
   - The client never subtracts HP itself.
5. **Give up** (target dead, left, or out of the leash): 0x9E {mob, 8 zero bytes}. +0x971 = 0; after one 30 ms node the clients' own wander AI resumes, and it walks the mob back inside its 600 px home leash.
6. **Death:** 0x29 (clears +0x971 and +0x96E).

The wire bytes are identical in 2008 and 2009. Only the client internals differ: local uid at scene+0x220 vs +0x224, and the 0x9E hold is the constant 30 vs DAT_0054A284.

## recommendation

These changes go in C:\Users\ohdri\Desktop\WindSlayer2Game\server\windslayer_server.py. The wire bytes are the same for both builds. In the examples, mob uid 0x000F0000 is `00 00 0F 00` and the player (session uid 1) is `01 00 00 00`.

**The monster command (new helper `_mob_command(sock, session, mob, lo)`)**

It is the existing 16-byte 0x2A self-form, with the input in lo:

`P.send('0x2A', {'mover_uid': mob.uid, 'move_bits': struct.pack('<II', lo, 0), 'target_uid': P.session_uid(session)})`

What it does in the client:
- flushes the queue, so it takes effect on the next tick and never builds a backlog
- sets +0x971 = 1 and releases the hit-lock
- keeps direction and motion in the node
- holds for 960 ms, so the mob keeps moving if the server is a little late

Keep `_release_hit_lock` exactly as it is (lo = 0). It is the only release short of a kill, and it is also what takes control.

Always keep hi = 0, and never set bits 12-19 (action, reaction). With hi = 0 the hold is exactly 960 ms. Action bits would also trigger the extra read for events 9/10.

lo = direction | motion << 2:

| Command | Left | Right |
|---|---|---|
| walk | 01 | 02 |
| jump | 0D | 0E |
| drop | 11 | 12 |
| attack A | 05 | 06 |
| attack B | 15 | 16 |
| dash/skill | 19 | 1A |

Stop is 00. Example, walk right: payload `00 00 0F 00 02 00 00 00 00 00 00 00 01 00 00 00`.

**1. Monster fields**
- From the EN template (en_content NpcTemplate): ai flags (pad `tpl.ai` to 13), `weak_atk`, `strong_atk` (`body_atk` already exists).
- Per-mob state: `aggro_uid`, `hate {uid: dmg}`, `ai_dir` (1 or 2), `ai_lo`, `ai_sent_t`, `ai_hit_t`.
- Derived flags: `can_attack_a = ai[0]`, `skill = ai[3]`, `no_jump = ai[7]`, `can_attack_b = ai[8]`, `stationary = ai[9]`, and in 2009 `proximity = ai[5]`.

**2. Aggro**
- For every surviving hit: the `_resolve_hit(ack)` survivor, and survivors of skills, DoT and traps inside `_damage_monster` callers.
- Rules: `hate[attacker] += dmg`; `aggro_uid = attacker` if there is none yet, or the attacker is the top hater, or the current target is dead or offline. This mirrors 0x41A737..0x41A815. Set `ai_hit_t = now`.
- Skip stationary templates.
- Server-side hits (skills, DoT, `!trap`) set no client hit-lock, so also send `_mob_command(lo=0)` to take control. The ack path already sent the release.

**3. New 'monster-ai' timer**
- Run every `MOB_AI_TICK_SECS` = 0.3 on `self.ticks`, under the combat lock, like debuff-ticks.
- Positions: player from `session['pos']`; mob from `mob.x`/`mob.y`. In the dev setup `_track_driver_position` and `_track_driver_monsters` refresh both every 0.25 s from client memory (that already happens with `MOB_SERVER_CONTROLLED` false). Without the driver, dead-reckon at 82.5 px/s while walking and resync on every C2S 0x0D interact tail.
- Decide the input the way the client does (constants 65/140/200/20/40 from the exe):
  - dx = px - mx.
  - If |dx| > 65, dir = 2 when dx > 0, else 1. Otherwise keep `ai_dir`, so contact-only mobs walk through the player.
  - motion 3 if the player is more than 10 px higher and `no_jump` is 0.
  - motion 4 if the player is more than 140 px lower with |dx| < 200, or more than 20 px lower with |dx| < 40.
  - If `can_attack_a` or `can_attack_b`, and |dx| <= `MOB_ATTACK_REACH_X` (60) and |dy| <= 40: motion 1, or 5 when B is picked. Pick A if `can_attack_a` and (random bit or not `can_attack_b`).
  - Otherwise, if `skill`: motion 6.
  - Otherwise: walk.
- Never send 00 to a chasing mob. An idle node gets its hold zeroed, which stops ticking it (frozen, no contact detection).
- For a stunned mob (`debuffmod.controlled`), send 00.
- Send `_mob_command` when lo changes, or when at least 0.6 s have passed since the last send (keep-alive under the 960 ms hold).
- Check `mob.alive` inside the same critical section as the kill, so no command follows a 0x29.

**4. Release**
- When: the target is dead, not in world or on another map; or |mob.x - spawn_x| > `MOB_LEASH_PX` (600, the client's own leash constant @0x52EFC0); or `MOB_AGGRO_TIMEOUT_SECS` (15) have passed with no hit and the distance is over 400 px.
- Send `P.send('0x9E', {'mover_uid': mob.uid, 'move_bits': bytes(8)})`, payload `00 00 0F 00 00 00 00 00 00 00 00 00`, then clear the aggro state. The client wander resumes and walks the mob home.
- On `_player_death`, send 0x9E for every mob aggroed on that player.
- On a kill, clear the state only; 0x29 already resets +0x971.
- A map change needs no packet.

**5. Damage to the player**
- Default `MOB_CONTACT_DAMAGE` to true, in config.py and config.json.
- In `_monster_contact`, pick the stat by the reported event (bits 12-15 of C2S 0x0D state_lo, event_source_uid = a live mob of the session):
  - 1 or 6: `body_atk`
  - 7 or 8: `weak_atk`
  - 4, 9 or 10: `strong_atk`
  - 2, 3 or 5 (guarded): 0 or a reduced value
- Keep the `combat.body_damage(atk, def)` placeholder and send through `damage_player`, which sends S2C 0x28 {u16 hp}, or 0x3E at 0.
- Keep `CONTACT_MIN_SECS` 0.5 and the control check.
- Optionally accept events 7-10 only from a mob the server sent an attack motion in the last 1.5 s.
- A hit on the player does not change aggro.

**6. Config defaults**
- `MOB_SERVER_CONTROLLED`: false (unchanged)
- `MOB_CONTACT_DAMAGE`: true
- new: `MOB_AGGRO` true, `MOB_AI_TICK_SECS` 0.3, `MOB_CMD_KEEPALIVE_SECS` 0.6, `MOB_LEASH_PX` 600, `MOB_AGGRO_TIMEOUT_SECS` 15, `MOB_ATTACK_REACH_X` 60, `MOB_ATTACK_REACH_Y` 40
- 2008 needs no special case: the same packets work, and the self check reads scene+0x220.

**7. Fallback and later**
- If live test T2 shows that 0x2A does not carry motion, stream 0x1B after the release instead: {uid, hold 300, lo, hi 0} (`1B` payload `00 00 0F 00 2C 01 00 00 02 00 00 00 00 00 00 00`). Send the next node only when the previous hold has 30 ms or less left; that avoids the backlog (C3) and the freeze on expiry (C29).
- Retail-faithful alternative: 0x9F {uid, lo, 0} (hold 990, queued) once per 990 ms decision.
- Later, for multiplayer (P5): the target's client gets the 16-byte self-form. Other viewers get the 33-byte 0x2A {uid, lo, 0, target = aggro uid, f64 x, f64 y, u8 0}, or 0x1B once they are under control.

**Do not**
- Send 0x1A with server_controlled 1, or a 0x1B to a mob with +0x971 = 0. Both freeze it for the hold.
- Rely on the client patch (the JZ at 2009 file offset 0x1A6C8, rel32 `82 01` at 0x1A6CA; 2008 0x19CA5, `71 01` at 0x19CA7). It makes each client run its own, different AI, needs a 0x2A+0x9E pair per hit, and leaves the mob frozen in the hurt pose for about 1 s. It is not retail.

## proven_vs_inferred

**PROVEN by my own re-read of the asm (2009 first, [2008]):**
- AI gate 0x418B22..0x418B66 [0x41834B..0x418386].
- Chase branch 0x418C86..0x4191A6 [0x4184A4..0x4187B7] and its distance constants, read from the exe: 65, 140, 200, 20, 40, and the leash 600 @0x52EFC0.
- The complete writer list of +0xE44: 2009 has exactly 5 stores (0x41A815, 0x41930C, 0x4191A6, 0x4155D4, 0x487BD5 on a UI object); 2008 has exactly 3 (0x419DE1, 0x4187B7, 0x414CB4). No S2C handler writes it.
- The host gates 0x41A6C2/0x41A6C8/0x41A70E, 0x41927A, 0x413B1B [0x419C9F/0x419CA5/0x419CEB].
- scene+0xF40 [+0xF24] is only ever stored as 0, in the constructor (0x4128B3 [0x411FC3]). Caveat: a write through a computed or memcpy address cannot be fully excluded, though none was found.
- 0x2A/0x9E/0x9F handler 0x459C3D [0x454E6D]:
  - +0x971 set at 0x459C89 [0x454EB3]
  - flush and +0xEE4 = 0 only for 0x2A
  - hit-lock clear 0x459F04 [0x455136], with direction and motion kept
  - hold 990, 960 for 0x2A, 30 for 0x9E
- Consumer 0x4129F0 [0x412100] field mapping and the idle-zero rule.
- Tick gates 0x4142B8, 0x41602B, 0x416B50.
- 0x29 clears +0x96E/+0x971 (0x459B4A/0x459B51).
- Monster-to-local-player detection 0x416EFF..0x416F20, events 7/8 at 0x4172ED/0x4172F5, contact 6 at 0x416DD6.
- No field HP subtraction (0x41A6C2). The 270 ms i-frames (0x41531A).
- The template AI and stat copies (0x456C99 [0x451F67], 0x456B9E..0x456C10) and the loader layout (FUN_00409150 locals, template+0x254).
- The Ssiyo/Pupu flag values, read from both hni files.
- The event-to-stat table 0x41A954.
- The host-only 990 ms cadence, post-hit block and history ring, and that +0x972 has no writer in the client.
- PySlayer has no 0x2A/0x9E/0x9F/0x1B code (the import targets are missing files).

**LIVE-VERIFIED earlier (spec errata C29/C3, LIVE_TEST_LOG lines 220-222):**
- 0x1B motion codes; walk speed 82.5 px/s; physics stops when the hold ends.
- 0x2A takes effect at once, 0x9E/0x9F queue.
- The 16-byte 0x2A self-form releases the hit-lock on each surviving hit.

**INFERRED:**
- That a hit mob now stays frozen. It follows from the gates; check it live in T0.
- That the 0x2A self-form with lo != 0 walks or attacks the mob. It is statically proven, but no one has injected it yet (T2).
- That retail ran the host engine server-side and used 0x2A (post-hit), 0x9F (990 ms decisions) and 0x9E (hand-back).
- Per-template walk speed; attack reach; the damage formula; that events 8/10 deal Weak/Strong damage (the client estimate table gives them none); the meaning of AI[1], AI[2], AI[4] and AI[10..12].
- The leash and timeout values; the client chase itself has none.

## live_test_plan

- T0 baseline (2009 client; server unchanged; nothing injected). Run `python wsdev.py --build 2009 state` to get a wandering Ssiyo's address and uid (0x000F00xx). Hit it once so it survives (5 HP). Expected: the flinch plays, then it stands still and stops animating, and it never wanders again until you kill it. If you can read memory, read the mob's +0x971 (should be 1), +0xEE4 (0), +0xE44 (0) and +0x96D (0), and scene(*0x54F0C0)+0xF40 (0). That confirms Gates A and B.
- T1, take control without a hit. On a different wandering Ssiyo: `python wsdev.py --build 2009 send 2A 00 00 0F 00 00 00 00 00 00 00 00 00 01 00 00 00` (use its real uid). Expected: it stops wandering and freezes in idle within about 1 s (+0x971 = 1).
- T2, walk command (the key unproven step). Stand to its right and send `send 2A <uid> 02 00 00 00 00 00 00 00 01 00 00 00`. Expected: it faces right and walks about 0.96 s (about 79 px at 82.5 px/s), then stops. Repeat with 01: it walks left. Then send 02 five times, 0.5 s apart. Expected: one smooth walk of about 3 s with no stutter. Log the px moved per second for this template. If it does NOT move, use the 0x1B fallback: `send 1B <uid> 2C 01 00 00 02 00 00 00 00 00 00 00`.
- T3, contact damage. Set MOB_CONTACT_DAMAGE true in config.json and restart (one change only). Stand in the mob's path and walk it into you with repeated 02 commands. Expected: you flinch, then the server log shows `[DAMAGE] ... contact (C2S 0x0D action 6)` and sends 0x28, the HP globe drops by max(1, 3 - Def), at most one hit per 0.5 s. Also check that walking into a wandering, un-aggroed Ssiyo gives the same event 6 (that is retail body damage).
- T4, attack (needs a mob with attack flags, e.g. Rynx, template 12). Walk it next to you with 01/02 commands, then send lo 06 (attack A facing right) or 05 (facing left): `send 2A <uid> 06 00 00 00 00 00 00 00 01 00 00 00`. Expected: it swings for about 1 s; the player's C2S 0x0D shows action 7 (8 if airborne) with event_source_uid = the mob. Then try 16/15 for attack B: event 9. Optional: send 06 to a Ssiyo and note whether any swing animation or report appears (none is expected in retail).
- T5, release: `send 9E <uid> 00 00 00 00 00 00 00 00`. Expected: within about 1 s +0x971 goes back to 0 and the mob wanders again (the client wander, 600 px leash).
- T6, optional, and not with the server fix running: write the local uid (1) into +0xE44 of a wandering Ssiyo you never hit, using a small WriteProcessMemory snippet (there is no wsview command for it). Expected: the client's own chase starts. It faces you when |dx| > 65, walks through you, drops after you and never jumps. That is the exact behaviour the server AI must reproduce; compare it with T8.
- T7, 2008 client: repeat T1-T5 on WindSlayer_patched.exe with `--build 2008` (the default). The payload bytes are identical. The 2008 memory offsets are +0x8E4, +0xE48, +0xDB4, +0x8E1, +0x8E2, and scene(*0x70EECC)+0xF24. The 0x9E hold is a constant 30 ms.
- T8, after implementing the server AI: config MOB_AGGRO true, MOB_CONTACT_DAMAGE true. Hit a Ssiyo. Expected: after the flinch it walks toward you within about 0.3-0.6 s, bumps through you (contact damage and 0x28), and turns back once it is more than 65 px past you. It drops off ledges after you but never jumps. Run more than 600 px from its spawn, or stay unhit for 15 s: the server logs 0x9E and it wanders home. Kill it mid-chase: 0x29, exp and drop still work, and it respawns wandering. Die to it: 0x3E, and every aggroed mob gets 0x9E. Change one thing per run and restart the server in between (experiment discipline).