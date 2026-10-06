#!/usr/bin/env python3
"""
presence.py - other players on the same map (roadmap P5 stage 2)
=================================================================
    world-presence (+party-mp-presence, trade-mp-presence, social_friend-player-visibility)
    world-move-relay (+trade-move-relay)
    world-position-estimate (spike S-1, decision G1)

The world registry (world.py) knows who is on which map; this module makes the clients
know it too. Everything is driven by the F5 lifecycle hooks and by C2S 0x0D, in both client
builds (the record layouts come from records.py per build; 0x06 / 0x0D / 0x1B are identical
in 2008 and 2009, spec_2009 diff_vs_2008 "identical" / "wire identical"):

    on_enter_world(S)   S gets S2C 0x04 with the other players of its map (<= 5 records and
                        <= 2038 B per packet, world_movement_npc.md F1 step 5); every peer gets
                        S's record as S2C 0x05 (0x04 count 1 when the record carries what 0x05
                        cannot: a stall sign or a ground-point buff 0x0A31..0x0A3B, F1 step 9).
    on_map_change(S)    the old map's peers get S2C 0x06 {S.uid} (before S's lead packet is
                        even built, trade-mp-presence "0x06 to old-map peers before 0x08").
    on_leave_world(S)   the same 0x06 on disconnect, kick, idle reap, character delete.
    C2S 0x0D from S     relayed to S's peers as S2C 0x1B, NEVER to S (an echo of the mover's own
                        0x0D desynced its cipher: loading screen, menus, death - project
                        history; registry NEVER_REPLY 0x0D).

What each client has spawned (never a duplicate, never a dangling entity)
------------------------------------------------------------------------
No client handler de-duplicates uids (every 0x04/0x05 allocates a new entity, world doc
1.2) and a 0x06 with the receiver's own uid leaves scene+0x970 dangling (spec 0x06
gates_and_hazards). So the server keeps, per RECEIVER, the players its client holds:
session['spawned_players'] = {uid: the subject's session}, guarded by the receiver's
session['presence_lock'] and changed only together with the packet that changes the client:

  - a record goes out only when the uid is not spawned there yet (a stale entity of the same
    uid - a 2009 relogin's old session - is removed with 0x06 first);
  - 0x06 goes out only when the receiver holds exactly that subject session (so the old
    session of a 2009 relogin can never despawn its replacement: F5 `superseded`);
  - a 0x1B goes out only when the receiver holds the mover (spec 0x1B "Unknown uid: only the
    4-byte uid is consumed" would be safe, but a relay queued before the 0x05 is wasted);
  - the receiver's own map load (its 0x03 destroys every entity, world doc 1.1 step 4) clears
    the dict in on_map_change / on_leave_world, AFTER MapTransfer set in_world False and
    departed the map. Every sender re-checks reachable() and "same map" under the receiver's
    lock, so a push either lands before that 0x03 (and dies with the dict) or is skipped.
Two presence locks are never held at once, and nothing but the receiver's send_lock is taken
under one (world.py "Locks": the registry index lock is a leaf and is not held here).

Records (world-player-record, records.player_record(remote=True))
-----------------------------------------------------------------
Real cur_hp/cur_mp (0 renders a corpse), idle motion defaults (C12: remote records need
them), appearance + equipment (clothed), name tag, GM tag (2008 gm_level / 2009
gm_or_guild_id = 1 draws "Game Master"; no guilds exist yet), level from exp. The position
is the server's estimate with y put ON the floor line under it: a remote spawn record's y is
used verbatim and a remote entity does not fall until it runs a queued command (C2,
world_movement_npc#12 - the design's "spawn slightly above the floor so gravity settles it"
holds only for the LOCAL player). The records keep shop_open 0: a player selling at a stall
(market.py, P7 stage 2) is followed by its S2C 0x85 sign right after the record, under the
same receiver lock (sign_after_record; shop_storage-stall-presence, F14.2) - the 0x85 path is
the one live-verified to draw the signboard (C48, trade#16).

Observer broadcasts (P5 stage 4: item_inventory-observer-broadcast, lc-level-broadcast)
---------------------------------------------------------------------------------------
A change the other clients' copies must follow - equipment (S2C 0x1D / 0x1E / 0x24), the
level (S2C 0x22), an item buff (S2C 0x41) - goes out through to_holders(): the subject's
record revision is bumped (touch), then every peer whose client HOLDS the subject gets the
packet, decided and queued under that peer's presence lock like a 0x1B relay. A peer whose
client lacks the entity gets nothing: 0x1D / 0x1E / 0x24 / 0x41 would be no-ops there, but
0x22 queues its level-up effect before the entity lookup (spec 0x22 step 1), so a non-GM
client would flash the effect of a shadowed GM. The race this closes: a peer whose record
was built before the change and was still in flight when the change was broadcast (it did
not hold the subject yet, so it got no 0x1D) - spawn() and show_peers_to() compare the
revision they built the record at with the current one under the receiver's lock and
rebuild a stale record, so that peer spawns the subject with the new state instead.
A change with no side-effect-free packet - a level DOWN (0x22 always plays the level-up
effect and heal) - goes through reshow_to_holders(): touch, then 0x06 + a fresh record on
every holder.

Visibility and privacy
----------------------
None of the five refuse flags (privacy.py) is about being seen. The one visibility rule the
designs give is the GM shadow (chat_mail_gm F10 /shadow: hide = 0x06 to the map peers,
unhide = 0x05): a GM whose record says gm_hidden is not spawned on a non-GM client, and so
gets no 0x1B there either. Another GM's client gets the record (with gm_hidden, which makes
its render and sound paths skip the entity, chat_mail_gm 3.2).

Movement relay (world-move-relay, world_movement_npc.md F2; spec corrections C3/C12)
------------------------------------------------------------------------------------
Each accepted C2S 0x0D becomes ONE S2C 0x1B node for every peer that holds the mover:
{uid, hold_ms = min(logic_elapsed_ms, 990), state words, tails by ae / vb / ie}. The client
applies node k `hold_ms` after node k-1 and a type-3 entity simulates only while a node is
pending (C3: "0x1B must be streamed, hold = logic_elapsed_ms"; 2009 FUN_004129f0 and the
tick gates 0x412CB1 / 0x414278 / 0x415FEF / 0x416B0D: node k's input runs exactly while node
k+1's hold counts down, and the copy is frozen while its queue is empty), so the observer
replays the mover's own input timeline with its own physics - that, not the server's
estimate, is what keeps B's picture of A within a few px, and only while the relayed holds
add up to the mover's own logic time (POSITION_SYNC_RE 2026-09-28 R1).

The start hold (desync fix P1, config RELAY_START_HOLD_GAP_MS, default 450): a node gets the
30 ms START_HOLD_MS instead of its elapsed time only when the last relayed words were idle
(or nothing was relayed since the spawn), its own words are not, AND more than
RELAY_START_HOLD_GAP_MS passed. The builder (FUN_0042e1b0) also sends idle words while its
entity is still BUSY - an attack or cast animation after the key went up, the dash end, a
hurt slide, a fall - repeating every 210 ms (CMP 0xD2) until the entity is idle, then one
final idle packet and only the 15 s keepalive after it. So a gap over 450 ms after idle
words means that idle packet was the final one (the mover stood in state 8 with neutral
input for the whole gap: clamping loses nothing, and it still keeps a walk from starting
~1 s late on an observer whose copy has +0x904 12 from the last stop, world_movement_npc#23),
while a shorter gap is relayed exactly: clamping it cut up to 180 ms of simulation out of
"release a key, press another within 210 ms" (the copy left floating after jump + attack,
dashes and knockbacks short; live b_jatk1 / c_dash1 / g_kb3). A copy that really is idle
zeroes any hold on its next pass anyway (0x412ABC..0x412AF6). 0 = the legacy clamp after
every idle node.

The settle node (desync fix P2, config RELAY_SETTLE_NODE): once the mover's last relayed
words are idle and no C2S 0x0D came for RELAY_SETTLE_AFTER_MS (450), every holder gets
exactly once a duplicate of that final node with hold RELAY_SETTLE_HOLD_MS (990):
settle_node(). An idle copy zeroes the hold on its next pass (nothing visible); a copy left
in the air or mid-animation by any shortfall simulates the idle input until it lands or
finishes (then its idle check zeroes the rest), so no float outlasts the settle. It is
idle words only (never an ae or ie tail, which would replay a hit), never to the mover, and
decided under the mover's move lock (state['lock']) - the same lock relay() holds around its
decision and push - so a settle can never be queued behind a newer non-idle node. Lock order:
world_lock -> move lock -> the receiver's presence_lock -> send_lock.

lo is masked to bits 0-21 and hi to 0-11: the rest is stale scratch (scene+0xEF8) that the
observer's next own 0x0D would echo back. 0x2A is NOT used for players: it flushes the queue
and teleports to a server point (spec 0x2A C3), which would replace the exact stream with the
estimate. Pure idle keepalives (idle, no tails, the same words as the last relay,
logic_elapsed >= 15000) are not relayed (F2 step 5 option); the one or two idle packets
after a stop are, because they let a remote walker's +0x904 settle from 12 to 8
(world_movement_npc#23).

Cast animations (cs-cast-anim-relay, P6 stage 4)
------------------------------------------------
The caster's own client writes its cast pose (motion 7 + variant, +0x8B4 / +0x8B5) into its
local entity through its UDP loopback and sends it at once in a C2S 0x0D (trigger rule 1;
live 2009 capture lo 0x0010003C). relay() forwards it as an ordinary 0x1B node, so every
observer's copy plays the cast: no P2P path and no server-made packet is needed
(is_cast_pose; the relay logs "[MOVE] cast pose of ...").

Server keyframes (world-keyframe-2a, P6 stage 4)
------------------------------------------------
send_keyframe(): S2C 0x2A to the holders of a player (flush their copy's queue and put it at
a server point: GM moves, corrections), always the 33-byte form with target 0 and no action /
reaction nibble; send_stop(): S2C 0x9E zero blob. Never to the mover (its own queue is never
consumed). Players are still streamed with 0x1B; these are the exceptions (`!keyframe`).

Position estimate (world-position-estimate, spike S-1)
------------------------------------------------------
A walk step carries no coordinates (0x0D has pos only in the interact tail, both builds),
so session['pos'] is dead-reckoned from the state words:
  - the logic_elapsed_ms of a packet is the time the PREVIOUS packet's input was held;
  - while that input was left/right (bits 0-1 = 1/2) with motion walk (0) or jump (3) and no
    action event: x += dir * 0.25 px/ms * elapsed (live world_movement_npc#02: 250 px/s, 7.5
    px per 30 ms tick, scene clock 1.002 x wall; the 2009 logic tick is also 30 ms, LIVE
    2026-09-23 smoothness note; the 2009 walk speed is taken to be the same);
  - with POSITION_ESTIMATE_DASH_KNOCK (desync fix P3, on): a dash (motion 6 with a
    direction) moves 2.7 x the walk (20.25 px per tick) after a 60 ms wind-up for at most
    540 ms (18 ticks, 364.5 px; live session 3), counted across packets in
    state['dash_ms']; a knockback (the action nibble of the packet) moves 4 ticks x 7.5 px
    for actions 6 / 9 / 10 and 2 ticks for 7 / 8 in the facing2 direction (bits 20-21: 1 =
    -x, 2 = +x; live g_kb3: 30 and 15 px); a dash attack (motion 1 or 5 right after motion
    6) adds 37.5 px in the dash direction; and his own hurt (action 6 / 7 / 9 of his packet,
    hi its ms: state 3) moves him by that knockback only: neither a walk nor a dash word
    moves him for hitstun.hurt_len ms from that packet's tick (contact 250: 270 ms, a Monkey
    Soldier swing 810: 750), on the logic clock state['clock'], and a dash restarts at its
    end (live session 3: the direction words sent in the stun put the estimate up to 112 px
    off);
  - x stays inside the map's collision lines and y follows the EN floor lines
    (en_maps.MapData.floor_near: slopes up to the step, ledges down);
  - fixes: every interact tail pos (a hit report, a trap, ...) and every arrival point;
    the dev memory driver (DEV_MEMORY_COMBAT) is a fix too when POSITION_DRIVER_FIX is on,
    and only a measurement of the estimate's error when it is off - never a dependency.
Jumps onto a higher platform, skill lunges and item / buff speed are invisible to it until
the next fix: that is the error G1 bounds (LIVE_TEST_LOG "P5 stage 2" and
GameServer._dev_where).

Dropped connections
-------------------
A closed client resets its socket, so the connection's finally (on_leave_world, 0x06) runs
at once. A connection that dies silently (cable, frozen machine) is found by TCP keepalive
within DEAD_PEER_SECS (set_dead_peer_timeout), so its peers see it despawn within ~2 s
instead of after the 120 s recv timeout.
"""
import itertools
import logging
import math
import socket
import struct
import threading
import time

