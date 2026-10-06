#!/usr/bin/env python3
"""
config.py - server configuration loader (roadmap 1.12 F12: arch-config)
=======================================================================
One JSON file (server/config.json) overrides the defaults below; every key is optional.

    import config
    cfg = config.load()                    # server/config.json (or $WS_CONFIG), defaults if absent
    cfg.GAME_PORT, cfg.PUBLIC_IP           # attribute access
    cfg.accounts_path                      # ACCOUNTS_FILE resolved next to the config file
    cfg.public_ip_host_order()             # u32 for the S2C 0x01 channel entry
    config.defaults()                      # pure defaults, no file read (offline tests)

Rules
-----
- A value must have the type of its default (an int is accepted where a float is
  expected); anything else raises ConfigError at load, before a port is bound.
- Unknown keys are logged and ignored; keys starting with '_' are comments.
- GAME_PORT other than 7022 is allowed but logged for the 2008 build: that client connects
  to 7022 no matter what the version server says (push 0x1B6E at VA 0x44080E). The 2009
  build connects to the port its S2C 0x01 channel entry carries (spec 0x01
  game_server_port, ConnectToGameServer 0x440C70), which is GAME_PORT.
- CLIENT_BUILD picks the client the server speaks to (client-2009-login): '2008' (EN 2008,
  WindSlayer2Game, protocol_spec.json - the default) or '2009' (EN Outspark v1.04 Build 14,
  WindSlayer2009, protocol_spec_2009.json). The build selects the spec (packets.spec_path),
  the client data install (Config.client_dir: CLIENT_DIR / CLIENT_DIR_2009) and the
  build-specific wire forms (version reply, login, character list). It is a per-server
  value (GameServer.client_build), never a module global, so one process can run both.
- Later items add their keys here (lc-version-config: notice, maintenance, user counts;
  lc-create: CREATE_REPLY_0x02; pvp: BF_MATCH_SIZE, room limits).
"""
import json
import logging
import math
import os
import socket

log = logging.getLogger('WS')

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, 'config.json')
ENV_PATH = 'WS_CONFIG'

# The port the EN client hard-codes for the game connection (VA 0x44080E).
CLIENT_GAME_PORT = 7022
# S2C 0x01 channel table limits (login_character.md F1 step 2): <= 10 slots, channel_no
# strictly ascending. Every channel needs its own IP because the game port is fixed.
MAX_CHANNELS = 10
# S2C 0x01 notice: the launcher copies it into a fixed buffer (login_character.md F1).
NOTICE_MAX_BYTES = 1000
# S2C 0x01 version_code (login_character.md 1.2 step 1): 3 = OK, >= 0xEA61 = notice +
# "Connection to Server got disconnected" + WM_CLOSE after 5 s, anything else = the dead
# CDN auto-patcher.
VERSION_OK = 3
VERSION_MAINTENANCE = 0xEA61
# The 2009 client accepts only 14 (LIVE-VERIFIED; spec_2009 0x01 version_code, 0x451A28).
VERSION_OK_2009 = 14
# Client builds the server can speak (config CLIENT_BUILD). '2008' stays the default.
BUILD_2008, BUILD_2009 = '2008', '2009'
CLIENT_BUILDS = (BUILD_2008, BUILD_2009)
# Channel status the client requires before it lets START connect (0x448211).
CHANNEL_STATUS_OPEN = 3
# MOB_AI_COMMAND values, and the hold of the 0x2A self-form node the keep-alive must beat
# (0x459F04 [0x455136]: 960 - min(+0xE3C, hi12) with hi = 0).
MOB_AI_COMMANDS = ('2A', '1B')
# DAMAGE_FORMULA values: 'client' = the client's own FUN_0041b830 / FUN_004194f0 port
# (damage.py), 'placeholder' = the old STR + W_Att - Def rules (combat.py), kept for rollback.
DAMAGE_FORMULAS = ('client', 'placeholder')
MOB_COMMAND_HOLD_SECS = 0.96
# MILEAGE_BONUS_PCT / MILEAGE_EVENT_PCT: a percent of the price, at most 10x (a larger value
# is a typo; mileage itself is clamped to the u32-safe cash.CASH_MAX anyway).
MILEAGE_PCT_MAX = 1000
# Keys that existed once and are ignored now, with why (a config.json that still sets one
# gets this line instead of "unknown key"). DEV_FREE_NOTES: P6 let a Note (1894 / 3320) be
# sent without owning one; since P8 a Note is a cash inventory record whose serial the 0x77
# names (roadmap P8 "Turn off DEV_FREE_NOTES", retired in P8 stage 4).
RETIRED_KEYS = {
    'DEV_FREE_NOTES': 'retired in P8: a Note must be owned (a cash inventory record; GM `!note` grants one)',
}
# DROP_MODE values (P13 boss-b2, bosses.roll_drops): 'rates' = every hni Drop entry rolled on
# its own at rate / DROP_RATE_UNIT (evb B6/E-B2, the default); 'single' = one draw over the
# same column (the same chance per entry, at most one item a kill); 'legacy' = the P0 roll
# (60 %, one entry picked uniformly, rates ignored), kept for rollback.
DROP_MODES = ('rates', 'single', 'legacy')
# MOB_ATTACK_B / MOB_DASH values (P13 boss-b3, bosses.command_flags): which monsters the chase
# may command attack B / the dash - every template with the flag, only the field bosses, or none.
MOB_COMMAND_SCOPES = ('all', 'bosses', 'off')
# RELAY_START_HOLD_GAP_MS (desync fix P1): 0 or this range - above the builder's 210 ms busy
# cadence plus one 30 ms tick, at most the 990 ms hold cap. RELAY_SETTLE_AFTER_MS >= 300.
RELAY_START_HOLD_GAP_RANGE = (240, 990)
RELAY_SETTLE_AFTER_MIN_MS = 300
# MOB_HIT_RECOVER_MARGIN_SECS (desync fix M1): the chase gate is the hit + the stun + this
# margin - at least the 60 ms of tick rounding plus one 30 ms tick (the M1 minimum), at most 1 s.
MOB_HIT_RECOVER_MARGIN_RANGE = (0.09, 1.0)
# MOB_HIT_ICE_CHASE_SECS (desync fix M1, ice): 0 or above 0 and below this - the node hold of
# the hit's 0x2A (0.96 s, MOB_COMMAND_HOLD_SECS) minus a tick of relay latency: past it the
# copies' +0xE9C stalls (the type-4 tick gate 0x4142B8) and the stun stretches.
MOB_HIT_ICE_CHASE_MAX_SECS = 0.95
# Keys whose value is true, false or null (auto: decided by the build, see the key).
AUTO_BOOL_KEYS = frozenset(('DAMAGE_GRADE_ROLL',))

DEFAULTS = {
    # --- client build (client-2009-login) ---
    # '2008' (default) or '2009'. See the module docstring; everything build-specific keys
    # off this one value.
    'CLIENT_BUILD': BUILD_2008,
    # --- sockets ---
    'BIND_HOST': '0.0.0.0',
    'VERSION_PORT': 7011,
    'GAME_PORT': CLIENT_GAME_PORT,
    'ADMIN_PORT': 7099,               # localhost-only debug injection (wsdev send/sendspec)
    'UDP_ROOM_PORT_BASE': 10000,      # room UDP host binds base + room_no (F11, pvp-udp-host)
    # --- identity of this server ---
    'PUBLIC_IP': '127.0.0.1',         # S2C 0x01 game_server_ip: the address clients connect to
    'WORLD_NAME': 'WindSlayer',
    'CHANNELS': [{'no': 1, 'name': 'Channel 1'}],   # optional per-channel "ip" (default PUBLIC_IP)
    # --- version server / login gate (lc-version-config, lc-login-errors) ---
    # S2C 0x01 notice text (<= NOTICE_MAX_BYTES). The launcher prints its first line under
    # the announcement panel; in maintenance it is the whole message box.
    'NOTICE': 'Press start button to start the game.\n\n© 2009 OUTSPARK.com. All rights reserved.',
    # MAINTENANCE turns the version reply into version_code 0xEA61 (notice, then the client
    # exits after 5 s on timer 7) and answers every login with S2C 0x02 result 0x0E.
    'MAINTENANCE': False,
    'MAX_ONLINE': 100,                # more logged-in accounts -> S2C 0x02 result 6 "server full"
    'AUTO_REGISTER': False,           # unknown account + password -> create it instead of 0x11
    # Passwords are stored as pbkdf2_sha256 hashes (auth.py). The store hashes every
    # plaintext record once at load, so an operator can still hand-add {"password": "abc"}.
    'HASH_PASSWORDS': True,
    # --- persistence (F4, lc-data-model) ---
    'ACCOUNTS_FILE': 'accounts.json', # relative paths resolve next to the config file
    'SAVE_DEBOUNCE_SECS': 2.0,        # a change reaches the disk at most this long after it
    'AUTOSAVE_SECS': 60.0,
    # --- new characters (F4 migration: map 101, not map 0 (100,100)) ---
    # Retail put a new character beside Elder Murubisiri (750,912) on map 101, where the
    # map's own key-guide tile (stage01_01.hmi Layer 7 Tile 5, basic_key.hsi at x 367) is on
    # screen: 2010/2011 retail videos, RE 2026-09-24. (1411,714) is the 102->101 portal
    # arrival, whose camera never shows the guide.
    'START_MAP': 101,
    'START_X': 700.0,
    'START_Y': 812.0,
    # Starting wallet written into a character record once (shop_storage-wallet). The store
    # is the only source of the gold and Victy the client shows; the old 999999 session
    # default and the 100000 constant in the 0x03 builder are gone (B2/B3).
    'START_GOLD': 100000,
    'START_VICTY': 0,
    # --- content (F7 arch-content-loader) ---
    # The client install that owns hs/windslayer.hii and .hqi, relative to this file.
    # '..' is the normal layout: server/ lives inside WindSlayer2Game/. CLIENT_DIR is the 2008
    # install; CLIENT_DIR_2009 the 2009 one (Desktop/WindSlayer2009 next to WindSlayer2Game),
    # used when CLIENT_BUILD is '2009' (Config.client_dir()).
    'CLIENT_DIR': '..',
    'CLIENT_DIR_2009': '../../WindSlayer2009',
    # How often the live world state (map, position, HP/MP) of in-world sessions is written
    # back to the store (world-persistence: "and every 60 s"). Map transfer and disconnect
    # save immediately, so this only bounds what a hard kill can lose.
    'WORLD_SAVE_SECS': 60.0,
    # --- HP/MP (cs-regen) ---
    # Natural regeneration period. 15 s is the client's own constant (FUN_00417e10 compares
    # +0xE7C/+0xE80 against 15000 ms); the field client never runs it, so the server does.
    # Only tests and experiments should change it.
    'REGEN_SECS': 15.0,
    # --- monsters (world-1a-defaults, world_movement_npc.md 3.5) ---
    # S2C 0x1A effect entry per monster: 0 = none. Live T-1A-1 (world_movement_npc#20):
    # with the idle defaults a Pupu renders its body with effect_count 0; the old 0x0B3B
    # (2875, Mutation Lv1) entry only masked the non-idle seed. Set 2875 to put it back if a
    # template ever renders body-less.
    'MOB_SPAWN_EFFECT_ID': 0,
    # S2C 0x1A server_controlled (+0x8E4). False (retail): the client wander AI walks the mob
    # and plays its animations; hits come from the client's own 61 B C2S 0x0D reports with
    # the victim's position, so the server needs no mob position (live-verified 2026-09-23:
    # roaming Pupus on their floor spawns take hits and die). True keeps the mob idle where
    # the server put it (world_movement_npc#20), holding one idle frame - the client does not
    # tick a server-controlled idle mob (decomp FUN_00412100/FUN_00413920).
    'MOB_SERVER_CONTROLLED': False,
    # --- ground items (item_inventory-ground-loot-pickup, item_inventory.md F6-F9) ---
    # True: monster loot falls on the ground (S2C 0x12 at the corpse) and the pickup key
    # (C2S 0x1F) moves it into the bag (S2C 0x13); the kill's S2C 0x18 carries the gold only.
    # False: the P0 path - the loot goes straight into the bag with the 0x18 - kept until
    # the ground flow is live-verified in the 2009 client (P4 exit criterion 3). Dropping
    # bag / worn items (C2S 0x13 / 0x14) always uses the ground.
    'GROUND_LOOT': True,
    # Seconds an unpicked ground item lies before it despawns (S2C 0x13 with picker 0,
    # live-safe as T-S13b). Retail's lifetime is unknown; item_inventory.md F6 suggests 60-120.
    'GROUND_ITEM_SECS': 60.0,
    # At most this many items per map: a new drop despawns the oldest, so un-picked loot can
    # never pile up and lag the client (the P0 reason ground drops were deferred).
    'GROUND_ITEMS_PER_MAP': 100,
    # --- crafting (P4 stage 3: item_inventory-crafting / -reinforcement / -gathering) ---
    # Concoction / Mineral Refining roll the client's own "Success probability" table
    # (crafting.CRAFT_SUCCESS_PCT, DAT_006f09ec / 2009 DAT_005250c4), so they have no key.
    # Reinforcement success chance in percent. Retail's rate is unknown (item_inventory Q8);
    # a failure costs nothing but the 5 s (the client keeps the stone on every non-1 S2C
    # 0x8E result and the server mirrors that).
    'REINFORCE_SUCCESS_PCT': 70,
    # Unit of a gather node's hni Drop column: each reward entry is a rate-per-DIVISOR chance
    # (every EN node sums to 65550, i.e. a 65.55% chance of finding something at 100000).
    # 0 = always a reward, picked with the Drop column as weights. The tool is used up on
    # every result either way (the client removes it before it reads the result).
    'GATHER_RATE_DIVISOR': 100000,
    # --- card deck and notices (P4 stage 4: quest_cards_misc-card-exp-bonus / -system-notice-0x99) ---
    # +10% exp for a kill whose Monster Card is registered in the killer's deck (the card
    # tooltip "Card effect: EXP + 10%"; at least 1 point, cards.bonus_exp). Retail behaviour
    # is unproven (quest_cards_misc Q2), hence the switch.
    'CARD_EXP_BONUS': True,
    # S2C 0x99 sub 8 "In channel N." once per connection, on the client's first C2S 0x63
    # (right after the enter-world burst).
    'CHANNEL_NOTICE': True,
    # --- events (P13 stage 1: ev-e1..ev-e4, events.py) ---
    # The event schedule: a JSON file {"events": [...]} next to this config (events.py has
    # the format); '' = no events. A malformed file stops the start like a bad key here.
    'EVENTS_FILE': '',
    # Scale quest exp (the 0x21 after a 0x27) by the event multiplier too (evb E2 "optional").
    # Off: only kills and their party shares are scaled; a mentor's 0x7F share follows the
    # kill it comes from either way.
    'EVENT_EXP_QUESTS': False,
    # The entry announcement and the Event News popup are once per login; a relogin of the
    # same character within this many seconds (a relog, a crash-reconnect, a 2009 channel
    # change) does not repeat them (P13 exit criterion 2). The gift is persisted instead.
    'EVENT_RELOGIN_QUIET_SECS': 1800.0,
    # ev-e5 (P13 stage 2): push_quests only ever pushes quests whose quest-log texts (hqi
    # title / Explain / Intro in QSTLngKo.lng) are plain ASCII - the 2009 chain 158-160. The
    # Korean-text event quests (232-291, and the 2008 hqi's own 158-161) would show as CP949
    # bytes in the EN font (evb A3, Q12); true pushes them anyway.
    'EVENT_QUEST_KOREAN_TEXT': False,
    # --- field bosses and drops (P13 stage 3: boss-b1, boss-b2, bosses.py) ---
    # A monster spawn tile whose map value_num is one of these is a field boss (evb B1: the
    # data has no boss flag; exactly 11 event-2 tiles carry 300 in the 2009 maps, 15 in 2008.
    # The one Crow tile at 901 and the Shovel Frog NPC tiles at 9901 are not bosses).
    'BOSS_TILE_VALUES': [300],
    # A boss respawns value_num * BOSS_RESPAWN_SCALE seconds after its death (300 s at 1.0;
    # evb B5, reading value_num as seconds is INFERRED). The respawn time is kept in the boss
    # ledger, so it holds across a map discard, the last player leaving and a server restart.
    'BOSS_RESPAWN_SCALE': 1.0,
    # The boss ledger, a small JSON next to ACCOUNTS_FILE (same directory); '' = memory only
    # (a restart respawns every boss).
    'BOSS_LEDGER_FILE': 'boss_ledger.json',
    # How a kill's loot is rolled over the monster's hni item/Drop columns (DROP_MODES above).
    'DROP_MODE': 'rates',
    # The unit of the hni Drop column: an entry drops with rate / DROP_RATE_UNIT (evb B6,
    # INFERRED: every boss trophy is 99990 = 99.99 %, every table sums to <= 100000).
    'DROP_RATE_UNIT': 100000,
    # --- boss combat (P13 stage 4: boss-b3, bosses.command_flags) ---
    # Which monsters the server's chase may command attack B (S2C 0x2A lo 15 left / 16 right:
    # motion 5, template AI[8], its hits are Strong_Atk) and the dash (lo 19 / 1A: motion 6,
    # AI[3]): 'all' = every template with the flag (the mobai chase since P5), 'bosses' = only
    # the field bosses (BOSS_TILE_VALUES tiles: the Monkey King, Rynx, ...), 'off' = never (the
    # mob swings attack A / walks instead). Attack A (lo 05/06) is live-verified (LIVE_TEST_LOG
    # 2026-09-24, Wild Cliff); attack B and the dash are NOT yet (evb B10, T-B3): turn one off
    # here if the live check shows it misbehaving. A template without the flag never gets the
    # word in any mode - Monkey Lord (hni 140, AI 1100001000000) swings attack A only.
    'MOB_ATTACK_B': 'all',
    'MOB_DASH': 'all',
    # --- skills (cs-skill-learn) ---
    # Skill ids a new character is created knowing ("starting skills"). EN gives none at
    # creation: Dash 80, Double Jump 94, Mining 82, Herb Gathering 86, ... are quest rewards
    # (quests 28, 26, 156, 155; combat_skill.md open question Q11). Each id is learned with
    # the client's own family rule, persisted and sent in the 0x07 skill list.
    'STARTING_SKILLS': [],
    # --- death and revive (cs-player-death, combat_skill.md F8) ---
    # HP a revive restores, in percent of the maximum (P3 exit criterion 5: "partial HP").
    # MP is left as it was (the client's death processing only zeroes HP).
    'REVIVE_HP_PCT': 50,
    # {"<map code>": [map, x, y]}: where a death on that map revives. Maps not listed revive in
    # the nearest town by portal hops (combat.revive_point): the start map 101 or one of the
    # nine towns with a flea-market portal.
    'REVIVE_POINTS': {},
    # Take FUN_0041a230's exp penalty on a server-side death (levels 10..98, never a level
    # down), sent as a negative S2C 0x21. The client's own death path (S2C 0x3E) applies none.
    'DEATH_EXP_PENALTY': True,
    # Damage the player when the client reports a monster hitting him (C2S 0x0D action event
    # 1..10 with event_source_uid = one of this session's monsters). Only the victim's own
    # client detects it (0x416EFF..0x416F20) and it never subtracts HP itself (0x41A6C2), so
    # without this the player takes no damage at all: the stat follows the event (1/6
    # Body_Atk, 7/8 Weak_Atk, 4/5/9/10 Strong_Atk, 2/3 a guarded weak swing - no damage),
    # S2C 0x28 or the 0x3E death (DAMAGE_FORMULA says how much).
    'MOB_CONTACT_DAMAGE': True,
    # Minimum seconds between two BODY-contact hits (events 1/6) of one monster on one player.
    # The client reports a touch after each 270 ms i-frame window and a chasing mob walks back
    # and forth through the player, so at the old shared 0.5 s slot contact drained HP about
    # every 0.54 s and starved the swings (livetest bug 2; swings keep their own 0.5 s slot,
    # GameServer.MOB_SWING_MIN_SECS). Retail's rate is unknown; its footage shows single
    # swing digits, not a steady drain. 0 = every reported touch.
    'MOB_CONTACT_MIN_SECS': 1.2,
    # How the server computes a hit (both directions: the player's swings, strong attacks,
    # attack skills, traps and detonations on monsters, and a monster's hit on the player).
    # 'client' (default) = the client's own formula, ported in damage.py: the stats
    # FUN_0041b830 derives (class tables x STR/DEX/INT/SPR, weapon W_Att/S_Att, armour Def,
    # level) through FUN_004194f0's level scale ~ 2 A^2 La / ((A+D)(La+Lv)), the skill and
    # element terms, clamp >= 1, capped at the victim's HP - the number under the client's
    # damage digit before its grade roll. 'placeholder' = the old rules (STR + W_Att - Def,
    # Body_Atk - Def), for rollback. DoT ticks are the same in both.
    'DAMAGE_FORMULA': 'client',
    # Roll the grade on the server's value of a player's hit on a monster (livetest bug 10):
    # the 2009 client patch (combo HUD v2, WindSlayer2009/combo_hud_2009.py cave A at
    # 0x41A55A) rolls the digit it draws - CRITICAL! x1.5 5%, BAD x0.75 13% (only BAD may
    # reach 0), GOOD x1.25 13%, else x1.0, times a uniform 0.9..1.1 jitter, truncated, >= 1 -
    # and the server applies the same table at the same point (damage.grade_roll):
    # client-reported swings / strong attacks, attack skills, traps and the detonation
    # release, under either DAMAGE_FORMULA. Never DoT ticks, reflections or a monster's hit on
    # the player. The digit and the server's value are INDEPENDENT draws with the same
    # distribution and mean (~1.025 x the base), not the same number per hit (that needs a
    # seed shared with the client - an open question).
    # None (default, "auto") = on only for CLIENT_BUILD '2009' (grade_roll_on): no 2008 exe
    # rolls its digit, so a 2008 server rolling would make the HP taken disagree with the
    # exact digit drawn - bug 10 in reverse. True / False force it; a 2009 exe built with
    # patch_2009.py --no-combo-hud draws the unrolled value and needs False.
    'DAMAGE_GRADE_ROLL': None,
    # With MOB_AGGRO on, only a monster that is after THIS player (he is its aggro target or on
    # its hate list: he hit it, or it saw him with the AI[5] proximity scan) hurts on touch.
    # Retail: walking through un-hit wandering Ssiyo does nothing; once hit they chase and
    # every bump hurts (2011 gameplay video). False = every reported touch hurts (the old way).
    'MOB_CONTACT_AGGRO_ONLY': True,
    # --- monster aggro (retail server-side monster AI, MONSTER_AGGRO_RE_2026-09-23) ---
    # A monster the player hits turns on him: the server keeps the hate list the field client
    # never runs (its host engine is off, scene+0xF40 == 0) and drives the mob with command
    # nodes every MOB_AI_TICK_SECS - walk / jump / drop / attack by the client's own chase
    # rules - handing it back with S2C 0x9E on the target's death or departure, past
    # MOB_LEASH_PX from its spawn, or MOB_AGGRO_TIMEOUT_SECS without a hit. False = the old
    # behaviour (a hit mob only gets the hit-lock release).
    'MOB_AGGRO': True,
    # The command form: '2A' (default) = the 16-byte S2C 0x2A self-form {mob, lo, 0, receiver}
    # (flushes the queue, holds 960 ms, re-sent on a change or every MOB_CMD_KEEPALIVE_SECS);
    # '1B' = the fallback if a live test shows 0x2A carries no motion: S2C 0x1B {mob, hold 300,
    # lo, 0} nodes streamed after the 0x2A release, each only when the last is nearly spent.
    'MOB_AI_COMMAND': '2A',
    'MOB_AI_TICK_SECS': 0.3,
    'MOB_CMD_KEEPALIVE_SECS': 0.6,     # must stay under the 0x2A node's 960 ms hold
    'MOB_LEASH_PX': 900.0,             # max chase distance from where the chase began
    'MOB_AGGRO_TIMEOUT_SECS': 15.0,    # no hit for this long and > 400 px away: give up
    'MOB_ATTACK_REACH_X': 60.0,        # the attack box in front of an AI[0]/AI[8] mob
    'MOB_ATTACK_REACH_Y': 40.0,
    # Hysteresis of the attack box (mobai.ATTACK_HOLD_X): a swing starts with the target
    # within MOB_ATTACK_REACH_X and keeps going until it is more than MOB_ATTACK_REACH_X +
    # MOB_ATTACK_HOLD_X in front, so the chase word no longer flips walk / attack on every
    # 0.3 s tick near the edge (P7 live L2: 36 flips in 21 s, each a queue-flushing 0x2A).
    # 0 = the plain box every tick.
    'MOB_ATTACK_HOLD_X': 30.0,
    # ... for this long after the attack word was first commanded (about one swing: the
    # client re-decides every 990 ms; mobai.ATTACK_HOLD_SECS). Then the plain box decides
    # again, so a target standing 1..30 px beyond reach is walked after instead of swung at
    # forever (a commanded attack never moves the mob). 0 = no hold.
    'MOB_ATTACK_HOLD_SECS': 1.0,
    # --- shared monsters (P5 stage 3: world-shared-monsters) ---
    # One set of monsters per map, the same uids on every client there. A map nobody stands
    # on keeps its monsters this long - dead ones stay dead until their own respawn - before
    # they are discarded and rebuilt fresh on the next arrival (world_movement_npc.md F6 step
    # 5: no kill -> leave -> return free respawn). 0 = discard when the last player leaves.
    'MOB_MAP_KEEP_SECS': 300.0,
    # --- other players (P5 stage 2: world-presence, world-position-estimate) ---
    # A connection that dies without a FIN/RST (cable, frozen machine) is detected by TCP
    # keepalive within about this many seconds, so its peers see it despawn (S2C 0x06) that
    # fast instead of after the 120 s recv timeout. 0 = off. A lossy internet link should
    # raise it: an outage longer than this drops the connection.
    'DEAD_PEER_SECS': 2.0,
    # The dev memory driver's position read (DEV_MEMORY_COMBAT, one local client) is the
    # position when true; false leaves the dead-reckoned estimate alone and only measures its
    # error against memory (`!where`, spike S-1 / decision G1).
    'POSITION_DRIVER_FIX': True,
    # --- position sync between clients (desync fix plan 2026-09-28, POSITION_SYNC_RE) ---
    # Each change has its own switch for the staged live rollout (one per session: P1, M1,
    # P2, P3). Both client builds.
    # P1: an S2C 0x1B node after idle words gets the 30 ms start hold only when more than
    # this many ms passed (the builder's FINAL idle packet); a shorter gap - idle words sent
    # while the mover was still busy, every 210 ms - keeps its real elapsed time, so the
    # observer's copy replays the mover's whole timeline (presence.relay_fields). 0 = the
    # legacy clamp after every idle node; else 240..990 (the 210 ms busy cadence + a tick).
    'RELAY_START_HOLD_GAP_MS': 450,
    # P2: once a mover's last relayed words are idle and no C2S 0x0D came for
    # RELAY_SETTLE_AFTER_MS (>= 300), every client holding him gets that node once more with
    # hold RELAY_SETTLE_HOLD_MS (30..990) - a copy left floating or mid-animation finishes
    # and lands; an idle one ignores it (presence.settle_node, 'presence-settle' tick).
    'RELAY_SETTLE_NODE': True,
    'RELAY_SETTLE_AFTER_MS': 450,
    'RELAY_SETTLE_HOLD_MS': 990,
    # P3: the server's position estimate follows a player's dash (2.7 x walk after a 60 ms
    # wind-up, <= 540 ms: 18 ticks, 364.5 px), knockback (7.5 px x 4 or 2 ticks along
    # facing2) and dash attack (+37.5 px), and stands still in his own hurt (state 3 for
    # hitstun.hurt_len after his action 6 / 7 / 9) - spawn records, keyframes and the
    # monster chase read it (presence.advance).
    'POSITION_ESTIMATE_DASH_KNOCK': True,
    # P3: a commanded monster is dead-reckoned at its template's own speed (1e7 / hni speed
    # px/s: Ssiyo 120000 -> 83.3, Monkey Soldier 80000 -> 125; x 2.7 in a dash) instead of the
    # one measured 82.5 px/s for every template.
    'MOB_SPEED_FROM_TEMPLATE': True,
    # M1: the hit relay to the OTHER clients holding a monster carries the attacker's facing
    # (lo facing2 bits 20-21) and the hurt time (hi, 0..4095), so their copies slide and stun
    # as the attacker's own copy does; the server's point of the mob moves by the same slide.
    # The attacker still gets only his 16-byte release. For a basic swing / dash attack
    # (interact event 7) the packet is 33 bytes, action = the tail's target_action_event (7,
    # or 8 for an airborne victim), a 2-tick slide and a stun of hurt - 60 ms. False = the old
    # flinch in place (action 7, facing2 0, hi 0). A guarded hit (4) and a trap catch (0xC)
    # always get the old flinch in place; a skill hit (9) is MOB_HIT_RELAY_SKILL_VARIANT's.
    'MOB_HIT_RELAY_KNOCKBACK': True,
    # The hurt of a hit whose kind or timing the server cannot tell (no swing / cast words
    # counted, a stale action, MOB_HIT_HURT_PER_SWING off): 490 = the basic swing from
    # standing, the median of the attacker's +0x9E4 in live session 2 (10/10). With
    # MOB_HIT_HURT_PER_SWING on it is the floor of a guess that errs high (a low hurt lets
    # the chase in early, a high one only holds it): a skill / strong-attack hit (event 9)
    # gets the state-1 default L - 270 (1020 for a Warrior), a held attack key past its
    # action the stage-2 default 760 (hitstun.classify).
    'MOB_HIT_RELAY_HURT_MS': 490,
    # Per-swing hurt (HIT_STUN_PER_SWING_RE_2026-09-28): hurt = L - E, what is left of the
    # attacker's action at the hit - L its length (combo 610 / 1250 / 2140, bow 720, staff
    # 740, skill / strong attack by weapon, variant and tier), E his elapsed time, counted
    # from his own C2S 0x0D words (hitstun.py): basic swing 490, dash attack 760, Ice Spear
    # 1020 (+1000 ice). False = MOB_HIT_RELAY_HURT_MS for every hit. The L / E0 / variant
    # tables are the 2009 client's (static RE + live session 2); for CLIENT_BUILD '2008'
    # they are UNVERIFIED (its body001.hsi and variant table were never read).
    'MOB_HIT_HURT_PER_SWING': True,
    # M3: a skill or strong-attack hit (interact event 9) is relayed in the case-9 form: 34
    # bytes, action 9 (10 airborne) + the u8 action_flag = the attacker's cast variant (lo
    # bits 5-8 of his motion-7 words; 0 for a strong attack) before the position block, hi =
    # the hurt. The watchers' copies then run the attacker's own pass 2: the 4-tick slide,
    # the stun, the element (ice +1000 ms, wind 19-tick slide) and the local debuff. The
    # server's point slides 4 ticks (19 x 1.0 / 2.5 for wind). False = the old flinch in
    # place (33 bytes, action 7, facing2 0, hi 0). A Priest tier-2 Vampiric Attack goes out
    # with flag 0: its drain would be applied to the watchers' copies of the attacker
    # (hitstun.NO_RELAY_FLAG). Verified for the 2009 client only; for CLIENT_BUILD '2008'
    # the bytes match its 0x2A grammar, but how its pass 2 uses the flag is UNVERIFIED.
    'MOB_HIT_RELAY_SKILL_VARIANT': True,
    # M1: no chase word for a monster until its copies have finished the hurt of a
    # client-caught swing or skill hit (interact events 7 and 9; guarded hits and trap
    # catches set no gate): until the hit + max(MOB_HIT_RECOVER_SECS, stun +
    # MOB_HIT_RECOVER_MARGIN_SECS), the stun being hurt - 60 for a swing and hurt (+1000 ice,
    # >= 600 wind) for a skill. Basic swing 0.55 s, dash attack 0.82 s, Ice Spear 2.14 s. A
    # combo extends it (never shortens it). MOB_HIT_RECOVER_SECS is the floor; 0 = no gate
    # at all (the chase word on the next 'monster-ai' tick).
    'MOB_HIT_RECOVER_SECS': 0.45,
    # 60 ms of tick rounding plus the relay's latency; >= 0.09 (the M1 minimum), <= 1.
    'MOB_HIT_RECOVER_MARGIN_SECS': 0.12,
    # The gate armed at CAST time too: an attack skill the server's box lands on a monster
    # holds its chase for the skill's action length L + the margin (the client reports its
    # own catch within L), so the skill damage / aggro path sends no chase word into the
    # hurt the client is about to start; the report then extends it. Needs
    # MOB_HIT_RECOVER_SECS > 0.
    'MOB_HIT_CAST_GATE': True,
    # The chase gate of an ice-element skill hit (+0x95D = 2: the stun is hurt + 1000 ms, at
    # least 1 s on every copy): no longer the stun + the margin but the hit + this. A server-
    # driven copy's state machine runs only while its last 0x2A node's 960 ms hold does (the
    # type-4 tick gate (+0xEE4 && +0x971) || +0x96D, 0x4142B8), so with no word queued both
    # copies stalled their +0xE9C at 960-990 until the chase word came and then ran the rest
    # of the stun (live session 2b: 3.3-3.5 s frozen, retail 2.0 s). A word queued before the
    # stall keeps them ticking and does not cut the stun short (the state-3 exit is +0xE9C
    # against +0x9E4 + 1000 alone, 0x41528A..0x4152C2; a word's action nibble 0 never reaches
    # pass 2): both end at 1000 + their own hurt and walk. The word goes out at that time on
    # its own (a one-shot timer), not on the next 'monster-ai' tick, even when the decision
    # did not change (STOP included), and is kept alive until the stun ends. 0.6 (the
    # keep-alive cadence) leaves about 0.36 s before the copies stall (their 0x2A arrival +
    # 960 ms) for downlink jitter and the shared 'world' scheduler thread; an earlier word
    # costs nothing (live session 2: a word 0.17 s after the hit still gave the 2.0 s stun).
    # 0 = the plain stun gate; else above 0 and below 0.95. Needs MOB_HIT_RECOVER_SECS > 0.
    'MOB_HIT_ICE_CHASE_SECS': 0.6,
    # --- village transfer (world-village-transfer, world_movement_npc.md F5) ---
    # {"<town map>": [x, y]}: where a Garan Maria transfer to that town lands. Arrival
    # points are not in the client (T-5D-3), so the default is next to Garan Maria in the
    # town's EN map file (the revive-town point where she is absent: 101, 201, 401).
    'VILLAGE_ARRIVALS': {},
    # Minimum time between two village transfers of one session; a request inside it gets
    # S2C 0x81 {0}, whose text is "Please try again in a few minutes." (F5 step 2).
    'VILLAGE_COOLDOWN_SECS': 10.0,
    # --- messenger (P6 stage 2: social_friend F9/F12, social_friend-mentor-exp-share) ---
    # Gold one Frenaiga friend-list expansion (+5 slots, C2S 0x73) costs. The client only
    # gates on gold >= 50000 before sending; UI 8990 fills the price in at run time from an
    # unknown source (social_friend Q5), so 50000 is the default.
    'FRIEND_SLOT_PRICE': 50000,
    # Percent of a mentee's kill exp its online mentor gets as S2C 0x7F "(Menti[name])"
    # (the retail rate is unknown: UI 2672/8970 only say "bonus EXP"). 0 = no share.
    'MENTOR_EXP_SHARE_PCT': 10,
    # --- reputation (P7 stage 3: social_friend-compliment, reputation.py) ---
    # Manner points one compliment (popup Praise, C2S 0x6D) gives the target's account. The
    # client prints whatever S2C 0x96 carries ("<A> added N of your manner points.").
    'COMPLIMENT_DELTA': 1,
    # Compliments one account may RECEIVE per server-local day; one more gets S2C 0x94 {5}
    # ("... got enough compliment for today", UI 8944). Retail value unknown (social_friend Q9).
    'COMPLIMENT_DAILY_CAP': 5,
    # The same giver account may compliment the same target account again only after this many
    # days (UI 8944 "the same person only once a week" -> 0x94 {6}). 0 = no such rule; the
    # giver's own once-a-day rule (0x94 {7}) always applies.
    'COMPLIMENT_REPEAT_DAYS': 7,
    # --- the Item Mall / Spark Shop (P8 stage 2: premium_cash-buy / -gift, mall.py) ---
    # Percent of a Wind Cash purchase's (or gift's) price credited as Mileage. The client pops
    # "50% bonus mileage has been deposited." whenever the balance it is sent rises; the
    # retail rate is unknown (premium_cash Q9), so the default credits nothing. Mileage
    # purchases never earn mileage.
    'MILEAGE_BONUS_PCT': 0,
    # A MILEAGE EVENT (P8 stage 4: premium_cash-mileage-event, F16): percent of every Wind Cash
    # purchase / gift price credited as event Mileage on top, told the retail way - S2C 0x70
    # {cash, mileage, 0} then S2C 0x98, the client's own chat line "※Mileage Event※ You got
    # bonus mileage." (spec 0x98). 0 = no event running. GM `!mileage event <pct|off>`
    # overrides it until a restart; `!mileage <n> [name|all]` credits an event bonus directly.
    'MILEAGE_EVENT_PCT': 0,
    # Sell and gift the 2009 pets (hii Type 6) in the Spark Shop (ROADMAP_2009_ADDENDUM C1,
    # mall.py "Pets"). The pet records, their box -> character binding and the 0x6A / 0x6F /
    # 0x6C encodings are in place either way; off until P15 answers C2S 0x82 PetEquip, since a
    # bought pet could otherwise only sit in the bag.
    'MALL_PETS': False,
    # --- using cash items (P8 stage 3: premium_cash-megaphone, cashuse.py) ---
    # How a megaphone's count goes down on its owner's client (the original packet is unknown,
    # premium_cash Q7): '0x72' = the client's own consume-by-serial through S2C 0x72 {uid, item,
    # serial} (plays the use sparkle 0x140), '0x6F' = the whole owned list again (no sparkle).
    'MEGAPHONE_CONSUME_PACKET': '0x72',
    # --- parties (P6 stage 3: party.md party-exp-share / party-map-change-hud) ---
    # Share a kill's exp with the killer's party members alive on the same map (server
    # policy - no client packet or string implies one, party.md Q10 - but the retail footage
    # shows the "party EXP split" and "a party gives more total XP than soloing"): a pool of
    # exp * (100 + PARTY_EXP_BONUS_PCT * (members - 1)) / 100, split evenly (party.split_exp).
    'PARTY_EXP_SHARE': True,
    'PARTY_EXP_BONUS_PCT': 20,
    # Re-send the party frames (0x51 self + 0x4F per member) after every map load. Off: the
    # frames survive 0x08/0x03 (live 2008 party#14; the 2009 0x08 closes the same windows).
    'PARTY_HUD_REBUILD_ON_MAP_LOAD': False,
    # --- characters (lc-create / lc-delete) ---
    # Retail refused a delete within 24 h (S2C 0x1F result 3); 0 is the dev default.
    'DELETE_MIN_AGE_HOURS': 0.0,
    # EN item id a new character is created wearing (0 = none, which is what the design
    # gives a Novice: login_character.md F3 2d lists no items). It goes into the item's equip
    # grid slot and the stored look is composed as an S2C 0x1D would (inventory.compose), so
    # the 0x02 / 0x07 records show it in the hand and in the Equipment window.
    'STARTER_WEAPON': 0,
    # --- dev flags ---
    'DEV_MEMORY_COMBAT': True,        # the ReadProcessMemory swing driver (F10 dev path)
    # (DEV_FREE_NOTES is gone: RETIRED_KEYS.) A Note (1894/3320, C2S 0x4B) is a record of the
    # cash inventory (cash.py; `!note` grants one, limit_type 1) and the S2C 0x77 success names
    # its serial, so the client uses one up (livetest 2026-09-25); none owned -> 0x77 {0}.
    # The driver's swing-based damage. Off: the client reports its own connecting swings in
    # C2S 0x0D (61 B, interact event 7) and the server applies those, so a driver hit on top
    # would count every swing twice. The driver keeps position tracking and the `hit` hook.
    'DRIVER_MELEE': False,
    # Answer a successful create with the full S2C 0x02 list as well as S2C 0x1C. Live
    # verification (login_character#03/#05) showed a second success 0x02 at character
    # select appends duplicate entities instead of replacing them (uids restart at
    # 30000000) and leaves window 0x13 open, so this stays off; 0x1C result 1 makes the
    # client build the new entity from its own creation state.
    'CREATE_REPLY_0x02': False,
}