import en_maps
import hitstun as HS
import packets as P
import records as R
import world as worldmod

log = logging.getLogger('WS')

# --- C2S 0x0D state words (spec 0x42CE94/0x0D = 2009 0x42E704/0x0D, "identical") ---------
LO_MASK = 0x003FFFFF        # bits 22-31: stale scratch bytes of scene+0xEF8 (2009 +0xF10)
HI_MASK = 0x00000FFF        # hi bits 0-11 = +0x958; the rest is scratch
DIR_LEFT, DIR_RIGHT = 1, 2  # lo bits 0-1 (+0x8B3: wire 1 = 2 left, 2 = 6 right, 0/3 = 8 none)
MOTION_WALK, MOTION_ATTACK, MOTION_JUMP, MOTION_DOWN = 0, 1, 3, 4   # lo bits 2-4 (+0x8B4, C12)
MOTION_STRONG, MOTION_DASH = 5, 6   # strong attack (skill 88), dash (skill 80; POSITION_SYNC_RE 1)
MOTION_CAST = 7             # +0x8B4 7: the skill-cast pose (cs-cast-anim-relay, is_cast_pose)
MOVING_MOTIONS = (MOTION_WALK, MOTION_JUMP)
# S2C 0x1B hold_ms cap (world-move-relay: "hold_ms = min(logic_elapsed_ms, 990)"): a stalled
# client's long step must not park the observer's copy behind a 15 s delay.
HOLD_MAX_MS = 990
# The hold of the first node after the mover stood idle: one logic tick. The time he stood
# there moves nothing, and the queue consumer applies a node at once only to an entity whose
# +0x904 is back to 8 - a remote walker can keep 12 after its stop node (world_movement_npc
# #23), and a 990 hold would then start every walk ~1 s late on the observer's screen.
START_HOLD_MS = 30
# ... but only after a REAL idle gap (desync fix P1, config RELAY_START_HOLD_GAP_MS; module
# docstring "Movement relay"): a busy entity's builder repeats its idle words every 210 ms
# (FUN_0042e1b0 CMP 0xD2), so an idle node followed by more than this is the final idle
# packet. 450 = two busy sends plus frame hitches. 0 = clamp after every idle node (legacy).
START_HOLD_GAP_MS = 450
# The settle node (desync fix P2, config RELAY_SETTLE_NODE / _AFTER_MS / _HOLD_MS;
# settle_node()): the mover's last relayed words idle and no 0x0D for SETTLE_AFTER_MS -> one
# duplicate of that node with SETTLE_HOLD_MS to every holder.
SETTLE_AFTER_MS = 450
SETTLE_HOLD_MS = 990
# The client's idle keepalive (spec 0x0D trigger rule 5): an idle client sends every 15 s.
IDLE_KEEPALIVE_MS = 15000
# Live world_movement_npc#02: 250 px/s = 7.5 px per 30 ms logic tick.
WALK_PX_PER_MS = 0.25
# Dash, knockback and dash attack (desync fix P3, config POSITION_ESTIMATE_DASH_KNOCK;
# POSITION_SYNC_RE_2026-09-28 sections 1-3): state 6 moves 2.7 x the walk step (0x416403) =
# 20.25 px per 30 ms tick, after the state 5 wind-up (T[5] 50 ms: state 6 from the dash's
# tick + 60). State 6 runs on while +0xE24 < 500 and the motion is still 6 (0x4150F0):
# +0xE24 counts 30 a tick from 0 there, so it turns to state 7 on the 18th tick after the
# wind-up - 18 ticks of movement, 540 ms, 364.5 px. Live session 3 measured 364.5 px for
# three tapped dashes and a held one (3b_dash1..3, 3b_dashheld); the old 420 ms cap (14
# ticks, from live c_dash1) left the estimate 81 px short. The same for every class and
# level: the length is that constant, Dash (skill 0x50) has one level and FUN_004281b0
# only gates its start (motion case 6, state 8 / 0xC, 0x414887). What else changes it is
# not in the words: the +0x942 speed grade and a slow (+0x1280) scale the walk as well, a
# root in his buff slots (+0xF24: Arrow Grapple, Thornbush, Spider Web, Forced Blindfold)
# stops any dash, and scene+0xF18 5 (not the field, 6) lifts the cap.
DASH_PX_PER_MS = 2.7 * WALK_PX_PER_MS
DASH_WINDUP_MS = 60
DASH_MOVE_MS = 540
# A hit reaction slides the victim 7.5 px per tick in its facing2 direction (+0x95C -> +0x95B,
# 0x413BAF): 4 ticks for actions 6 / 9 / 10 (+0xE9C starts at 0), 2 for 7 / 8 (it starts at
# 0x3C). Live g_kb3: 30 px for action 6, 15 px for action 7, one action packet per hit.
KNOCK_TICK_PX = 7.5
KNOCK_TICKS = {6: 4, 9: 4, 10: 4, 7: 2, 8: 2}
# His own hurt (live session 3, config POSITION_ESTIMATE_DASH_KNOCK): pass 2 cases 6 / 7 / 9
# put him in state 3 from the tick of the packet that carries the action, for
# hitstun.hurt_len(action, hi) ms (contact 250: 270, a Monkey Soldier swing 810: 750; the
# hitstun tracker's busy window). FUN_00415f80 moves a body only in 6 / 0xC / the air or by
# the slide (+0x95B, the knockback above), and the motion case acts only in 8 / 0xC: a walk
# or dash word inside the hurt moves nothing, and a dash word still held at its end starts
# a new dash there (wind-up included). 8 / 10 launch him (state 0x17, which moves in the
# air with a direction key): not frozen.
STUN_ACTIONS = tuple(sorted(HS.HURT_EVENTS - set(HS.AIRBORNE_HURTS)))
# Motion 1 or 5 in state 6 (the dash itself: after the wind-up, before its end) slides at
# walk speed for ~150 ms (+0x95B = facing, cleared at 0x4158A3 / 0x415987): 37.5 px in the
# dash direction. In the wind-up (state 5) or the dash end (7) the attack input is refused.
DASH_ATTACK_PX = 37.5
# A single packet never reports more than this much walking: the client sends every ~210 ms
# while moving, so a bigger logic_elapsed_ms is a stall (minimised window, debugger).
MAX_STEP_MS = 5000
# y follows a slope up to one step's width (45 degrees) plus this slack for the per-tick
# rounding of the client's own collision.
SLOPE_SLACK_PX = 8.0