class ConfigError(ValueError):
    pass


class Config(dict):
    """Validated settings with attribute access."""

    def __init__(self, values, path=None):
        super().__init__(values)
        self.path = path

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    @property
    def base_dir(self):
        return os.path.dirname(os.path.abspath(self.path)) if self.path else HERE

    def resolve(self, value):
        return os.path.normpath(value if os.path.isabs(value) else os.path.join(self.base_dir, value))

    @property
    def accounts_path(self):
        return self.resolve(self['ACCOUNTS_FILE'])

    def channels(self):
        """[(channel_no, name, ip)] with each ip defaulted to PUBLIC_IP."""
        return [(int(c['no']), str(c.get('name', f'Channel {c["no"]}')), str(c.get('ip') or self['PUBLIC_IP']))
                for c in self['CHANNELS']]

    def public_ip_host_order(self, ip=None):
        return ip_host_order(ip or self['PUBLIC_IP'])

    def notice(self):
        """S2C 0x01 notice text as wire bytes (already length-checked at load)."""
        return notice_bytes(self['NOTICE'])

    def version_code(self):
        """S2C 0x01 version_code: 3 = OK (arms the START blink), 0xEA61 = notice and the
        client exits 5 s later (timer 7). No other value: the launcher would start the
        dead CDN auto-patcher (login_character.md F1). The 2009 client's OK value is 14
        (spec_2009 0x01, live-verified); maintenance is the same 0xEA61 gate."""
        if self['MAINTENANCE']:
            return VERSION_MAINTENANCE
        return VERSION_OK_2009 if self['CLIENT_BUILD'] == BUILD_2009 else VERSION_OK

    @property
    def client_build(self):
        return self['CLIENT_BUILD']

    def client_dir(self):
        """The client data install of the active build (en_content.configure resolves a
        relative path against the server directory)."""
        return self['CLIENT_DIR_2009'] if self['CLIENT_BUILD'] == BUILD_2009 else self['CLIENT_DIR']