# S2C payload limit: 0x7FF packet size - 8 header - 1 opcode (world_movement_npc.md F1 step 5).
MAX_S2C_PAYLOAD = 0x7FF - 8 - 1

# session['move']['fix'] values
FIX_ARRIVAL, FIX_INTERACT, FIX_DRIVER, FIX_RECKONED = 'arrival', 'interact', 'driver', 'dead reckoning'

# Record revisions (module docstring "Observer broadcasts"): one server-wide counter, so every
# touch() gives the subject a value no earlier snapshot can hold (next() on a count is atomic
# under the GIL; two threads touching one subject never collapse into a stale equal value).
_REVISIONS = itertools.count(1)
# How often spawn() rebuilds a record that went stale while it was being built before it
# sends the last one anyway (a subject changing that fast is changing faster than any client
# could show it; the next change's broadcast reaches the receiver once it holds the entity).
STALE_RETRIES = 3


# ------------------------------------------------------------- state words ---
def masked(lo, hi=0):
    """(lo, hi) with the stale scratch bits cleared (F2 step 3)."""
    return int(lo or 0) & LO_MASK, int(hi or 0) & HI_MASK


def direction(lo):
    """-1 left, +1 right, 0 no horizontal input (lo bits 0-1)."""
    bits = int(lo) & 0x3
    return -1 if bits == DIR_LEFT else 1 if bits == DIR_RIGHT else 0


def motion(lo):
    return (int(lo) >> 2) & 0x7


def action(lo):
    """+0x94C action event (ae, bits 12-15): 1-5 attacks, 6-10 hits/grabs, 0xD death."""
    return (int(lo) >> 12) & 0xF


def interact(lo):
    """+0x950 interact event (ie, bits 16-19): 7 = the client's hit report, 0xC a trap."""
    return (int(lo) >> 16) & 0xF


def sub_b(lo):
    """+0x8B6 (vb, bits 9-11): 4 with action 7/9 adds the target point."""
    return (int(lo) >> 9) & 0x7


def is_idle(lo):
    """The client's own idle test (0x904 aside): no horizontal input, +0x8B4..+0x8B6 = 0, no
    action and no interact event. Bits 20-21 (+0x8D0) do not count."""
    lo = int(lo)
    return direction(lo) == 0 and (lo >> 2) & 0x3FF == 0 and action(lo) == 0 and interact(lo) == 0


def is_moving(lo):
    """Input that moves the body horizontally: left/right held while walking or in a jump,
    with no action event (an attack pose, a flinch or a grab does not walk)."""
    return direction(lo) != 0 and motion(lo) in MOVING_MOTIONS and action(lo) == 0


def facing2(lo):
    """-1 / +1 / 0: the knockback direction of a hit reaction (bits 20-21 = +0x95C / 2009
    node+0x50: 1 = left, 2 = right)."""
    bits = (int(lo) >> 20) & 0x3
    return -1 if bits == 1 else 1 if bits == 2 else 0


def is_dash(lo):
    """Dash words: motion 6 with a direction (0x19 left, 0x1A right)."""
    return motion(lo) == MOTION_DASH and direction(lo) != 0


# ------------------------------------------------------------- the relay ---
def start_hold_applies(prev_lo, lo, elapsed, gap_ms=START_HOLD_GAP_MS):
    """The P1 rule (module docstring "Movement relay"): the 30 ms start hold replaces the
    elapsed time only when (a) the last relayed words were idle or nothing was relayed since
    the spawn (prev_lo None), (b) these words are not idle and (c) more than `gap_ms` passed
    - the gap after the builder's FINAL idle packet. gap_ms <= 0: (c) always holds (the
    legacy clamp after every idle node)."""
    return ((prev_lo is None or is_idle(prev_lo)) and not is_idle(lo)
            and (gap_ms <= 0 or int(elapsed) > gap_ms))


def relay_fields(uid, rec, prev_lo=None, gap_ms=START_HOLD_GAP_MS):
    """S2C 0x1B fields for one decoded C2S 0x0D (world_movement_npc.md 1.4 field mapping):
    logic_elapsed_ms -> hold_ms (capped at 990; START_HOLD_MS when start_hold_applies: the
    last relayed words - prev_lo, None = none since the spawn - were idle, these are not and
    more than `gap_ms` passed), event_source_uid -> target_uid, f64_1338/1340 ->
    target_x/y, the interact tail's pos_x/pos_y -> pos_x/pos_y. The other interact-tail
    fields have no carrier in any EN TCP S2C (P2P path only)."""
    lo, hi = masked(rec.get('state_lo', 0), rec.get('state_hi', 0))
    elapsed = max(0, int(rec.get('logic_elapsed_ms', 0) or 0))
    hold = min(elapsed, HOLD_MAX_MS)
    if start_hold_applies(prev_lo, lo, elapsed, gap_ms):
        hold = min(hold, START_HOLD_MS)
    fields = {'uid': int(uid) & 0xFFFFFFFF, 'hold_ms': hold,
              'state_blob': struct.pack('<II', lo, hi)}
    ae, ie = action(lo), interact(lo)
    if ae:
        fields['target_uid'] = int(rec.get('event_source_uid', 0) or 0) & 0xFFFFFFFF
        if sub_b(lo) == 4 and ae in (7, 9):
            fields['target_x'] = float(rec.get('f64_1338', 0.0) or 0.0)
            fields['target_y'] = float(rec.get('f64_1340', 0.0) or 0.0)
    if ie:
        fields['pos_x'] = float(rec.get('pos_x', 0.0) or 0.0)
        fields['pos_y'] = float(rec.get('pos_y', 0.0) or 0.0)
    return fields


def is_keepalive(rec, last_relayed):
    """A pure idle keepalive that changes nothing on an observer (F2 step 5 option)."""
    lo, hi = masked(rec.get('state_lo', 0), rec.get('state_hi', 0))
    return (is_idle(lo) and (lo, hi) == last_relayed
            and int(rec.get('logic_elapsed_ms', 0) or 0) >= IDLE_KEEPALIVE_MS)


# ----------------------------------------------------------- the estimate ---
def _map_data(map_code):
    try:
        return en_maps.load_map(int(map_code))
    except Exception:                                          # noqa: BLE001 - no map, no settle
        log.debug(f'[MOVE] map {map_code}: no collision lines', exc_info=True)
        return None


def settle(map_code, x, y, rise=0.0):
    """(x, y) with x inside the map's line span and y on the floor line nearest to it (a
    slope up to `rise`, or the next floor down). Unchanged where the map has no lines."""
    data = _map_data(map_code)
    if data is None:
        return float(x), float(y)
    bounds = data.x_bounds()
    if bounds is not None:
        x = min(max(float(x), bounds[0]), bounds[1])
    floor = data.floor_near(x, y, rise)
    return float(x), float(y if floor is None else floor)


def floor_point(session):
    """Where a remote record puts this player: the estimate with y on the floor (C2)."""
    pos = session.get('pos') or (0.0, 0.0)
    code = session.get('current_map')
    if code is None:
        return float(pos[0]), float(pos[1])
    return settle(code, pos[0], pos[1], SLOPE_SLACK_PX)


def move_state(session):
    """session['move']: the estimator's and the relay's per-mover state."""
    state = session.get('move')
    if not isinstance(state, dict):
        state = session['move'] = {}
    return state


def reset_estimate(session, fix=FIX_ARRIVAL, now=None):
    """A map load put the player at session['pos'] standing still (the arrival point: the
    own 0x07 spawned him there, idle)."""
    now = time.monotonic() if now is None else now
    state = move_state(session)
    state.update({'lo': 0, 'hi': 0, 'relayed': None, 'fix': fix, 'fix_t': now, 'dash_ms': 0,
                  'stun_until': None})
    return state


def stunned_ms(state, elapsed):
    """How many of the first ms of a packet's `elapsed` (the logic window from the previous
    packet's tick, state['clock'] before it) he spent in his own hurt (state 3, STUN_ACTIONS:
    until state['stun_until'] on that clock). The hurt begins at a packet's own tick, so it
    is always a prefix of the later windows."""
    until = state.get('stun_until')
    if until is None:
        return 0
    return min(int(elapsed), max(0, int(until) - int(state.get('clock') or 0)))


def dash_knock_dx(state, prev, lo, elapsed, stunned=0):
    """The x a dash, a knockback or a dash attack adds for one packet (desync fix P3, module
    docstring "Position estimate"): `prev` the words held for `elapsed` ms, `lo` the new
    ones. Keeps the dash clock state['dash_ms'] (the ms since motion 6 started) across
    packets. `stunned`: the first ms of `elapsed` he spent in his own hurt (stunned_ms): the
    hurt ended any dash, the dash moves nothing there, and dash words still held when it
    ends start a new dash at that moment (the clock restarts, wind-up included)."""
    dx = 0.0
    stunned = min(int(elapsed), max(0, int(stunned or 0)))
    if is_dash(prev):
        t0 = 0 if stunned else int(state.get('dash_ms') or 0)
        t1 = t0 + int(elapsed) - stunned
        run = min(t1, DASH_WINDUP_MS + DASH_MOVE_MS) - max(t0, DASH_WINDUP_MS)
        dx += direction(prev) * DASH_PX_PER_MS * max(0, run)
        state['dash_ms'] = t1
        if (motion(lo) in (MOTION_ATTACK, MOTION_STRONG)
                and DASH_WINDUP_MS <= t1 <= DASH_WINDUP_MS + DASH_MOVE_MS):
            dx += direction(prev) * DASH_ATTACK_PX         # the dash attack: pressed in state 6
    if is_dash(lo) and not (is_dash(prev) and direction(prev) == direction(lo)):
        state['dash_ms'] = 0                               # a dash starts: its clock restarts
    ticks = KNOCK_TICKS.get(action(lo), 0)
    if ticks:
        dx += facing2(lo) * ticks * KNOCK_TICK_PX
    return dx