def notice_bytes(text):
    """S2C 0x01 notice text as bytes. latin-1 first, so the live-proven default keeps its
    one-byte (c) 0xA9 exactly as the launcher was verified with (live login_character#01);
    anything outside latin-1 (a Korean notice) falls back to the client's cp949."""
    if isinstance(text, (bytes, bytearray)):
        return bytes(text)
    text = str(text)
    try:
        return text.encode('latin-1')
    except UnicodeEncodeError:
        return text.encode('cp949', 'replace')


def ip_host_order(ip):
    """Dotted quad -> the host-order u32 the client stores (it htonl's it at Fireway Connect
    0x10001CA0): 127.0.0.1 -> 0x7F000001, packed little-endian as 01 00 00 7F."""
    a, b, c, d = socket.inet_aton(ip)
    return (a << 24) | (b << 16) | (c << 8) | d


def grade_roll_on(cfg):
    """DAMAGE_GRADE_ROLL resolved for a config mapping: None (auto) is on only for
    CLIENT_BUILD '2009', whose combo HUD v2 patch rolls the digit (the key's comment)."""
    flag = cfg.get('DAMAGE_GRADE_ROLL')
    if flag is None:
        return cfg.get('CLIENT_BUILD') == BUILD_2009
    return bool(flag)


def _check_type(key, value, default):
    if key in AUTO_BOOL_KEYS:
        if value is None or isinstance(value, bool):
            return value
        raise ConfigError(f'{key}: expected true, false or null (auto), got {value!r}')
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(default, float):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        value = float(value) if ok else value
    else:
        ok = isinstance(value, type(default))
    if not ok:
        raise ConfigError(f'{key}: expected {type(default).__name__}, got {value!r}')
    return value


def _validate(values):
    if values['CLIENT_BUILD'] not in CLIENT_BUILDS:
        raise ConfigError(f'CLIENT_BUILD: {values["CLIENT_BUILD"]!r} is not one of {CLIENT_BUILDS}')
    for key in ('VERSION_PORT', 'GAME_PORT', 'ADMIN_PORT'):
        if not 1 <= values[key] <= 0xFFFF:
            raise ConfigError(f'{key}: {values[key]} is not a TCP port')
    if not 1 <= values['UDP_ROOM_PORT_BASE'] <= 0xFFFF - 128:
        raise ConfigError(f'UDP_ROOM_PORT_BASE: {values["UDP_ROOM_PORT_BASE"]} leaves no room for 128 rooms')
    for key in ('SAVE_DEBOUNCE_SECS', 'AUTOSAVE_SECS', 'REGEN_SECS'):
        if values[key] <= 0:
            raise ConfigError(f'{key}: must be > 0')
    if not 1 <= values['START_MAP'] <= 0xFFFF:
        raise ConfigError(f'START_MAP: {values["START_MAP"]} is not a u16 map code')
    if values['MAX_ONLINE'] < 1:
        raise ConfigError(f'MAX_ONLINE: {values["MAX_ONLINE"]} would refuse every login')
    if values['DELETE_MIN_AGE_HOURS'] < 0:
        raise ConfigError(f'DELETE_MIN_AGE_HOURS: {values["DELETE_MIN_AGE_HOURS"]} must be >= 0')
    if not 0 <= values['STARTER_WEAPON'] <= 0xFFFF:
        raise ConfigError(f'STARTER_WEAPON: {values["STARTER_WEAPON"]} is not a u16 item id')
    skills = values['STARTING_SKILLS']
    if len(skills) > 30 or not all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 4248
                                   for v in skills):
        raise ConfigError(f'STARTING_SKILLS: at most 30 EN skill ids (1..4248), got {skills!r}')
    if not 1 <= values['REVIVE_HP_PCT'] <= 100:
        raise ConfigError(f'REVIVE_HP_PCT: {values["REVIVE_HP_PCT"]} must be 1..100 (0 would revive a corpse)')
    for key, row in values['REVIVE_POINTS'].items():
        ok = str(key).isdigit() and isinstance(row, list) and len(row) == 3
        ok = ok and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in row)
        if not ok or not 1 <= int(row[0]) <= 0xFFFF:
            raise ConfigError(f'REVIVE_POINTS: {key!r}: {row!r} must be "<map>": [map, x, y]')
    for key, row in values['VILLAGE_ARRIVALS'].items():
        ok = str(key).isdigit() and isinstance(row, list) and len(row) == 2
        if not ok or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in row):
            raise ConfigError(f'VILLAGE_ARRIVALS: {key!r}: {row!r} must be "<town map>": [x, y]')
    for key in ('MILEAGE_BONUS_PCT', 'MILEAGE_EVENT_PCT'):
        if not 0 <= values[key] <= MILEAGE_PCT_MAX:
            raise ConfigError(f'{key}: {values[key]} must be 0..{MILEAGE_PCT_MAX} (percent of a cash price)')
    if values['MEGAPHONE_CONSUME_PACKET'].lower() not in ('0x72', '0x6f'):
        raise ConfigError(f'MEGAPHONE_CONSUME_PACKET: {values["MEGAPHONE_CONSUME_PACKET"]!r} is not "0x72" or "0x6F"')
    if not 0 <= values['PARTY_EXP_BONUS_PCT'] <= 100:
        raise ConfigError(f'PARTY_EXP_BONUS_PCT: {values["PARTY_EXP_BONUS_PCT"]} must be 0..100')
    # json.load takes NaN / Infinity: NaN would turn the limit off (every `<` is False), inf
    # would let each monster touch a player once for ever and never prune its slot.
    if not math.isfinite(values['MOB_CONTACT_MIN_SECS']) or values['MOB_CONTACT_MIN_SECS'] < 0:
        raise ConfigError(f'MOB_CONTACT_MIN_SECS: {values["MOB_CONTACT_MIN_SECS"]} must be a finite '
                          f'number >= 0 (0 = no limit)')
    if not 1 <= values['COMPLIMENT_DELTA'] <= 1000:
        raise ConfigError(f'COMPLIMENT_DELTA: {values["COMPLIMENT_DELTA"]} must be 1..1000')
    if values['COMPLIMENT_DAILY_CAP'] < 1:
        raise ConfigError(f'COMPLIMENT_DAILY_CAP: {values["COMPLIMENT_DAILY_CAP"]} would refuse every compliment')
    if not 0 <= values['COMPLIMENT_REPEAT_DAYS'] <= 366:
        raise ConfigError(f'COMPLIMENT_REPEAT_DAYS: {values["COMPLIMENT_REPEAT_DAYS"]} must be 0..366')
    if values['VILLAGE_COOLDOWN_SECS'] < 0:
        raise ConfigError(f'VILLAGE_COOLDOWN_SECS: {values["VILLAGE_COOLDOWN_SECS"]} must be >= 0')
    if values['EVENT_RELOGIN_QUIET_SECS'] < 0:
        raise ConfigError(f'EVENT_RELOGIN_QUIET_SECS: {values["EVENT_RELOGIN_QUIET_SECS"]} must be >= 0 '
                          f'(0 = every login repeats the announcement and the popup)')
    if not 0 <= values['MOB_SPAWN_EFFECT_ID'] <= 0xFFFF:
        raise ConfigError(f'MOB_SPAWN_EFFECT_ID: {values["MOB_SPAWN_EFFECT_ID"]} is not a u16 effect id')
    if values['GROUND_ITEM_SECS'] <= 0:
        raise ConfigError(f'GROUND_ITEM_SECS: {values["GROUND_ITEM_SECS"]} must be > 0')
    # Ground ids are u16 per map (ids.GROUND_ITEM); the round-robin allocator needs free ids.
    if not 1 <= values['GROUND_ITEMS_PER_MAP'] <= 1000:
        raise ConfigError(f'GROUND_ITEMS_PER_MAP: {values["GROUND_ITEMS_PER_MAP"]} must be 1..1000')
    if not 0 <= values['REINFORCE_SUCCESS_PCT'] <= 100:
        raise ConfigError(f'REINFORCE_SUCCESS_PCT: {values["REINFORCE_SUCCESS_PCT"]} must be 0..100')
    if not 0 <= values['GATHER_RATE_DIVISOR'] <= 100_000_000:
        raise ConfigError(f'GATHER_RATE_DIVISOR: {values["GATHER_RATE_DIVISOR"]} must be 0 (weights) '
                          f'or a positive rate unit')
    if values['DAMAGE_FORMULA'] not in DAMAGE_FORMULAS:
        raise ConfigError(f'DAMAGE_FORMULA: {values["DAMAGE_FORMULA"]!r} is not one of {DAMAGE_FORMULAS}')
    if values['MOB_AI_COMMAND'] not in MOB_AI_COMMANDS:
        raise ConfigError(f'MOB_AI_COMMAND: {values["MOB_AI_COMMAND"]!r} is not one of {MOB_AI_COMMANDS}')
    for key in ('MOB_AI_TICK_SECS', 'MOB_LEASH_PX', 'MOB_AGGRO_TIMEOUT_SECS', 'MOB_ATTACK_REACH_X',
                'MOB_ATTACK_REACH_Y'):
        if values[key] <= 0:
            raise ConfigError(f'{key}: must be > 0')
    if not math.isfinite(values['MOB_ATTACK_HOLD_X']) or values['MOB_ATTACK_HOLD_X'] < 0:
        raise ConfigError(f'MOB_ATTACK_HOLD_X: {values["MOB_ATTACK_HOLD_X"]} must be a finite number >= 0 '
                          f'(0 = no hysteresis)')
    if not math.isfinite(values['MOB_ATTACK_HOLD_SECS']) or values['MOB_ATTACK_HOLD_SECS'] < 0:
        raise ConfigError(f'MOB_ATTACK_HOLD_SECS: {values["MOB_ATTACK_HOLD_SECS"]} must be a finite number '
                          f'>= 0 (0 = no hold): an endless hold parks a mob swinging at a target just '
                          f'out of reach')
    if values['MOB_MAP_KEEP_SECS'] < 0:
        raise ConfigError(f'MOB_MAP_KEEP_SECS: {values["MOB_MAP_KEEP_SECS"]} must be >= 0 (0 = discard at once)')
    tiles = values['BOSS_TILE_VALUES']
    if not all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 0x7FFFFFFF for v in tiles):
        raise ConfigError(f'BOSS_TILE_VALUES: map tile value_num numbers (positive ints), got {tiles!r}')
    if not 0 < values['BOSS_RESPAWN_SCALE'] <= 1000:
        raise ConfigError(f'BOSS_RESPAWN_SCALE: {values["BOSS_RESPAWN_SCALE"]} must be above 0 and at most 1000')
    if values['DROP_MODE'] not in DROP_MODES:
        raise ConfigError(f'DROP_MODE: {values["DROP_MODE"]!r} is not one of {DROP_MODES}')
    if not 1 <= values['DROP_RATE_UNIT'] <= 100_000_000:
        raise ConfigError(f'DROP_RATE_UNIT: {values["DROP_RATE_UNIT"]} must be 1..100000000')
    for key in ('MOB_ATTACK_B', 'MOB_DASH'):
        if values[key] not in MOB_COMMAND_SCOPES:
            raise ConfigError(f'{key}: {values[key]!r} is not one of {MOB_COMMAND_SCOPES}')
    if values['DEAD_PEER_SECS'] < 0:
        raise ConfigError(f'DEAD_PEER_SECS: {values["DEAD_PEER_SECS"]} must be >= 0 (0 = off)')
    gap = values['RELAY_START_HOLD_GAP_MS']
    if gap != 0 and not RELAY_START_HOLD_GAP_RANGE[0] <= gap <= RELAY_START_HOLD_GAP_RANGE[1]:
        raise ConfigError(f'RELAY_START_HOLD_GAP_MS: {gap} must be 0 (legacy clamp) or '
                          f'{RELAY_START_HOLD_GAP_RANGE[0]}..{RELAY_START_HOLD_GAP_RANGE[1]} (the 210 ms busy '
                          f'cadence plus a tick, up to the 990 ms hold cap)')
    if values['RELAY_SETTLE_AFTER_MS'] < RELAY_SETTLE_AFTER_MIN_MS:
        raise ConfigError(f'RELAY_SETTLE_AFTER_MS: {values["RELAY_SETTLE_AFTER_MS"]} must be >= '
                          f'{RELAY_SETTLE_AFTER_MIN_MS} (a busy mover sends every 210 ms)')
    if not 30 <= values['RELAY_SETTLE_HOLD_MS'] <= 990:
        raise ConfigError(f'RELAY_SETTLE_HOLD_MS: {values["RELAY_SETTLE_HOLD_MS"]} must be 30..990')
    hurt = values['MOB_HIT_RELAY_HURT_MS']
    if not 0 <= hurt <= 0xFFF:
        raise ConfigError(f'MOB_HIT_RELAY_HURT_MS: {hurt} must be 0..4095 (the 12-bit hi word)')
    recover = values['MOB_HIT_RECOVER_SECS']
    if not math.isfinite(recover) or recover < 0:
        raise ConfigError(f'MOB_HIT_RECOVER_SECS: {recover} must be a finite number >= 0 (the floor '
                          f'of the chase gate; 0 = no gate)')
    margin = values['MOB_HIT_RECOVER_MARGIN_SECS']
    if not (math.isfinite(margin) and MOB_HIT_RECOVER_MARGIN_RANGE[0] <= margin <= MOB_HIT_RECOVER_MARGIN_RANGE[1]):
        raise ConfigError(f'MOB_HIT_RECOVER_MARGIN_SECS: {margin} must be {MOB_HIT_RECOVER_MARGIN_RANGE[0]}..'
                          f'{MOB_HIT_RECOVER_MARGIN_RANGE[1]} (the gate is the stun plus this: 60 ms of '
                          f'tick rounding and a tick of relay latency at least)')
    ice = values['MOB_HIT_ICE_CHASE_SECS']
    if not (math.isfinite(ice) and (ice == 0 or 0 < ice < MOB_HIT_ICE_CHASE_MAX_SECS)):
        raise ConfigError(f'MOB_HIT_ICE_CHASE_SECS: {ice} must be 0 (the plain stun gate) or above 0 and '
                          f'below {MOB_HIT_ICE_CHASE_MAX_SECS} (the chase word must come before the '
                          f'copies stall at the end of the 0x2A node hold)')
    if not 0 < values['MOB_CMD_KEEPALIVE_SECS'] < MOB_COMMAND_HOLD_SECS:
        raise ConfigError(f'MOB_CMD_KEEPALIVE_SECS: {values["MOB_CMD_KEEPALIVE_SECS"]} must be above 0 and '
                          f'below the 0x2A node hold ({MOB_COMMAND_HOLD_SECS} s), or a chasing mob stops')
    notice = notice_bytes(values['NOTICE'])
    if len(notice) > NOTICE_MAX_BYTES:
        raise ConfigError(f'NOTICE: {len(notice)} bytes, the client reads at most {NOTICE_MAX_BYTES}')
    try:
        ip_host_order(values['PUBLIC_IP'])
    except (OSError, ValueError):
        raise ConfigError(f'PUBLIC_IP: {values["PUBLIC_IP"]!r} is not a dotted IPv4 address') from None
    chans = values['CHANNELS']
    if not 1 <= len(chans) <= MAX_CHANNELS:
        raise ConfigError(f'CHANNELS: 1..{MAX_CHANNELS} entries (S2C 0x01 slot table), got {len(chans)}')
    last = 0
    for c in chans:
        if not isinstance(c, dict) or not isinstance(c.get('no'), int) or isinstance(c.get('no'), bool):
            raise ConfigError(f'CHANNELS: every entry needs an int "no", got {c!r}')
        if not last < c['no'] <= 0xFF:
            raise ConfigError(f'CHANNELS: channel numbers must be ascending u8 values, got {c["no"]} after {last}')
        last = c['no']
        if c.get('ip') is not None:
            try:
                ip_host_order(c['ip'])
            except (OSError, ValueError, TypeError):
                raise ConfigError(f'CHANNELS: channel {c["no"]} ip {c["ip"]!r} is not a dotted IPv4 address') from None
    ips = [c.get('ip') or values['PUBLIC_IP'] for c in chans]
    if len(set(ips)) != len(ips):
        # Not fatal, but the client always connects to port 7022 (VA 0x44080E), so two
        # channels on one IP are the same server twice (login_character.md F1 step 4).
        log.warning(f'[CONFIG] CHANNELS: {ips} - channels sharing an IP are the same game '
                    f'server (the client\'s game port 7022 is hard-coded)')