def advance(session, rec, now=None, *, dash_knock=True):
    """Follow one accepted C2S 0x0D: dead-reckon session['pos'] over the time the previous
    input was held (and, with `dash_knock` - config POSITION_ESTIMATE_DASH_KNOCK - a dash,
    a knockback, a dash attack: dash_knock_dx, and no walk or dash inside his own hurt:
    stunned_ms), then take the interact tail's point as a fix. Returns True when the player
    just stopped (for the stop log line)."""
    now = time.monotonic() if now is None else now
    state = move_state(session)
    stats = state.setdefault('stats', {'packets': 0, 'ae': 0, 'ie': 0})
    lo, hi = masked(rec.get('state_lo', 0), rec.get('state_hi', 0))
    prev = int(state.get('lo') or 0)
    elapsed = min(MAX_STEP_MS, max(0, int(rec.get('logic_elapsed_ms', 0) or 0)))
    stats['packets'] += 1
    stats['ae'] += bool(action(lo))
    stats['ie'] += bool(interact(lo))
    x, y = session.get('pos') or (0.0, 0.0)
    code = session.get('current_map')
    stunned = stunned_ms(state, elapsed) if dash_knock else 0
    state['clock'] = int(state.get('clock') or 0) + elapsed       # his logic clock, this tick
    extra = dash_knock_dx(state, prev, lo, elapsed, stunned) if dash_knock else 0.0
    if code is not None and (is_moving(prev) or extra or state.get('fix') == FIX_ARRIVAL):
        # Moving, or the first packet after an arrival (the arrival point is 100 px above the
        # portal floor and the player has fallen onto it by now).
        walk = direction(prev) * WALK_PX_PER_MS * (elapsed - stunned) if is_moving(prev) else 0.0
        dx = walk + extra
        x, y = settle(code, float(x) + dx, float(y), abs(dx) + SLOPE_SLACK_PX)
        session['pos'] = (x, y)
        if state.get('fix') == FIX_ARRIVAL:
            state['fix'] = FIX_RECKONED
    if dash_knock and action(lo) in STUN_ACTIONS:
        # His own hurt: state 3 from this packet's tick (pass 2 runs before the state machine).
        state['stun_until'] = state['clock'] + HS.hurt_len(action(lo), hi)
    if interact(lo) and 'pos_x' in rec:
        session['pos'] = (float(rec['pos_x']), float(rec['pos_y']))
        state['fix'], state['fix_t'] = FIX_INTERACT, now
    stopped = is_moving(prev) and not is_moving(lo)
    state['lo'], state['hi'] = lo, hi
    return stopped


def driver_sample(session, px, py, *, apply=True):
    """The dev memory driver read the client's true position: record how far the estimate
    was from it (spike S-1: the dead-reckoning error G1 is decided on) and, with `apply`
    (config POSITION_DRIVER_FIX), make it the position."""
    est = session.get('pos')
    state = move_state(session)
    if est is not None:
        err = math.hypot(float(est[0]) - px, float(est[1]) - py)
        e = state.setdefault('error', {'n': 0, 'sum': 0.0, 'max': 0.0, 'last': 0.0})
        e['n'] += 1
        e['sum'] += err
        e['last'] = err
        e['max'] = max(e['max'], err)
    if apply:
        session['pos'] = (float(px), float(py))
        state['fix'], state['fix_t'] = FIX_DRIVER, time.monotonic()


def describe(session, now=None):
    """The `!where` lines of one player (each fits the 87-byte S2C 0x15 text): the estimate,
    the floor y a remote record carries, the last fix and its age; then the spike S-1
    counts - C2S 0x0D packets, how many carried an action tail (ae) and how many the
    interact position tail (ie) - and, while the dev driver samples that client, the
    estimate's error against its memory."""
    now = time.monotonic() if now is None else now
    pos = session.get('pos') or (0.0, 0.0)
    state = move_state(session)
    fy = floor_point(session)[1]
    age = now - float(state.get('fix_t') or now)
    stats = state.get('stats') or {}
    lines = [f'{session.get("char_name") or session.get("username")} uid {session.get("uid")} '
             f'map {session.get("current_map")} ({pos[0]:.0f},{pos[1]:.0f}) floor {fy:.0f} '
             f'fix {state.get("fix", "-")} {age:.1f}s ago']
    tail = (f'  0x0D {stats.get("packets", 0)} (ae {stats.get("ae", 0)}, ie {stats.get("ie", 0)})')
    err = state.get('error')
    if err and err['n']:
        tail += (f'; err last {err["last"]:.0f} max {err["max"]:.0f} mean '
                 f'{err["sum"] / err["n"]:.0f} px (n {err["n"]})')
    lines.append(tail)
    return lines


# ------------------------------------------------------ who holds whom ---
def _lock(session):
    lock = session.get('presence_lock')
    if lock is None:
        lock = session.setdefault('presence_lock', threading.RLock())
    return lock


def spawned(session):
    """{uid: subject session} of the players this session's client has spawned (a copy)."""
    with _lock(session):
        return dict(session.get('spawned_players') or {})


def _held(receiver):
    held = receiver.get('spawned_players')
    if held is None:
        held = receiver['spawned_players'] = {}
    return held


def visible_to(subject, receiver):
    """False for a GM in shadow (gm_hidden) and a receiver that is no GM (module docstring)."""
    return not (subject.get('gm') and subject.get('gm_hidden')) or bool(receiver.get('gm'))


def _same_map(server, a, b):
    code = server.world.map_of(a)
    return code is not None and code == server.world.map_of(b)


def _can_show(server, subject, receiver):
    """Re-checked under the receiver's lock: both in world on the same map (the registry, not
    a snapshot taken before), different uids, and the receiver may see the subject. Neither
    may be in the Item Mall (premium_cash-presence, P8 stage 4: a player in the mall is
    hidden from his map until his own EXIT - mall.py "Presence"; the mall already takes him
    off the map, this keeps it true whatever else runs)."""
    return (subject is not receiver and worldmod.reachable(subject) and worldmod.reachable(receiver)
            and not subject.get('in_cash_shop') and not receiver.get('in_cash_shop')
            and subject.get('uid') is not None and subject.get('uid') != receiver.get('uid')
            and _same_map(server, subject, receiver) and visible_to(subject, receiver))


def record_of(server, subject):
    """The subject's player row (0x07/0x04 names, this server's build) for another client,
    at its estimated position on the floor. None when it has no character."""
    char = server._session_char(subject)
    if char is None:
        return None
    return R.player_record(subject, char, server._session_account(subject), remote=True,
                           pos=floor_point(subject), client_build=server.client_build)


def needs_list_form(rec):
    """True when 0x05 would lose part of the record: its stall block or a buff's ground point
    (F1 step 9: send 0x04 with count 1 instead)."""
    return bool(rec.get('shop_open')) or any(
        b.get('buff_skill_id') in R.BUFF_POINT_IDS for b in rec.get('repeat[buff_count]') or [])


def _appear_packet(server, rec):
    if needs_list_form(rec):
        return '0x04', R.player_list(rec)
    return '0x05', R.to_0x05(rec, server.client_build)


def spawn(server, subject, receiver, rec=None, rev=None):
    """Put the subject on the receiver's client (0x05, or 0x04 count 1) unless it is there
    already. Returns True when a record was sent. The record is built before the receiver's
    lock is taken (it reads the store), the decision and the send happen under it. `rec` /
    `rev`: a record the caller built and the subject's revision() it was built at (one
    record for every peer of an entrant); without `rev` it is rebuilt here. A record whose
    revision is no longer the subject's (an equipment / level / buff broadcast ran while it
    was built) is rebuilt, up to STALE_RETRIES times (module docstring "Observer
    broadcasts")."""
    uid = subject.get('uid')
    if rev is None:
        rec = None
    for attempt in range(STALE_RETRIES + 1):
        if not _can_show(server, subject, receiver) or spawned(receiver).get(uid) is subject:
            return False
        if rec is None:
            rev = revision(subject)
            rec = record_of(server, subject)
            if rec is None:
                return False
        key, fields = _appear_packet(server, rec)
        with _lock(receiver):
            if not _can_show(server, subject, receiver):
                return False
            held = _held(receiver)
            shown = held.get(uid)
            if shown is subject:
                return False
            if revision(subject) != rev and attempt < STALE_RETRIES:
                rec = None                # changed under us: rebuild outside the lock
                continue
            if shown is not None:
                # A stale entity with this uid (the old session of a 2009 relogin): its 0x06
                # first, or the receiver would hold two entities with one uid.
                server._push(receiver, '0x06', {'uid': uid}, 'PRESENCE')
                del held[uid]
            if not server._push(receiver, key, fields, 'PRESENCE'):
                return False
            held[uid] = subject
            sign_after_record(server, subject, receiver)
        log.info(f'[PRESENCE] {_who(subject)} appears on {_who(receiver)} ({key} at '
                 f'{rec["pos_x"]:.0f},{rec["pos_y"]:.0f})')
        return True
    return False


def despawn(server, subject, receiver):
    """Remove the subject from the receiver's client (0x06) if - and only if - the receiver
    holds exactly that subject session. Returns True when a 0x06 was sent."""
    uid = subject.get('uid')
    with _lock(receiver):
        held = _held(receiver)
        if uid is None or held.get(uid) is not subject:
            return False
        del held[uid]
        if not worldmod.reachable(receiver):
            return False                  # its own map load clears the client anyway
        sent = server._push(receiver, '0x06', {'uid': uid}, 'PRESENCE')
    if sent:
        log.info(f'[PRESENCE] {_who(subject)} leaves {_who(receiver)} (0x06)')
    return sent


def show_peers_to(server, session):
    """S2C 0x04 to `session` with the records of the players of its map it does not hold
    yet, packed <= 5 records and <= MAX_S2C_PAYLOAD bytes per packet. Returns the count."""
    held_now = spawned(session)
    pairs = []
    for peer in server.world.peers(session):
        if held_now.get(peer.get('uid')) is peer or not _can_show(server, peer, session):
            continue
        rev = revision(peer)
        rec = record_of(server, peer)
        if rec is not None:
            pairs.append((peer, rec, rev))
    shown = 0
    stale = []
    with _lock(session):
        held = _held(session)
        todo = []
        for peer, rec, rev in pairs:
            if held.get(peer.get('uid')) is peer or not _can_show(server, peer, session):
                continue                  # a concurrent 0x05 got there first, or it left
            if revision(peer) != rev:
                stale.append(peer)        # changed while its row was built: spawn() below
                continue
            if held.pop(peer.get('uid'), None) is not None:
                server._push(session, '0x06', {'uid': peer.get('uid')}, 'PRESENCE')
            todo.append((peer, rec))
        for batch in _batches(server, session, todo):
            if not server._push(session, '0x04', R.player_list([rec for _, rec in batch]), 'PRESENCE'):
                break
            for peer, _ in batch:
                held[peer.get('uid')] = peer
                shown += 1
                sign_after_record(server, peer, session)
    for peer in stale:
        shown += bool(spawn(server, peer, session))       # rebuilt, as its own 0x05
    if shown:
        log.info(f'[PRESENCE] {_who(session)} sees {shown} player(s) on map '
                 f'{session.get("current_map")} (0x04)')
    return shown


def _batches(server, receiver, pairs):
    """Split (subject, row) pairs into 0x04 packets: at most R.MAX_RECORDS_PER_PACKET rows
    and MAX_S2C_PAYLOAD bytes each (a 2009 row with buffs and 30 skills is ~650 B)."""
    uid = P.session_uid(receiver)
    out, cur = [], []
    for pair in pairs:
        trial = cur + [pair]
        fits = len(trial) <= R.MAX_RECORDS_PER_PACKET and len(P.build(
            '0x04', R.player_list([rec for _, rec in trial]), receiver_uid=uid,
            client_build=server.client_build)) <= MAX_S2C_PAYLOAD
        if cur and not fits:
            out.append(cur)
            cur = [pair]
        else:
            cur = trial
    if cur:
        out.append(cur)
    return out


def sign_after_record(server, subject, receiver):
    """shop_storage-stall-presence (F14.2, fixes B13): the S2C 0x85 {uid, sprite, title} of
    the subject's open stall, queued right after the subject's record on the receiver (caller
    holds the receiver's presence lock). market.Market.sign_fields takes no lock, so nothing
    but the receiver's send_lock is taken under the presence lock. A stall that opens or
    closes concurrently is ordered by that lock: its 0x85 / 0x86 broadcast reaches the
    receiver after this record (the receiver then holds the subject), so the last packet the
    client gets always matches the registry (market.py "Presence")."""
    market = getattr(server, 'market', None)
    fields = market.sign_fields(subject) if market is not None else None
    if fields is not None:
        server._push(receiver, '0x85', fields, 'STALL')
    return fields is not None


def clear(session):
    """The session's own client lost every remote entity (its 0x03 map load / it left)."""
    with _lock(session):
        held = session.get('spawned_players')
        if held:
            held.clear()


# ------------------------------------------------- observer broadcasts ---
def revision(subject):
    """The subject's record revision (0 until its first touch)."""
    return int(subject.get('presence_rev') or 0)


def touch(subject):
    """The subject's record changed (worn gear, level, an item buff): a record of it built
    before this call is stale (module docstring "Observer broadcasts"). Call it AFTER the
    model change - a record built after the new revision was read then has the change."""
    subject['presence_rev'] = next(_REVISIONS)


def holders(server, subject):
    """The peers whose client holds the subject's entity right now (a snapshot; the ground
    code gives these the 0x12 that falls at their own copy of a dropper)."""
    uid = subject.get('uid')
    out = []
    if uid is None:
        return out
    for peer in server.world.peers(subject):
        with _lock(peer):
            if _held(peer).get(uid) is subject and worldmod.reachable(peer):
                out.append(peer)
    return out


def to_holders(server, subject, key, fields, tag='PRESENCE'):
    """touch(subject), then S2C `key` to every peer whose client holds the subject - the
    observer half of an equipment / level / item-buff change (P5 stage 4). The decision and
    the send happen under the receiver's presence lock, the book spawn() / despawn() keep, so
    a peer gets it exactly when its client has the entity; a peer whose record is still in
    flight rebuilds it with the change instead (touch). Never the subject itself: its own
    packet (0x1D, 0x42, ...) or none (0x22: the owner levelled itself on 0x21) is the
    caller's. Returns how many peers got it."""
    touch(subject)
    return _to_holders_only(server, subject, key, fields, tag)


def reshow_to_holders(server, subject):
    """touch(subject), then replace the subject's entity on every peer whose client holds it:
    its 0x06, then a fresh record (spawn) built after the touch. For a change no in-place
    packet can show without a side effect - a level-DOWN, since 0x22 plays the level-up
    effect and heal on every receipt (livetest bug 8: observers kept the old level). A peer
    the subject stops being visible to between the two (it left the map) keeps only the
    0x06, which is what it needed anyway. Never the subject. Returns how many peers got the
    new record."""
    touch(subject)
    shown = 0
    for peer in holders(server, subject):
        if despawn(server, subject, peer):
            shown += bool(spawn(server, subject, peer))
    return shown