def defaults():
    """The default configuration (deep-copied), without reading any file."""
    return Config(json.loads(json.dumps(DEFAULTS)))


def from_dict(overrides, path=None):
    values = json.loads(json.dumps(DEFAULTS))
    for key, value in (overrides or {}).items():
        if key.startswith('_'):
            continue
        if key in RETIRED_KEYS:
            log.warning(f'[CONFIG] {key!r} ignored: {RETIRED_KEYS[key]}')
            continue
        if key not in DEFAULTS:
            log.warning(f'[CONFIG] unknown key {key!r} ignored')
            continue
        values[key] = _check_type(key, value, DEFAULTS[key])
    _validate(values)
    if values['GAME_PORT'] != CLIENT_GAME_PORT and values['CLIENT_BUILD'] == BUILD_2008:
        log.warning(f'[CONFIG] GAME_PORT {values["GAME_PORT"]}: the EN client always connects to '
                    f'{CLIENT_GAME_PORT} (VA 0x44080E)')
    return Config(values, path)


def load(path=None):
    """Config from `path`, else $WS_CONFIG, else server/config.json. A missing file gives
    the defaults; a malformed one raises ConfigError."""
    path = path or os.environ.get(ENV_PATH) or DEFAULT_PATH
    if not os.path.exists(path):
        log.info(f'[CONFIG] {path} not found; using defaults')
        return Config(defaults(), path)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except ValueError as e:
        raise ConfigError(f'{path}: not valid JSON: {e}') from None
    if not isinstance(data, dict):
        raise ConfigError(f'{path}: top level must be an object')
    return from_dict(data, path)