def to_map_holders(server, subject, map_code, key, fields, tag='PRESENCE'):
    """touch(subject), then S2C `key` to every session on `map_code` whose client holds the
    subject - to_holders for a subject that may already be OFF that map (the stall sign
    removal 0x86 on disconnect runs in on_leave_world, after the subject departed, but
    before presence's own 0x06 there). Never the subject. Returns how many peers got it."""
    touch(subject)
    uid = subject.get('uid')
    if uid is None or map_code is None:
        return 0
    sent = 0
    for peer in server.world.map_sessions(map_code, exclude=subject):
        with _lock(peer):
            if _held(peer).get(uid) is subject and worldmod.reachable(peer):
                sent += bool(server._push(peer, key, fields, tag))
    return sent


def _config(server, key, default):
    cfg = getattr(server, 'config', None)
    return cfg.get(key, default) if cfg is not None else default


def move_lock(session):
    """The mover's move lock (state['lock'], desync fix P2): relay() holds it around its
    decision and push, settle_node() around its check and push. Taken under the world lock at
    most, never under a presence lock or a send_lock (module docstring "Movement relay").
    Creates session['move'] and the lock when missing - so only the session's own paths call
    it; the settle tick uses the lock relay() made or skips the mover (settle_node)."""
    state = move_state(session)
    lock = state.get('lock')
    if lock is None:
        lock = state.setdefault('lock', threading.Lock())
    return lock


def relay(server, session, rec, now=None):
    """C2S 0x0D -> S2C 0x1B for every peer whose client holds the mover. Never the mover.
    Every accepted packet - a keepalive it drops too - stamps state['rx_t'] (the settle
    clock); a relayed one re-arms the settle node (state['settled'] False). Returns how many
    peers got it."""
    uid = session.get('uid')
    if uid is None or not worldmod.reachable(session):
        return 0
    now = time.monotonic() if now is None else now
    gap = int(_config(server, 'RELAY_START_HOLD_GAP_MS', START_HOLD_GAP_MS))
    state = move_state(session)
    with move_lock(session):
        state['rx_t'] = now
        last = state.get('relayed')
        if is_keepalive(rec, last):
            return 0
        words = masked(rec.get('state_lo', 0), rec.get('state_hi', 0))
        state['relayed'] = words
        state['settled'] = False
        prev = None if last is None else last[0]
        fields = relay_fields(uid, rec, prev, gap)
        sent = 0
        for peer in server.world.peers(session):
            with _lock(peer):
                if _held(peer).get(uid) is session and worldmod.reachable(peer):
                    sent += bool(server._push(peer, '0x1B', fields, 'MOVE'))
    # The words decided under the lock, never state['relayed'] again: once the lock is let go
    # a map transfer on another thread (a GM's !warp -> on_enter_world -> reset_estimate) may
    # have set it to None already.
    lo = words[0]
    elapsed = max(0, int(rec.get('logic_elapsed_ms', 0) or 0))
    if (gap > 0 and sent and start_hold_applies(prev, lo, elapsed, 0)
            and fields['hold_ms'] > START_HOLD_MS):
        # desync fix P1: the line a live check looks for - a node after BUSY idle words that
        # the legacy clamp would have cut to 30 ms keeps the mover's own time.
        log.info(f'[MOVE] start hold kept: {_who(session)} lo {lo:#x} after idle words '
                 f'{elapsed} ms <= {gap} ms -> hold {fields["hold_ms"]} (legacy {START_HOLD_MS}), '
                 f'0x1B to {sent} peer(s)')
    if is_cast_pose(lo) and (last is None or not is_cast_pose(last[0])):
        # cs-cast-anim-relay: the one line a live check looks for (P6 exit criterion 6).
        log.info(f'[MOVE] cast pose of {_who(session)} (variant {cast_variant(lo)}, lo {lo:#x}) '
                 f'relayed as 0x1B to {sent} peer(s)')
    return sent


def settle_fields(uid, words, hold_ms=SETTLE_HOLD_MS):
    """The settle node (desync fix P2): S2C 0x1B {uid, hold, the last relayed lo, hi} - the
    mover's final idle node again with a long hold, no tail (idle words carry no ae / ie).
    uid 1, words 0/0, hold 990: 01 00 00 00 DE 03 00 00 00 00 00 00 00 00 00 00."""
    lo, hi = masked(*words)
    return {'uid': int(uid) & 0xFFFFFFFF, 'hold_ms': int(hold_ms),
            'state_blob': struct.pack('<II', lo, hi)}


def settle_node(server, mover, now=None):
    """Desync fix P2 (config RELAY_SETTLE_NODE, module docstring "Movement relay"): when the
    mover's last relayed words are idle and no C2S 0x0D arrived for RELAY_SETTLE_AFTER_MS,
    send every peer that holds the mover the settle node (settle_fields: that node again
    with hold RELAY_SETTLE_HOLD_MS), exactly once per stop - relay() re-arms it. A copy that
    is idle zeroes the hold on its next pass; one left in the air or mid-animation by any
    shortfall simulates the idle input until it lands, then zeroes it. Never the mover
    (_to_holders_only), never ae / ie words. The check and the push run under the mover's
    move lock, as relay()'s decision and push do, so a settle never lands behind a newer
    non-idle node. Runs on the tick thread, so it creates nothing: a mover with no move state
    or no move lock yet - not in the world yet (on_enter_world's reset_estimate has not run)
    or nothing relayed since - has nothing to settle and is skipped untouched ('relayed' is
    only ever set by relay(), under that lock). Returns how many peers got it."""
    uid = mover.get('uid')
    if uid is None or not bool(_config(server, 'RELAY_SETTLE_NODE', True)):
        return 0
    state = mover.get('move')
    lock = state.get('lock') if isinstance(state, dict) else None
    if lock is None:
        return 0
    now = time.monotonic() if now is None else now
    after = float(_config(server, 'RELAY_SETTLE_AFTER_MS', SETTLE_AFTER_MS)) / 1000.0
    hold = int(_config(server, 'RELAY_SETTLE_HOLD_MS', SETTLE_HOLD_MS))
    with lock:
        last, rx_t = state.get('relayed'), state.get('rx_t')
        if (last is None or state.get('settled') or rx_t is None or not is_idle(last[0])
                or now - float(rx_t) < after):
            return 0
        state['settled'] = True
        fields = settle_fields(uid, last, hold)
        sent = _to_holders_only(server, mover, '0x1B', fields, 'MOVE')
        quiet_ms = (now - float(rx_t)) * 1000.0
    if sent:
        log.info(f'[MOVE] settle node: {_who(mover)} quiet {quiet_ms:.0f} ms after idle words lo '
                 f'{last[0]:#x} hi {last[1]}: 0x1B hold {hold} to {sent} peer(s)')
    return sent


def is_cast_pose(lo):
    """The cast-animation words of C2S 0x0D (cs-cast-anim-relay): motion (+0x8B4, bits 2-4) 7
    with the variant in +0x8B5 (bits 5-8). The client writes them itself - 2008 from the
    S2C 0x25 / 0x3B accept (scene+0x255 = 7, +0x256 = variant), 2009 when it SENDS C2S 0x15
    (FUN_0044F070: scene+0x259 / +0x25A) - into the local entity through its own UDP loopback
    (UDP 0x03 input_a = action*10 + dir, FUN_00423850 -> +0x8B3..+0x8B6), and the change sends
    an immediate 0x0D (trigger rule 1). Live 2009 capture 2026-09-24 00:05:01 (Ice Spear):
    lo 0x0010003C = motion 7, variant 1, then idle 60 ms later. relay() forwards it as an
    ordinary 0x1B node, which sets +0x8B4 / +0x8B5 on every observer's copy - the same fields
    the P2P input path writes in a room - so the observers play the caster's animation with
    no packet of the server's own."""
    return motion(lo) == MOTION_CAST


def cast_variant(lo):
    return (int(lo) >> 5) & 0xF


# ------------------------------------------- server keyframes (world-keyframe-2a) ---
# world_movement_npc.md F8. The mover's OWN client is never sent either packet: its own uid's
# queue is never consumed (FUN_00412100 skips uid == scene+0x220), so a node would leak and
# a 0x2A would teleport it under its own input (spec 0x2A / 0x9E hazards). Only the peers
# that HOLD the mover get them (the relay's rule), so the entity lookup always succeeds and
# the grammar's position block is always read.
KEYFRAME_LO_MASK = LO_MASK & ~0x000FF000     # no action (bits 12-15) and no reaction (16-19)


def keyframe_fields(uid, x, y, lo=0, hi=0):
    """S2C 0x2A {mover_uid, move_bits, target_uid 0, pos_x, pos_y, airborne 0}: always the
    33-byte form (F8 "always append the 17-byte position block"). target_uid 0 is never a
    receiver's uid, so every receiver reads the block (a receiver on whose screen the mover is
    dead reads nothing after target_uid, and the trailing 17 bytes are ignored). The words are
    masked like a relay's and lose their action and reaction nibbles: a reaction (ie) nibble
    makes the queue consumer copy the node's zeroed x/y into the entity - a snap to (0,0) -
    and an action 9/10 would make the handler read a u8 action_flag first."""
    lo, hi = masked(lo, hi)
    return {'mover_uid': int(uid) & 0xFFFFFFFF, 'move_bits': struct.pack('<II', lo & KEYFRAME_LO_MASK, hi),
            'target_uid': 0, 'pos_x': float(x), 'pos_y': float(y), 'airborne': 0}


def stop_fields(uid):
    """S2C 0x9E {mover_uid, 8 zero bytes} (12 B): neutral input queued behind the pending
    nodes (C3: NOT immediate - use a keyframe to stop at once) and +0x8E4 / 2009 +0x971
    cleared at receipt, which the keyframe had set."""
    return {'mover_uid': int(uid) & 0xFFFFFFFF, 'move_bits': bytes(8)}


def _to_holders_only(server, mover, key, fields, tag):
    uid = mover.get('uid')
    if uid is None or not worldmod.reachable(mover):
        return 0
    sent = 0
    for peer in server.world.peers(mover):
        with _lock(peer):
            if _held(peer).get(uid) is mover and worldmod.reachable(peer):
                sent += bool(server._push(peer, key, fields, tag))
    return sent


def send_keyframe(server, mover, x=None, y=None, lo=None, hi=None):
    """world-keyframe-2a: put every observer's copy of `mover` at (x, y) at once - S2C 0x2A
    flushes that copy's command queue and writes the position immediately (spec 0x2A, C3) -
    for GM moves, server corrections and drift fixes. x / y default to the server's point of
    the mover (floor_point: the estimate on the floor), lo / hi to its last relayed words (a
    walking copy keeps walking; 0 = idle). Never the mover. Returns how many peers got it."""
    uid = mover.get('uid')
    if uid is None:
        return 0
    if x is None or y is None:
        x, y = floor_point(mover)
    last = move_state(mover).get('relayed') or (0, 0)
    fields = keyframe_fields(uid, x, y, last[0] if lo is None else lo, last[1] if hi is None else hi)
    sent = _to_holders_only(server, mover, '0x2A', fields, 'MOVE')
    log.info(f'[MOVE] keyframe {_who(mover)} -> ({float(x):.0f},{float(y):.0f}) '
             f'lo {struct.unpack("<I", fields["move_bits"][:4])[0]:#x}: 0x2A to {sent} peer(s)')
    return sent


def send_stop(server, mover):
    """world-keyframe-2a: S2C 0x9E zero blob about `mover` to its holders (never the mover):
    a neutral node after the pending ones, and the copy's server-controlled flag cleared.
    Returns how many peers got it."""
    uid = mover.get('uid')
    if uid is None:
        return 0
    sent = _to_holders_only(server, mover, '0x9E', stop_fields(uid), 'MOVE')
    log.info(f'[MOVE] stop {_who(mover)}: 0x9E to {sent} peer(s)')
    return sent


# ---------------------------------------------------------------- hooks ---
def on_enter_world(server, session, map_code=None, **_):
    """F5 on_enter_world (after the own 0x07, session already in peers()): the estimate
    restarts at the arrival point, the session sees its map (0x04), its map sees it (0x05)."""
    reset_estimate(session)
    show_peers_to(server, session)
    rec = rev = None
    for peer in server.world.peers(session):
        if rec is None:
            rev = revision(session)
            rec = record_of(server, session)
            if rec is None:
                return
        spawn(server, session, peer, rec, rev)


def on_map_change(server, session, old_map=None, **_):
    """F5 on_map_change (in_world False, off every map): its client's coming 0x03 destroys
    every remote entity, and the old map's clients lose it (0x06)."""
    clear(session)
    for peer in server.world.map_sessions(old_map) if old_map is not None else ():
        despawn(server, session, peer)


def on_leave_world(server, session, map_code=None, **_):
    """F5 on_leave_world (disconnect, kick, idle reap, delete): 0x06 on the map it was on. A
    superseded session (2009 relogin) is harmless here: despawn() only removes the entity a
    peer holds for THIS session, never the replacement's."""
    clear(session)
    for peer in server.world.map_sessions(map_code) if map_code is not None else ():
        despawn(server, session, peer)


def register(hooks):
    """Register the presence callbacks on a world.Hooks (GameServer.__init__)."""
    hooks.register(worldmod.ON_MAP_CHANGE, on_map_change)
    hooks.register(worldmod.ON_ENTER_WORLD, on_enter_world)
    hooks.register(worldmod.ON_LEAVE_WORLD, on_leave_world)


# ------------------------------------------------------ dead connections ---
def set_dead_peer_timeout(sock, secs):
    """TCP keepalive so a silently dropped client is detected within ~secs (recv then fails
    and the connection's finally despawns it). Windows: SIO_KEEPALIVE_VALS (idle, interval;
    Vista+ sends 10 probes) plus TCP_MAXRT for a connection that dies while the server is
    sending; elsewhere TCP_KEEPIDLE/INTVL/CNT and TCP_USER_TIMEOUT. The probes are answered
    by the peer's TCP stack, so a client busy in a map load (its network thread sleeps ~1.5 s)
    is never taken for dead. Best effort: False when the platform refuses."""
    try:
        secs = float(secs or 0)
        if secs <= 0:
            return False
        idle_ms = max(1, int(secs * 500))
        interval_ms = max(1, idle_ms // 10)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, 'SIO_KEEPALIVE_VALS'):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle_ms, interval_ms))
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, 'TCP_MAXRT', 5), max(1, math.ceil(secs)))
            except OSError:
                pass
        else:
            for name, value in (('TCP_KEEPIDLE', max(1, idle_ms // 1000)),
                                ('TCP_KEEPINTVL', max(1, interval_ms // 1000)),
                                ('TCP_KEEPCNT', 10),
                                ('TCP_USER_TIMEOUT', int(secs * 1000))):
                if hasattr(socket, name):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
        return True
    except (OSError, AttributeError, ValueError, TypeError) as e:
        log.debug(f'[PRESENCE] dead-peer keepalive not set: {e}')
        return False


def _who(session):
    return f'{session.get("char_name") or session.get("username") or "?"}/uid {session.get("uid")}'
