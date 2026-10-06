"""
WindSlayer Private Server
========================
Reverse-engineered server for WindSlayer English client (~2008).

Architecture:
  - Version Server (TCP 7011): Send version response + channel list, close.
    After close, client shows launcher. User clicks Start → connects to 7022
    (hard-coded in the client at VA 0x44080E; the version reply carries only the IP).

  - Game Server (TCP 7022): Full game protocol (Fireway).
    1. Server sends key exchange (opcode 0x5A, seq=1, NoEncode flag)
    2. Client calls SetCodeKey(seed) - all further packets encrypted
    3. Client sends login (opcode 0x01, encrypted)
    4. Server sends login response (opcode 0x02, encrypted)
    5. Character select, enter game, etc.

Ports, PUBLIC_IP, channels, the notice and maintenance flag, the accounts file and save
timing come from config.json (config.py, roadmap F12). CLIENT_BUILD picks the client the
server speaks to: '2008' (default) or '2009' (EN Outspark v1.04 Build 14, client-2009-login:
protocol_spec_2009.json, SSO login, 82-byte character records, version 14 + channel port;
client-2009-world: the 2009 world layouts - 0x08 reason byte, 0x1A/0x07 2009 records, 0x21
guild gate, ROUTES_2009 with the 0x8A guild-info and 0x2C room-list requests, 2009 content).
Accounts and characters live in store.py (F4), online sessions in world.py (F3/F5), password hashing in auth.py and the
character name rules in names.py (login_character.md 3.5.4).
"""

import collections
import datetime
import contextlib
import socket
import struct
import threading
import time
import traceback
import sys
import logging
import json
import os
import random
import secrets
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from cencmsg import CEncMsg

import auth
import bank_tabs as BT
import bosses as bossmod
import buffs as buffmod
import cards as cardsmod
import cash as cashmod
import cashuse as cashusemod
import chat as chatmod
import client_layout as CL
import classchange as CC
import combat
import config as cfgmod
import crafting as craftmod
import damage as dmgmod
import debuffs as debuffmod
import en_content as EC
import events as eventmod
import gm
import ground as groundmod
import hitstun as HS
import hpmp
import ids
import inventory as invmod
import mall as mallmod
import market as marketmod
import messenger as msgrmod
import mobai
import names
import packets as P
import party as partymod
import pets as petsmod
import presence
import privacy
import progression
import quests as questmod
import records as R
import registry
import reputation as repmod
import shop as SH
import skills as SK
import social
import store as storemod
import ticks
import trade as trademod
import village
import world as worldmod
from registry import Route, STYLE_RAW, STYLE_REC  # noqa: F401 - STYLE_* for route authors
from world import World

# Map code -> client map file name, still the KR Yahoo dump; logs and the dev `!warp`
# map-code check only. The portal topology and the EN map names come from the client's own
# .hmi/.hsi (en_maps.py, world-portal-table-en) and every item/npc path from en_content.py
# (F7).
from data import map_codes, map_filename


# ============================================================================
# EN client content (roadmap F7, arch-content-loader). en_content.py owns every
# catalog: hs/windslayer.hii items, hs/windslayer.hqi quests, the card list and the
# KR gamedef rows the EN files do not carry. The old helpers here - a KR
# `_gamedef_item` (wrong Kind for 289 items), the `_EN_ITEM_OVERRIDE = {377: 179}`
# patch over the hex quest parse, `_KIND_TO_GRID` guessed from PySlayer's KR slot
# order and an id-only `_en_catalog` - are all retired by it.
# ============================================================================
# The EN 2008 item table (hii header "Number_of_ITEM: 4248"). The client's item lookup
# FUN_00404210 returns NULL for id 0 or an id past the table and every item handler then
# silently does nothing, so a KR gamedef id above 4248 (e.g. drop 4356) used to be credited
# by the server but never reached the bag (LIVE_TEST_LOG bug 6, S2-11, item_inventory B4).
EN_ITEM_MAX_ID = EC.EN_ITEM_MAX_ID


def en_item(item_id):
    """The EN item definition (hii record) or None."""
    return EC.items().get(item_id)


def en_item_type(item_id):
    """Client item Type (0 stack, 1 equipment, 2 misc, 3 skill, 4 class change, 5 cash) or
    None for an id the EN client does not have."""
    return EC.items().type_of(item_id)


def en_item_exists(item_id):
    """True when the EN client has a definition for item_id. The single gate for every item
    the server grants, drops, sells or mirrors into the bag model: an id the client ignores
    must never change server state or go out in 0x18/0x19/0x1D (S2-11)."""
    return EC.items().exists(item_id)


def _log_text(value, limit=40):
    """Client text for a log line: NUL-cut and decoded as cp949, the client's codepage
    (roadmap 1.2.4: text stays raw bytes on the wire, cp949 is for logs only)."""
    return P.cut_text(value, limit).decode('cp949', 'replace')


# ============================================================================
# PHASE 1 COMBAT — server-authoritative monster model + content tables.
# The client only renders what 0x1A (spawn) / 0x14 (hp) / 0x29 (death) tell it.
# ============================================================================
@dataclass
class Monster:
    uid: int                 # entity uid; MOB_UID_BASE+index (ids.MONSTER, never a player uid)
    npccode: int             # EN hni template idx: quest ReqPro NPC, cards CardNpc, kill credit
    name: str
    level: int
    hp: int
    max_hp: int
    body_atk: int            # Body_Atk: its contact hit on a player (C2S 0x0D events 1/6)
    defense: int             # Def: the victim side of every hit on it (damage.py level scale)
    exp: int                 # exp granted to killer
    x: float
    y: float
    spawn_x: float = 0.0
    spawn_y: float = 0.0
    alive: bool = True
    aggro_uid: int = 0
    drop_items: list = field(default_factory=list)
    gold: int = 0
    # S2C 0x1A template_index (entity +0xE60): the 1-based place of the npccode's record in
    # hs/windslayer.hni, which is the order the client appends the game_state+0x4E0 list in
    # (world-template-map). 0 = "same as npccode", for hand-built test monsters.
    template: int = 0
    # cs-monster-death: True once S2C 0x06 removed the corpse from the client scene, so the
    # respawn 0x1A never lands on a uid that still has an entity (S1-07 duplicate entity).
    despawned: bool = False
    timers: list = field(default_factory=list)   # pending ticks.Timer (despawn, respawn)
    # cs-debuffs: {family base id: record} of the targeted slots (S2C 0x41) running on this
    # monster - DoT ticks, a pending detonation, a control effect (debuffs.py).
    debuffs: dict = field(default_factory=dict)
    # Monster aggro (mobai.py, MONSTER_AGGRO_RE_2026-09-23). The template's swing stats and AI
    # ints as the 0x1A handler copies them onto the entity (Weak_Atk +0x11D4, Strong_Atk
    # +0x11D0, 0x456B9E..0x456C10; AI to +0xE50.. @0x456C99 [+0xDC0.. @0x451F67]), then the
    # server-side AI state the field client never keeps (its host engine is off):
    weak_atk: int = 0
    strong_atk: int = 0
    # The hni element columns FUN_0041b830 derives the monster's element from (damage.py): type
    # 4..7 = element type-3 with Attri_Atk its element attack and Attri_Def its resist.
    elem_type: int = 0
    attri_atk: int = 0
    attri_def: int = 0
    ai: tuple = ()                   # hni AI: ints padded to 13 (mobai.pad_ai)
    hate: dict = field(default_factory=dict)   # {attacker uid: damage}, the +0x1410 list
    ai_dir: int = 0                  # direction last commanded (mobai.DIR_*): kept within 65 px
    ai_lo: int = -1                  # lo word last commanded (-1: none since the hand-back)
    ai_sent_t: float = 0.0           # when that command went out (keep-alive)
    ai_hit_t: float = 0.0            # last hit that fed the hate list (aggro timeout)
    ai_hold_end: float = 0.0         # when the last command node's hold runs out
    # The last attack motion commanded, A or B. Nothing in the server reads it any more:
    # livefix's L2 rule dropped the contact filter that did, and the swing gate reads the
    # per-kind times below. Still written on every attack send, for the tests that check it.
    ai_attack_t: float = 0.0
    # P13 boss-b3 (bosses.note_command / swing_age): the last attack A and attack B commanded -
    # what the swing gate reads: a swing event hurts only after its own kind (7/8 after A,
    # 4/5/9/10 after B) within MOB_ATTACK_EVENT_SECS.
    ai_attack_a_t: float = 0.0
    ai_attack_b_t: float = 0.0
    ai_attack_start_t: float = 0.0   # when the running attack word was first commanded (hold)
    ai_owned: bool = False           # a 0x2A set the client's +0x971: the server drives it
    # world-shared-monsters: the viewer keys (GameServer._viewer_key) whose client got a 0x2A
    # for this mob since it got the mob - its copy runs the server's words (+0x971 = 1), so a
    # client not in here gets the current word at once (_mob_send_decision).
    ai_takers: set = field(default_factory=set)
    aggro_x: float = 0.0             # x where the current chase began (the chase leash origin)
    ai_step_t: float = 0.0           # last dead-reckoning step
    fix_t: float = 0.0               # last position fix (driver sample, 0x0D interact tail)
    # P13 boss-b2 (bosses.roll_drops): the template's hni item/Drop columns as [(item, rate)]
    # (EN-grantable ids only), rolled per entry by DROP_MODE 'rates' / 'single'.
    drop_table: list = field(default_factory=list)
    # P13 boss-b1 (bosses.py): the boss ledger key (channel, map, (tile x, tile y)) of a field
    # boss (a map tile with value_num in BOSS_TILE_VALUES), None for every other monster.
    boss: tuple = None
    # Desync fix P3 (config MOB_SPEED_FROM_TEMPLATE): the template's walk speed in px/s,
    # 1e7 / its hni `speed` (the entity's +0x1280 = speed x 0.0001 and one walk step is
    # tick / +0x1280 px, 0x4163EE: Ssiyo 120000 -> 83.3 px/s, measured 82.5; Monkey Soldier
    # 80000 -> 125 px/s = 3.75 px per 30 ms tick). No speed: the measured 82.5.
    walk_px_s: float = 82.5
    # Desync fix M1 (config MOB_HIT_RECOVER_SECS): no chase word before this monotonic time -
    # every client's copy is still in the hurt a client-caught swing or skill hit (interact
    # event 7 / 9) started (_release_hit_lock), or an attack skill the server's box landed
    # is about to start (MOB_HIT_CAST_GATE, _skill_attack); _mob_send_decision holds it.
    ai_recover_until: float = 0.0
    # When the copies' hurt of the last client-caught swing / skill hit ends (the hit + its
    # stun, _arm_hit_gate): they stand in state 3 until then whatever word they hold, so the
    # server's dead reckoning (_mob_dead_reckon) starts no earlier - an ice hit's chase word
    # goes out inside the stun (MOB_HIT_ICE_CHASE_SECS).
    ai_stun_until: float = 0.0
    # The same for an ice-element hit's stun (hurt + 1000, MOB_HIT_ICE_CHASE_SECS on), else 0,
    # and when its chase word is due (the gate it set): while that stun runs under a LATER
    # gate (someone's cast), the copies' last word is kept alive (_mob_send_decision), or
    # their state machine stalls when its 960 ms node hold runs out.
    ai_ice_until: float = 0.0
    ai_ice_chase_t: float = 0.0
    # The last client-caught swing / skill hit's time and stun (s), kept until the first word
    # to the copies after it (_mob_word_after_stun): a stun past the 0x2A node's 960 ms hold
    # stalls both copies at the hold's end until a word comes, then runs its rest.
    ai_stun_t: float = 0.0
    ai_stun_secs: float = 0.0

    @property
    def flags(self):
        """The chase branch's template flags (attack A/B, dash, no-jump, stationary, ...)."""
        return mobai.Flags.of(self.ai)


# Monster stats and placement (world-template-map, roadmap F7). The EN client's own files
# are the source: the template record in hs/windslayer.hni (Lv, HP, Body_Atk, Def, Exp and
# the item drop column, EN name from hs/NPCLngKo.lng: 1 = "Pupu", LIVE_TEST_LOG bug 7) and
# the spawn points in the map's .hmi event-2 tiles (en_content.map_spawns). They replace
# the hand-written MONSTER_DB (KR names such as "Seeyo"/"Coring", KR exp, a dev HP of 24
# for Pupu where the client template says 5) and MAP_SPAWNS (102 only; its eight points are
# exactly map 102's eight NpcId=1 tiles). KR gamedef.sqlite3 disagrees with the EN hni on
# Exp for 64 of the 180 templates (KR Pupu 10, EN 5) and on type for 59; the EN numbers win.
#
# Gold is the one number no client file carries (neither the hni nor the KR npcs table has
# a column): the values the server has always paid for the first templates stay, and every
# other template pays MONSTER_GOLD_PER_EXP x its EN Exp (the old table is ~1.4 x EN Exp).
MONSTER_GOLD = {1: 7, 2: 10, 3: 14, 4: 20, 5: 25, 6: 20, 12: 60}
MONSTER_GOLD_PER_EXP = 1.4


def monster_gold(npccode, exp):
    if npccode in MONSTER_GOLD:
        return MONSTER_GOLD[npccode]
    return max(1, int(round(exp * MONSTER_GOLD_PER_EXP)))


# Monster uids live in the server-wide monster space of ids.py (F3); per-session index
# allocation stays until world-shared-monsters.
MOB_UID_BASE = ids.MONSTER.lo
# Monster lifecycle (cs-monster-death + world-entity-lifecycle, roadmap D4, combat_skill F6).
# Death: S2C 0x29 with respawn_tick 0x7FFFFFFF and the spawn point. The client's entity loop
# (FUN_00413920 case 0x16) revives a type-4 corpse by itself once respawn_tick - scene clock
# < 0; the clock is seeded to 1000 by 0x03, so the old tick 0 revived every corpse at (0, 0)
# with full HP (S1-07). 0x7FFFFFFF keeps it a corpse (~24 days of clock). The 0x29 x/y are
# also the monster's leash home (FUN_00417e10). The corpse gets S2C 0x06 after
# MOB_CORPSE_SECS (death animation), and a fresh 0x1A at the spawn point MOB_RESPAWN_SECS
# after death, both from the tick scheduler (ticks.py), so kills made by the memory combat
# driver respawn too (they never did: respawns only ran on C2S 0x38/0x25).
MOB_CORPSE_SECS = 3.0
MOB_RESPAWN_SECS = 15.0
MOB_RESPAWN_HOLD_TICK = 0x7FFFFFFF
# Ground items (monster loot, dropped bag/worn items) live in ground.py: u16 ids PER MAP
# (ids.GROUND_ITEM). The old GROUND_ITEM_UID_BASE 0x00200000 truncated to 0 in that u16
# field (item_inventory.md B11) and is gone (P4 stage 2).

# Leveling is CLIENT-SIDE: the 0x21 ExpDelta handler adds the delta to scene+0x278, and
# when a threshold is crossed the client auto-levels (entity+0x99, max HP/MP via 0x427d40,
# full heal, sound 0xa2 + effect 0x24). The exp table and level rule are progression.py
# (the old EXP_TABLE held per-level increments read as cumulative thresholds, S1-06).
# Basic-attack reach around the player's position, used by the memory-driven combat
# driver (_memory_melee). The EN basic attack sends no packet; C2S 0x38 is the
# Battlefield window request (S1-12), not a swing.
MELEE_RANGE_X = 130.0
MELEE_RANGE_Y = 90.0
# C2S 0x0D interact event (state_lo bits 16-19 = attacker +0x950) the client sends on the tick
# after its own hit detection caught a connecting swing: 7 = hit, victim in target_uid (61 B).
HIT_REPORT_EVENT = 7
# Monster wander DISABLED: the agent's 0x12="walk" was wrong — in-client it spawns
# spurious ground loot, not movement. Needs the real entity-move opcode (future RE).
WANDER_ENABLED = False
WANDER_RADIUS = 80.0
WANDER_PERIOD = 4.0
# How often the combat driver writes the attached client's own coordinates and facing into
# its session (world-persistence / premium_cash-map-replay-helper). cs-skill-damage lays the
# skill hitbox from this point, so it is sampled 4x a second (a walking player moves well
# over a hitbox width in the old 1 s); three memory reads each time.
POSITION_SAMPLE_SECS = 0.25
# How long a facing named by a C2S 0x0D outranks the driver's +0x8BD sample.
FACING_PACKET_HOLD_SECS = 1.5
# How often the combat driver re-finds the local player even when its cached entity
# still looks valid (a freed block keeps the uid). ~60 reads per revalidation.
DRIVER_REVALIDATE_SECS = 2.0

# Quests: the EN client's own hs/windslayer.hqi through en_content (F7). quest_defs.py,
# which read the id and count arrays as hex and only 10 reward slots, is retired: it
# decoded 242 of the 291 quests wrongly (Q-B1). The client's active log is 3 slots
# (quests.MAX_ACTIVE).
MAX_ACTIVE_QUESTS = questmod.MAX_ACTIVE

# Localhost-only debug packet injection (GameServer._admin_listener); config ADMIN_PORT.
ADMIN_PORT = cfgmod.DEFAULTS['ADMIN_PORT']

# Chat and system lines (chat_mail_gm-chat-builders / -system-notices, chat_mail_gm.md F1/F4).
# S2C 0x16 text lands in a 61-byte client stack buffer (packets.TEXT_FIELDS clamps it too).
CHAT_TEXT_MAX = 60
# S2C 0x15 SystemMessage kinds -> (msg_type, text prefix). The client colours type 0 light
# blue, type 1 yellow-green, and type 2 by the text: red for "[Warni...", light blue for
# "[Annou..." (handler 0x4519C9). Types >= 3 pick an uninitialised colour.
NOTICE_KINDS = {
    'plain': (0, ''),
    'info': (1, ''),
    'warn': (2, '[Warning] '),
    'announce': (2, '[Announce] '),
}
WELCOME_TEXT = 'Welcome to WindSlayer!'


def setup_logging():
    """Called from main() only. At import time this used to open server_live.log with
    mode='w' relative to the cwd, so any `import windslayer_server` (offline tests, a
    quick import check next to a running server) wiped the live server's log and
    appended fake traffic to server_history.log, which test_protocol.py treats as real
    client bytes."""
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('server_live.log', mode='w', encoding='utf-8'),
            # Accumulates across restarts: every real packet ever exchanged, used by
            # test_protocol.py to check the RE spec against live traffic.
            logging.FileHandler('server_history.log', mode='a', encoding='utf-8'),
        ]
    )


log = logging.getLogger('WS')

# ============================================================================
# Constants
# ============================================================================

HEADER_SIZE = 8
SIZE_MASK = 0x7FF
MAX_PKT = 0x7FF
NO_ENCODE_FLAG = 0x800

# ============================================================================
# Packet I/O
# ============================================================================

def make_raw_packet(body, seq=0, no_encode=False):
    """Build a Fireway packet. Body must include opcode as first byte."""
    total = HEADER_SIZE + len(body)
    if total > MAX_PKT:
        # 11-bit size field: masking a longer length would corrupt the stream (P8 review).
        raise ValueError(f'Fireway frame of {total} B > {MAX_PKT} B')
    dword0 = (total & SIZE_MASK) | ((seq & 0xFF) << 12)
    if no_encode:
        dword0 |= NO_ENCODE_FLAG
    return bytearray(struct.pack('<II', dword0, 0) + body)


def read_raw_packet(sock, timeout=60.0):
    """Read one Fireway packet. Returns bytearray or None."""
    sock.settimeout(timeout)
    buf = bytearray()

    while len(buf) < HEADER_SIZE:
        try:
            chunk = sock.recv(HEADER_SIZE - len(buf))
        except (socket.timeout, ConnectionError, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)

    size_dword = struct.unpack_from('<I', buf, 0)[0]
    pkt_size = size_dword & SIZE_MASK

    if pkt_size < HEADER_SIZE or pkt_size > MAX_PKT:
        log.warning(f'Invalid packet size: {pkt_size} (raw dw0: 0x{size_dword:08X})')
        return None

    while len(buf) < pkt_size:
        try:
            chunk = sock.recv(pkt_size - len(buf))
        except (socket.timeout, ConnectionError, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)

    return buf


def parse_packet(buf):
    """Parse packet → (size, seq, no_encode, opcode, payload)."""
    dw0 = struct.unpack_from('<I', buf, 0)[0]
    pkt_size = dw0 & SIZE_MASK
    seq = (dw0 >> 12) & 0xFF
    no_enc = bool(dw0 & NO_ENCODE_FLAG)
    body = bytes(buf[HEADER_SIZE:pkt_size])
    opcode = body[0] if body else 0
    payload = body[1:] if body else b''
    return pkt_size, seq, no_enc, opcode, payload


def hexdump(data, max_bytes=512):
    lines = []
    show = data[:max_bytes]
    for i in range(0, len(show), 16):
        chunk = show[i:i+16]
        h = ' '.join(f'{b:02X}' for b in chunk)
        a = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'    {i:04X}: {h:<48s}  {a}')
    if len(data) > max_bytes:
        lines.append(f'    ... ({len(data)} bytes total)')
    return '\n'.join(lines)


# ============================================================================
# Version Server (port 7011) - sends version response and closes
# ============================================================================

class VersionServer:
    """
    Port 7011. Sends version response (opcode 0x01, seq=1) and closes.
    After close, launcher shows start button. Clicking Start connects to 7022.

    Everything in the reply comes from config (lc-version-config, S2-30/B16): the game IP
    the client will connect to (PUBLIC_IP, host order), the channel table, the notice text
    and the maintenance flag. `user_counts` is a callable returning {channel_no: online}
    so the launcher's load colour (user_count/200) is live instead of a constant 0.

    CLIENT_BUILD '2009' (spec_2009 0x01, live-verified): version_code 14 and every channel
    entry is u8 no, u16 users, u32 ip (host order), u32 port - the 2009 client connects to
    that port (ConnectToGameServer 0x440C70) instead of a hard-coded 7022, so it is GAME_PORT.
    """

    def __init__(self, host='0.0.0.0', port=7011, config=None, user_counts=None):
        self.host = host
        self.port = port
        self.config = config if config is not None else cfgmod.defaults()
        self._user_counts = user_counts

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(5)
        log.info(f'[VERSION] Listening on {self.host}:{self.port}')

        while True:
            try:
                client, addr = srv.accept()
                log.info(f'[VERSION] Connection from {addr}')
                threading.Thread(target=self._handle, args=(client, addr), daemon=True).start()
            except Exception as e:
                log.error(f'[VERSION] Accept error: {e}')

    def user_counts(self):
        """{channel_no: logged-in accounts}. Empty (all 0) without a provider."""
        try:
            return dict(self._user_counts() or {}) if self._user_counts else {}
        except Exception:                            # noqa: BLE001 - the launcher must still get a reply
            log.exception('[VERSION] user count provider failed; sending zeros')
            return {}

    def _build_version_body(self):
        """
        Version response body (S2C 0x01, login_character.md F1 step 2).

        The ULONG field at [this+0x129C] is the IP address used by
        ConnectToGameServer (VA 0x440805-0x440814). Port is hardcoded to 7022.
        Everything here is config (lc-version-config): version_code (3, or 0xEA61 in
        maintenance - never any other value, that starts the dead CDN auto-patcher), the
        notice text, the channel table and each channel's IP. The default config still
        gives the live-proven bytes (one channel, 127.0.0.1, the OUTSPARK notice).
        """
        cfg = self.config
        counts = self.user_counts()
        with_port = cfg.CLIENT_BUILD == cfgmod.BUILD_2009
        body = bytearray()
        body.append(0x01)                                        # opcode = version response
        body.extend(struct.pack('<H', cfg.version_code()))       # 3 (2009: 14) = OK / 0xEA61 = maintenance

        message = cfg.notice()                                   # <= 1000 B (config validates)
        body.extend(struct.pack('<H', len(message)))
        body.extend(message)

        # Channel data (parsed by 0x4404D0)
        channels = cfg.channels()
        num_channels = len(channels)
        body.append(0x01)                           # server count (<= 4)
        body.append(cfgmod.CHANNEL_STATUS_OPEN)     # server status: 3 lets START connect
        body.append(num_channels)                   # channel slot count (<= 10)
        body.append(num_channels)                   # channel entry count

        # IP is passed through htonl() by Fireway Connect (VA 0x10001CA0).
        # So the stored ULONG must be in HOST byte order.
        # 127.0.0.1: a=127, b=0, c=0, d=1
        # In host order: 0x7F000001 -> little-endian bytes: 01 00 00 7F
        for channel_no, _name, ip in channels:
            body.append(channel_no)                  # channel number (config: strictly ascending)
            # Live player count; the launcher paints the load bar as user_count/200.
            body.extend(struct.pack('<H', min(int(counts.get(channel_no, 0)), 0xFFFF)))
            body.extend(struct.pack('<I', cfgmod.ip_host_order(ip)))   # IP (host order; client htonl's it)
            if with_port:
                # spec_2009 0x01 game_server_port (0x440B38): the port Connect uses; 0 or a
                # wrong one breaks the game connection.
                body.extend(struct.pack('<I', int(cfg.GAME_PORT)))
        return bytes(body)

    def _handle(self, sock, addr):
        try:
            body = self._build_version_body()
            pkt = make_raw_packet(body, seq=1)
            mode = ('MAINTENANCE (0xEA61)' if self.config.MAINTENANCE
                    else f'version {self.config.version_code()}, client build {self.config.CLIENT_BUILD}')
            log.info(f'[VERSION] Sending version response ({len(pkt)} bytes, {mode}, '
                     f'game IP {self.config.PUBLIC_IP}, {len(self.config.channels())} channel(s))')
            log.debug(f'[VERSION] Packet hex:\n{hexdump(pkt)}')
            sock.sendall(bytes(pkt))
            time.sleep(0.1)
        except Exception as e:
            log.error(f'[VERSION] Error: {e}')
            traceback.print_exc()
        finally:
            sock.close()
            log.info(f'[VERSION] Closed {addr}')


# ============================================================================
# Game Server (port 7022) - Fireway protocol
# ============================================================================

def routes_2009(base):
    """The C2S route table of the EN 2009 client from the 2008 one (client-2009-world).

    Changed meaning (spec_2009 diff_vs_2008):
      0x2C  u8 list_type: 1 arena / 4 play room / 5 dungeon list opened, 0 = any closed
            (replaces 2008 0x2C/0x2D/0x74/0x75) -> _handle_room_list
      0x2D  InstanceDungeonRoomKick (u8 0 + u32 uid) - no dungeon rooms exist
      0x93/0x94  BlacklistAdd / BlacklistRemove (2008: room-host dead code)
      0x95/0x97/0x99  InstanceDungeonStart / GuildBattleRosterAdd / GuildBattleRoomCreate
      0x08/0x09/0x0A  the renumbered room-host dead code (2008 0x93/0x94/0x95)
    New in 2009: 0x4D/0x82/0x83/0x85/0x86 pets, 0x4E/0x80/0x81 cash item options and sales,
    0x87..0x92 + 0x96..0x9D guild and guild battle, 0x8A GuildInfoRequest (sent by the
    client itself after every S2C 0x03 -> 0xB3 sub 15), 0x9E X-Trap answer, 0xBC dead code.
    Gone: 0x74/0x75 (play room list open/close), 0x7C.
    Owned by P8 (ROADMAP_2009_ADDENDUM C5 / C8): 0x4D PetRename -> the planned refusal S2C
    0x73 {0} (pets.py, a waiting box), 0x4E -> S2C 0xC4 {0}, 0x80 -> S2C 0x71 {1, 0x17}
    (a waiting box), 0x81 -> S2C 0x71 {1, 0x16} on the seller's Cancel, else nothing
    (mall.py "The 2009-only cash opcodes")."""
    routes = {op: r for op, r in base.items()
              if op not in (0x2D, 0x74, 0x75, 0x7C, 0x93, 0x94, 0x95, 0x97, 0x99)}

    def consumed(op, what, why='no model yet'):
        return Route(log=f'[0x{op:02X}] {what} - consumed ({why})')

    no_pets = 'no pets: the server sends no pet packet'
    no_guild = 'no guilds: every player is "not in a guild" (0xB3 sub 15)'
    routes.update({
        0x2C: Route('_handle_room_list', style=STYLE_REC),
        0x2D: consumed(0x2D, 'instance dungeon room kick', 'no dungeon rooms (pvp owns rooms)'),
        **{op: Route('_warn_dead_host_code') for op in (0x08, 0x09, 0x0A, 0xBC)},
        0x4D: Route('_handle_pet_rename', style=STYLE_REC),                 # -> 0x73 {0} (C5)
        0x4E: Route('_handle_cash_add_option', style=STYLE_REC),            # -> 0xC4 {0} (C8)
        0x80: Route('_handle_cash_sale_offer', style=STYLE_REC),            # -> 0x71 {1, 0x17} (C8)
        0x81: Route('_handle_cash_sale_reply', style=STYLE_REC),            # 1 -> 0x71 {1, 0x16} (C8)
        0x82: consumed(0x82, 'pet equip', no_pets),
        0x83: consumed(0x83, 'pet unequip', no_pets),
        # Sent by the client itself on S2C 0xAE (pet HP <= 10), which is never sent, or when
        # an item the exe takes for pet food (KR ids 0x10BA..0x10BD = EN 4282..4285: Cruiser
        # Sword, the guild billboards, the Pet Bell - ROADMAP_2009_ADDENDUM X7 / X8) is used
        # from the bag. No waiting box; EN food reaches C2S 0x48 instead (pets.py, C6).
        0x85: consumed(0x85, 'pet feed', no_pets),
        0x86: consumed(0x86, 'pet emote', no_pets),
        0x87: consumed(0x87, 'guild create', no_guild),
        0x88: consumed(0x88, 'guild billboard place', no_guild),
        0x89: consumed(0x89, 'guild join request', no_guild),
        # Sent by the client itself at the end of EVERY S2C 0x03 (0x453581) and after an
        # S2C 0xB3 sub 19 (0x48456D, never sent).
        0x8A: Route('_handle_guild_info'),
        0x8B: consumed(0x8B, 'guild application accept', no_guild),
        0x8C: consumed(0x8C, 'guild application reject', no_guild),
        0x8D: consumed(0x8D, 'guild chat', no_guild),
        0x8E: consumed(0x8E, 'guild member kick/leave/disband', no_guild),
        0x8F: consumed(0x8F, 'guild notice change', no_guild),
        0x90: consumed(0x90, 'guild max member increase', no_guild),
        0x91: consumed(0x91, 'guild master change', no_guild),
        0x92: consumed(0x92, 'guild member grade change', no_guild),
        0x93: consumed(0x93, 'blacklist add', 'no blacklist model (social_friend)'),
        0x94: consumed(0x94, 'blacklist remove', 'no blacklist model (social_friend)'),
        0x95: consumed(0x95, 'instance dungeon start', 'no dungeon rooms (pvp owns rooms)'),
        **{op: consumed(op, 'guild battle request', no_guild) for op in range(0x96, 0x9E)},
        # Only an answer to S2C 0xC5, which the server never sends (packets.FORBIDDEN_S2C).
        0x9E: consumed(0x9E, 'X-Trap response', 'the server never sends S2C 0xC5'),
    })
    return routes


class GameServer:
    """
    Port 7022. Full game protocol (Fireway).

    Flow:
      1. Server sends key exchange (opcode 0x5A, seq=1, NoEncode flag)
         Payload: INT32 encryption seed
      2. Client calls SetCodeKey(seed) - all further packets encrypted
      3. Client sends login (opcode 0x01, encrypted)
         Payload: CHAR[41] username + CHAR[21] password
      4. Server sends login response (opcode 0x02, encrypted)
      5. Character select, enter game, etc.
    """

    def __init__(self, host='0.0.0.0', port=7022, db_file=None, config=None):
        self.host = host
        self.port = port
        self.sessions = {}
        self.config = config if config is not None else cfgmod.load()
        # client-2009-login: the client build this server speaks to ('2008' | '2009', config
        # CLIENT_BUILD). packets.send() and the registry's decoder read it from here, so the
        # spec is per server, never a module global (tests run both builds in one process).
        self.client_build = self.config.CLIENT_BUILD
        # client-2009-tooling: the memory layout of that build's exe, for the local-memory
        # combat driver (client_layout.py; 2009 from client_map_2009.json).
        self.client_layout = CL.layout(self.client_build)
        # 2009 relogin (spec_2009 C2S 0x01 session_key / S2C 0x02 session_key): the live u32
        # key of each account uid, issued at its last password login and kept after the
        # connection closes, because the relogin after a channel change arrives on a NEW
        # connection. Server memory only: a restart makes every old key a fresh login.
        self.session_keys = {}
        # F7: the EN client install that owns hs/windslayer.hii and .hqi. Every catalog is
        # loaded lazily from there (and the KR gamedef only for what those files lack).
        # Per build: CLIENT_DIR (2008) or CLIENT_DIR_2009 (Config.client_dir); the build also
        # picks the per-build tables en_content derives from those files (portal table, item
        # Kind -> equip slot, item names; client-2009-world).
        EC.configure(self.config.client_dir(), self.client_build)

        # Roadmap F8: one timer thread; callbacks run under world_lock. Monster corpse
        # despawn and respawn run here (cs-monster-death), and the store's debounced saves.
        # Lock order: world_lock (tick callbacks, the login claim) -> session['combat_lock']
        # -> the map's MapMonsters.lock (world-shared-monsters, _combat) -> store.lock
        # (db_lock) -> session['send_lock']. Handlers and the combat driver
        # never take world_lock while holding a later lock. The world registry (self.world)
        # is off this ladder: its index lock is a private leaf, so a handler holding its
        # combat lock may ask it for the map audience while a tick callback holds world_lock
        # and waits for that combat lock (world.py "Locks"; P5 stage 1 review deadlock).
        self.world_lock = threading.RLock()
        self.ticks = ticks.Scheduler(lock=self.world_lock, name='world')
        # Per-connection slot counter for `!who` and /kick N (chat_mail_gm 3.2, F11), under
        # its own leaf lock: _slot_of runs from handlers and must not need world_lock.
        self._next_slot = 0
        self._slot_lock = threading.Lock()
        # chat_mail_gm-gm-stop-kick: the running /stop or admin shutdown countdown ({why, t,
        # save, close} timers), None when none runs (_start_maintenance).
        self._maintenance = None
        # cs-skill-cast: {lower(character name): {family base: monotonic() of the last
        # accepted cast}}, shared by every session of that character (a relog keeps them).
        self.skill_cooldowns = {}
        # item_inventory-ground-loot-pickup (ground.py): the items lying on each map - monster
        # loot and dropped bag/worn items - with their u16 per-map ids. World state, not
        # persisted: an unpicked item despawns after GROUND_ITEM_SECS anyway.
        self.ground = groundmod.GroundRegistry(max_per_map=self.config.get('GROUND_ITEMS_PER_MAP', 100))
        # F3/F5: online sessions by account uid and character name (lc-uid-online). World
        # guards its indexes with its own leaf lock, never world_lock (see above).
        self.world = World()
        # Desync fix P2 review: {uid: [monotonic() of the last line, failures not logged
        # since]} of the movers whose settle raised on the 'presence-settle' tick
        # (_settle_failed; that tick is the only reader and writer).
        self._settle_fail_log = {}
        # F5 lifecycle hooks. The welcome line is registered instead of inlined in the
        # enter-world handler: every map load then runs the same MapTransfer sequence, and
        # the groups that must clean up before a map load (trade, stall, messenger, the 0x06
        # to the old map's peers) register their own callbacks on before_server_map_load
        # without touching the primitive.
        self.world.hooks.register(worldmod.ON_ENTER_WORLD, self._hook_welcome)
        # shop_storage-bank-model: a 2009 client opens the bank window with no packet, so its
        # bank block is sent at every world entry (after the welcome line).
        self.world.hooks.register(worldmod.ON_ENTER_WORLD, self._hook_bank_contents)
        # P7 stage 2 (shop_storage F9-F14, market.py): personal stalls on the flea market.
        # F14.1: a stall is closed (escrow back in the bag, S2C 0x84 {1}) before any map-load
        # lead, or the late 0x84 duplicates its listed items in the client bag. Its leave /
        # disconnect hooks are registered BEFORE presence's, so the 0x86 sign removal reaches
        # the peers while their client still holds the seller (0x06 comes after).
        self.market = marketmod.Market(self)
        self.world.hooks.register(worldmod.BEFORE_SERVER_MAP_LOAD, self._hook_stall_close)
        marketmod.register(self.world.hooks, self.market)
        # P5 stage 2 (world-presence, world-move-relay, world-position-estimate): the other
        # players of the map - 0x04 to the entrant and 0x05 to its peers after the own 0x07
        # (registered after the welcome / bank block, so the live-verified 2009 entry prefix
        # 0x03 0x07 0x15 0x65 stays as it is), 0x06 to the old map's peers on a map change
        # and on leaving the world. presence.py keeps what each client holds.
        presence.register(self.world.hooks)
        # P6 stage 2 (social_friend F1-F12, messenger.py): friends, presence, chat rooms,
        # mentors, memos. Its hooks only push to OTHER players (watchers, room members, the
        # mentor), so every map load's own sequence is unchanged: before a server map load
        # the room is left and a Busy/AFK status reset (the 0x03 wipes both client-side),
        # the first entry announces the character (0x60 / 0x7B / 0x7D), leaving the world
        # or closing announces it gone.
        self.messenger = msgrmod.Messenger(self)
        msgrmod.register(self.world.hooks, self.messenger)
        # P6 stage 3 (party.md F1-F10, party.py): parties. Its hooks only flag a member's
        # map load for a vitals resync (the frames survive 0x08/0x03: live party#14, and the
        # 2009 0x08 closes the same windows) and take a member that leaves the world or
        # closes its connection out of the party (0x51 to the others), so every map load's
        # own sequence is unchanged.
        self.party = partymod.Parties(self)
        partymod.register(self.world.hooks, self.party)
        # P6 stage 4 (cs-party-skills): a leave ends the auras each side no longer gets
        # (S2C 0x43), a join covers both sides at once (party-skill-hooks listeners).
        self.party.leave_listeners.append(
            lambda server, s, p, reason, others=(): self._auras_after_leave(s, p, reason, others))
        self.party.join_listeners.append(lambda server, s, p: self._auras_after_join(s, p))
        # P7 stage 1 (trade.md 1-3, trade.py): player-to-player trade with escrow and an
        # atomic commit. Its hooks cancel a session's trade (0x49 to both) before a server
        # map load's lead - portal, warp, village transfer, revive, a repeated 0x2B - and
        # when it leaves the world or its connection closes; a death cancels it too
        # (_player_death). The offered goods stay in the records until the commit.
        self.trade = trademod.Trades(self)
        trademod.register(self.world.hooks, self.trade)
        # P7 stage 3 (social_friend F10 / F11, chat_mail_gm F9; reputation.py): compliments
        # (C2S 0x6D -> 0x94 / 0x96 / 0x97, account manner +1) and player reports (C2S 0x6E ->
        # fee, reports.jsonl, 0x95, a GM alert). No hooks: nothing is held across map loads.
        self.reputation = repmod.Reputation(self)

        # F4 (lc-data-model): the store owns accounts.json (load, one-shot migration,
        # debounced atomic saves). db_file is a parameter so offline tests run against a
        # temp copy and never touch the live accounts.json.
        self.store = storemod.Store.from_config(self.config, db_file)
        self.store.load()
        self.store.attach(self.ticks, autosave=False)     # autosave starts with the server
        # P8 stage 1 (premium_cash-wallet-model / -cash-inventory-api, cash.py): the account
        # wallets (Wind Cash, Mileage), the mall box and every character's owned cash list
        # live in the store; this is the API over them (serials, find, consume, grant). The
        # owned list goes to the client after every map load (_send_owned_cash).
        self.cash = cashmod.CashInventory(self.store)
        # P8 stage 2 (premium_cash-mall-enter .. -gift, chat_mail_gm-gift-inbox; mall.py): the
        # Item Mall / Spark Shop on top of that model - `!mall` in, C2S 0x42 out (the map-load
        # replay), 0x43 buy, 0x45 delete, 0x46 balance, 0x47 gift, and the 0x6D gift queue each
        # map load sends (_map_transfer).
        self.mall = mallmod.Mall(self)
        # P8 stage 3 (premium_cash-use-generic .. -friend-warp, -expiry, lc-rename; cashuse.py):
        # using what the owned list holds - C2S 0x48 generic use / hair / period items, 0x49
        # rename, 0x4A stat reset, 0x4C megaphone, 0x70 / 0x71 warp stones - and the period
        # expiry (S2C 0x93 ticker, the map load's sync). A rename is pushed to the groups that
        # show names through the world hook ON_RENAME (ROADMAP_2009_ADDENDUM C4; the messenger and
        # the party registered theirs above: friends' 0x0B, the mentor's 0x7B, party frames).
        self.cashuse = cashusemod.CashUse(self)
        # ROADMAP_2009_ADDENDUM C5 / C6 (pets.py): the pet stub P15 replaces - the C2S 0x48
        # gates / effects of EN pet food 4286..4289 and the name ticket 4322 (the unpatched exe
        # sends them there) and the 2009 C2S 0x4D PetRename refusal (S2C 0x73 {0}).
        self.pets = petsmod.PetStub(self).install(self.cashuse)
        # P13 stage 1 (events.py: ev-e1..ev-e4, arch09-window-open): the event schedule
        # (config EVENTS_FILE), the multiplier stage of award_exp, the login gift and the
        # Event News popup. It registers one hook (before_server_map_load: no event packet
        # until that map load's C2S 0x63) and its '!event' / '!expmult' commands.
        self.events = eventmod.Events(self)
        # P13 stage 3 (bosses.py: boss-b1, boss-b2): the field-boss ledger keyed (channel, map,
        # tile), persisted next to accounts.json (BOSS_LEDGER_FILE), which the monster lifecycle
        # below consults for a boss's respawn; its '!boss' command.
        self.bosses = bossmod.Bosses(self)

    @property
    def routes(self):
        """The C2S route table of this server's build (client-2009-world): ROUTES_2009 for the
        2009 client, else ROUTES - read at dispatch time, so a test that swaps self.ROUTES on
        one server still reaches the dispatcher."""
        return self.ROUTES_2009 if self.client_build == cfgmod.BUILD_2009 else self.ROUTES

    @property
    def db_file(self):
        return self.store.path

    @property
    def accounts(self):
        """The store's account dict (read-modify-write under self.store.lock)."""
        return self.store.accounts

    def _save_store_now(self, what):
        """The immediate save of a create / delete / AUTO_REGISTER, made once the handler let
        store.lock go: the write and its ~3 s replace backoff never hold db_lock (review of
        livetest bug 7). A failed write is logged and left to the store - the record is
        already in memory, the store stays dirty and retries with backoff - so the reply
        still goes out. Never call it holding store.lock."""
        try:
            self.store.save_now()
        except OSError:
            log.exception(f'[STORE] {what}: the immediate accounts.json save failed; kept dirty, '
                          f'the store retries')

    def channel_user_counts(self):
        """{channel_no: logged-in accounts} for the S2C 0x01 channel table
        (lc-version-config). One channel today, so every online account is on it; the
        per-channel split lands with the channel model."""
        online = len(self.world.by_uid)
        return {no: online for no, _name, _ip in self.config.channels()}

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(10)
        log.info(f'[GAME] Listening on {self.host}:{self.port}')
        self.store.attach(self.ticks, autosave=True)
        # world-persistence: mirror live map/position/HP into the records periodically, so a
        # hard kill loses at most WORLD_SAVE_SECS of world state (F8 "save debounce / 60 s").
        self.ticks.call_every(self.config.WORLD_SAVE_SECS, self._tick_world_state,
                              name='world-state-save')
        # cs-regen: HP/MP regeneration, which the field client never runs itself (F8 table).
        self.ticks.call_every(self.REGEN_TICK_SECS, self._tick_regen, name='regen')
        # cs-buffs: the client never expires a buff (spec correction C4), so every slot ends
        # with the server's S2C 0x43/0x3C; 50 ms is the F8 table's buff resolution.
        self.ticks.call_every(self.BUFF_TICK_SECS, self._tick_buffs, name='buff-expiry')
        # cs-debuffs: DoT ticks (990 ms), detonations and expiry of the slots on monsters -
        # host-only on the client (FUN_00417e10), so the server runs them for the field.
        self.ticks.call_every(self.BUFF_TICK_SECS, self._tick_debuffs, name='debuff-ticks')
        # monster aggro: the chase decisions of the monsters that turned on a player - the
        # host-only half of the client's monster engine, run by the server as retail did.
        if self._mob_ai_enabled():
            self.ticks.call_every(self.config.MOB_AI_TICK_SECS, self._tick_monster_ai, name='monster-ai')
        # world-shared-monsters: a map nobody stood on for MOB_MAP_KEEP_SECS loses its monsters.
        self.ticks.call_every(self.MOB_MAP_GC_SECS, self._tick_monster_maps, name='monster-maps')
        # desync fix P2: the settle node of every mover gone quiet on idle words (presence.settle_node).
        if self.config.get('RELAY_SETTLE_NODE', True):
            self.ticks.call_every(self.PRESENCE_SETTLE_TICK_SECS, self._tick_presence_settle,
                                  name='presence-settle')
        # party-vitals-sync (party.md F7): the frames' 0x54 / 0x55, coalesced per 250 ms.
        self.ticks.call_every(partymod.VITALS_SECS, self.party.tick_vitals, name='party-vitals')
        # premium_cash-expiry (F15; roadmap F8 ticker table "60 s"): activated period cash items
        # whose date passed -> S2C 0x93 "[x] is expired." to their in-world owner.
        self.ticks.call_every(cashusemod.EXPIRY_SCAN_SECS, self.cashuse.tick_expiry, name='cash-expiry')
        # P13 (events.py): the every-N-minutes [Announcement] lines and events that start
        # while players are in the world.
        self.ticks.call_every(eventmod.TICK_SECS, self.events.tick, name='events')
        self.ticks.start()
        if self.memory_driver_enabled():
            threading.Thread(target=self._combat_driver, daemon=True).start()
        else:
            log.info('[COMBAT] DEV_MEMORY_COMBAT off: no memory combat driver')
        threading.Thread(target=self._admin_listener, daemon=True).start()

        while True:
            try:
                client, addr = srv.accept()
                log.info(f'[GAME] Connection from {addr}')
                threading.Thread(target=self._handle, args=(client, addr), daemon=True).start()
            except Exception as e:
                log.error(f'[GAME] Accept error: {e}')

    def memory_driver_enabled(self):
        """DEV_MEMORY_COMBAT, for either build (client-2009-tooling). The driver reads the
        addresses of the server's CLIENT_BUILD (self.client_layout: 2008 scene 0x70EECC /
        uid +0x84 / pos +0x11F8,+0x1288; 2009 scene 0x54F0C0 / uid +0x88 / pos
        +0x1298,+0x1328, client_map_2009.json) and attaches only to a WindSlayer_patched.exe
        whose PE timestamp is that build's (_find_client_pid): both builds' exes share the
        name, and the other build's layout would feed garbage positions into the world saves.
        DRIVER_MELEE (swing damage) stays a separate switch, off by default."""
        return bool(self.config.DEV_MEMORY_COMBAT)

    # Admin injection session filters (roadmap F9 / S1-15). Default in_world: the
    # SubHandler3 messenger packets deref CMessenger+0x74, which only the first 0x03
    # sets, so injecting into a character-select session crashes that client.
    ADMIN_STATES = ('in_world', 'select', 'all')

    def _admin_command(self, line):
        """Handle one admin JSON line; returns the reply text.

        {"opcode": N, "payload_hex": "...",
         "target": <uid int> | "<account or character name>",   (optional)
         "state": "in_world" (default) | "select" | "all"}        (optional)

        state in_world = sessions whose own 0x07 was sent (session['in_world']);
        select = logged in but not in world (character select: 0x02/0x1C/0x1F/0x8C tests);
        all = every connected session (the pre-P0 behaviour). A numeric target is the
        account uid (lc-uid-online: test = 1, admin = 2). "c:2" / "client:2" is the client
        exe (party-test-harness): the session whose C2S 0x2B named the P2P port
        42907 + (N - 1) - 42907 WindSlayer_patched.exe, 42908 WindSlayer_p2.exe - whatever
        account it logged in with (world.client_number). Any other string is an account or
        character name (case-insensitive); a decimal STRING is a uid only when no connected
        session has that name (names.ALLOWED_RE allows a character called '12'). The colon
        keeps "c:2" apart from a character called "c2".

        {"gm": "<character name>", "level": 1}  sets that character's GM flag instead
        (chat_mail_gm-gm-flag-manner; see _admin_gm).
        {"dev": "<character name>", "cmd": "!level 3"}  runs a '!' dev command as that
        in-world character (_admin_dev; `wsdev dev`).
        {"kick": <target as above>, "reason": N}  kicks that session: S2C 0x5D, save, socket
        closed after 1 s (_admin_kick; quest_cards_misc-admin-kick-maintenance).
        {"shutdown": 1}  the 3-minute maintenance shutdown (/stop's path); {"maintenance": 0}
        lifts the login lock (and cancels a countdown), {"maintenance": 1} locks logins only."""
        cmd = json.loads(line)
        if 'gm' in cmd:
            return self._admin_gm(cmd)
        if 'dev' in cmd:
            return self._admin_dev(cmd)
        if 'kick' in cmd:
            return self._admin_kick(cmd)
        if 'shutdown' in cmd or 'maintenance' in cmd:
            return self._admin_maintenance(cmd)
        opcode = int(cmd['opcode'])
        payload = bytes.fromhex(cmd.get('payload_hex', ''))
        state = cmd.get('state') or 'in_world'
        if state not in self.ADMIN_STATES:
            raise ValueError(f'state must be one of {self.ADMIN_STATES}, not {state!r}')
        target = cmd.get('target')
        chosen, skipped = [], []
        live = [s for s in list(self.sessions.values()) if s.get('sock') and not s.get('kicked')]
        name_hit = isinstance(target, str) and any(self._admin_name_matches(s, target) for s in live)
        for s in live:
            if target is not None and not self._admin_target_matches(s, target, name_hit):
                continue
            if self._admin_state_matches(s, state):
                chosen.append(s)
            else:
                skipped.append(s)
        for s in chosen:
            # flush=True: the admin thread holds no game lock, so it writes on its own
            # thread and the reply below means "on the wire" (world.Outbox).
            self._send_encrypted(s['sock'], s, opcode, payload, use_by_array=True, flush=True)
        who = ', '.join(self._admin_label(s) for s in chosen)
        log.info(f'[ADMIN] injected 0x{opcode:02X} ({len(payload)}B) -> {len(chosen)} session(s) '
                 f'[{who}] state={state} target={target!r} skipped={len(skipped)}')
        reply = f'ok 0x{opcode:02X} {len(payload)}B sessions={len(chosen)}'
        if skipped:
            reply += (f' skipped={len(skipped)} not {state}: '
                      + ', '.join(self._admin_label(s) for s in skipped))
        if not chosen and not skipped:
            reply += ' (no connected session' + (f' matches target {target!r})' if target is not None else ')')
        return reply

    def _admin_gm(self, cmd):
        """{"gm": "<character>", "level": 0|1} from the localhost admin port.

        F10.0 step 1 makes the first GM by editing accounts.json, but the store owns that
        file while the server runs (the next debounced save overwrites a hand edit), so the
        same change goes through the store here - that is how the lead flags the first GM
        without stopping the server. Afterwards a GM can use `!gm <name> 0|1` in chat.
        The client reads gm_level out of the S2C 0x07 record, so the target's own client
        only offers /manner, /not and the rest after its next map load or relog; the
        server-side '!' commands work as soon as the session is flagged."""
        name = str(cmd['gm'])
        level = int(cmd.get('level', gm.GM_LEVEL_ON))
        found = self.store.set_gm(name, level)
        if found is None:
            return f'error: no character named {name!r}'
        username, char = found
        session = self._target_session(char['name'])
        if session is not None:
            session['gm'] = level
        log.info(f'[ADMIN] gm {char["name"]} ({username}) = {level}'
                 + (' (live session flagged)' if session is not None else ''))
        return (f'ok gm {char["name"]} ({username}) = {level}'
                + ('; live session flagged, client needs a map load or relog for its own commands'
                   if session is not None else '; offline, applies at the next enter world'))

    def _admin_dev(self, cmd):
        """{"dev": "<character>", "cmd": "!level 3"} from the localhost admin port: run one
        '!' dev command (DEV_COMMANDS) as that in-world character, exactly as if its GM had
        typed it - the answer is the usual S2C 0x15 line on that client, the action is
        audited under its account. Live checks need it because typing into the 2009 chat
        box is unreliable (LIVE_TEST_LOG 2026-09-23 19:18 "TODO harness": stray keys went to
        game hotkeys); e.g. P5 exit criterion 7, `wsdev dev TestHero !level 3`. The admin
        port is the operator's (localhost only, it already injects packets and sets GM
        flags), so the character's own GM flag is not required."""
        name = str(cmd['dev'])
        text = str(cmd.get('cmd') or '').strip()
        if not text:
            return 'error: no command'
        if not text.startswith('!'):
            text = '!' + text
        session = self._target_session(name)
        if session is None or not worldmod.reachable(session):
            return f'error: {name!r} is not in world'
        log.info(f'[ADMIN] dev {session.get("char_name")!r}: {text}')
        self._gm_chat_command(session['sock'], session, text.encode('cp949', 'replace'))
        return f'ok dev {session.get("char_name")}: {text}'

    def _admin_kick(self, cmd):
        """{"kick": target, "reason": N} (quest_cards_misc-admin-kick-maintenance): the
        operator's kick of one connected session - S2C 0x5D {reason} (default: the build's
        kick text, gm.kick_reason), its world state saved at once, the socket closed after
        ADMIN_KICK_CLOSE_SECS. The target is chosen like an injection target (uid, "c:N",
        account or character name)."""
        target = cmd.get('kick')
        if target is None or target == '':
            return 'error: kick needs a target (uid, "c:N", account or character name)'
        live = [s for s in list(self.sessions.values())
                if s.get('sock') and not s.get('kicked') and s.get('username')]
        name_hit = isinstance(target, str) and any(self._admin_name_matches(s, target) for s in live)
        chosen = [s for s in live if self._admin_target_matches(s, target, name_hit)]
        if not chosen:
            return f'error: no connected session matches {target!r}'
        reason = cmd.get('reason')
        done = []
        for s in chosen:
            sent = self._disconnect_with_notice(s, f'admin kick ({target!r})', reason=reason,
                                                close_after=gm.ADMIN_KICK_CLOSE_SECS, save=True)
            done.append(f'{self._admin_label(s)} reason {sent}')
        gm.audit(self.gm_audit_path, 'admin', 'kick', f'{target!r}: {", ".join(done)}')
        return f'ok kick {", ".join(done)}; closing in {gm.ADMIN_KICK_CLOSE_SECS:g} s'

    def _admin_maintenance(self, cmd):
        """{"shutdown": 1} / {"maintenance": 0|1} from the admin port (see _admin_command)."""
        if cmd.get('shutdown'):
            started = self._start_maintenance('admin shutdown')
            gm.audit(self.gm_audit_path, 'admin', 'shutdown', 'started' if started else 'already running')
            return ('ok shutdown: 0x3A sent, logins locked, save at '
                    f'+{gm.MAINTENANCE_SAVE_SECS:g} s, close at +{gm.MAINTENANCE_CLOSE_SECS:g} s'
                    if started else 'ok shutdown already running')
        if 'maintenance' in cmd:
            if cmd['maintenance']:
                self.config['MAINTENANCE'] = True
                log.warning('[ADMIN] MAINTENANCE login lock on')
            else:
                self._lift_maintenance()
            gm.audit(self.gm_audit_path, 'admin', 'maintenance', self.maintenance_state())
        return f'ok maintenance {self.maintenance_state()}'

    @staticmethod
    def _admin_label(session):
        where = 'world' if session.get('in_world') else ('select' if session.get('username') else 'login')
        return f"{session.get('username') or '?'}/{session.get('char_name') or '-'}@{where}"

    @staticmethod
    def _admin_name_matches(session, target):
        """The session's account or character name is `target` (case-insensitive)."""
        name = str(target).strip().lower()
        return bool(name) and name in (str(session.get('username') or '').lower(),
                                       str(session.get('char_name') or '').lower())

    @staticmethod
    def _admin_target_matches(session, target, name_hit=False):
        """Does admin `target` select `session`? An int is the account uid; "c:N" /
        "client:N" the client exe; a string an account or character name. A decimal string
        falls back to the uid only when `name_hit` is False, i.e. no connected session is
        called that (the caller checks all of them, see _admin_command)."""
        if isinstance(target, bool):
            return False
        if isinstance(target, int):
            return P.session_uid(session) == target
        client = worldmod.parse_client_target(target)
        if client is not None:
            return worldmod.client_number(session) == client
        if GameServer._admin_name_matches(session, target):
            return True
        text = str(target).strip()
        return not name_hit and text.isdigit() and P.session_uid(session) == int(text)

    @staticmethod
    def _admin_state_matches(session, state):
        if state == 'all':
            return True
        if state == 'select':
            return bool(session.get('username')) and not session.get('in_world')
        return bool(session.get('in_world'))

    def _admin_listener(self):
        """Localhost-only debug channel used by `wsdev.py send`. Each line received
        is a JSON command (see _admin_command); the packet is sent to the matching
        sessions exactly as a normal server->client packet. This is how individual
        S2C opcodes from the RE spec get tested against the real client."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port = self.config.ADMIN_PORT
        try:
            srv.bind(('127.0.0.1', port))
        except OSError as e:
            log.warning(f'[ADMIN] cannot bind 127.0.0.1:{port}: {e}')
            return
        srv.listen(4)
        log.info(f'[ADMIN] debug packet injection on 127.0.0.1:{port}')
        while True:
            conn, _ = srv.accept()
            with conn:
                buf = b''
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                replies = []
                for line in buf.decode('utf-8', 'replace').splitlines():
                    if not line.strip():
                        continue
                    try:
                        replies.append(self._admin_command(line))
                    except Exception as e:
                        replies.append(f'error: {e}')
                try:
                    conn.sendall(('\n'.join(replies) + '\n').encode())
                except OSError:
                    pass

    def _handle(self, sock, addr):
        """
        Determine if this is HTTP or Fireway by checking first byte quickly.
        HTTP clients send first. Fireway clients wait for server.
        We send Fireway key exchange immediately - HTTP would error here.
        """
        try:
            self._handle_fireway(sock, addr)
        except Exception as e:
            log.error(f'[GAME] Error for {addr}: {e}')
            traceback.print_exc()
            sock.close()

    # Idle reaping (quest_cards_misc-keepalive-reaper, quest doc F13). The client sends a
    # 0x05 keepalive every 240 s (live-measured to 240.0 s, quest_cards_misc#00) plus a 0x0D
    # every 15 s while it is in world, so 300 s of complete silence means the connection is
    # dead: the socket is still open but nothing is arriving, and without this the session,
    # its monsters and its unsaved state stayed forever (Q-B17, the recv timeout just
    # continued). Nothing is sent to the reaped client - it is already gone or silent.
    RECV_TIMEOUT_SECS = 120.0
    IDLE_TIMEOUT_SECS = 300.0

    def _handle_fireway(self, sock, addr):
        """Handle a Fireway game connection."""
        close_reason = 'connection closed'      # why the finally below ends the session
        try:
            # Step 1: Send key exchange (opcode 0x5A, seq=1) using EncodebyArray
            # The key exchange is sent BEFORE SetCodeKey, so it uses a static-table
            # based encoding (EncodebyArray) instead of the MT-based one.
            seed = int(time.time() * 1000) & 0x7FFFFFFF
            log.info(f'[FIREWAY] {addr}: seed=0x{seed:08X}')

            body = struct.pack('<B', 0x5A) + struct.pack('<i', seed)
            # Packet MUST have NoEncode bit set AND be encoded with EncodebyArray.
            # GetHeader (Fireway.dll VA 0x10003810) checks the NoEncode bit: if set
            # it calls DecodebyArray (static table XOR). So the server must:
            #   1. Set NoEncode bit (0x800) in DWORD[0]
            #   2. Encode body+checksum with EncodebyArray
            pkt = make_raw_packet(body, seq=1, no_encode=True)
            enc_init = CEncMsg()
            enc_init.encode_by_array(pkt)
            log.info(f'[FIREWAY] Sending key exchange ({len(pkt)}B, NoEncode+EncodebyArray):\n{hexdump(pkt)}')
            sock.sendall(bytes(pkt))

            # Step 2: Initialize encryption
            enc = CEncMsg()
            enc.set_code_key(seed)

            session = {
                'addr': addr,
                'seed': seed,
                'enc': enc,
                'send_seq': 2,
                'username': None,
                # Account uid (F3, lc-uid-online): set by World.claim at login from the store;
                # the client's scene+0x220 and every local-player record carry it.
                'uid': None,
                'sock': sock,          # so the combat driver thread can reach this client
                # True after our own 0x07 spawn, False while a server map load is in
                # flight (roadmap F5). The admin injector only targets in-world sessions.
                'in_world': False,
                # Created here, not by setdefault on first use: the connection thread, the
                # combat driver and the tick thread all send to and mutate this session
                # (roadmap F5, S1-08; cs-combat-lock guards monsters and hp/mp).
                'send_lock': threading.Lock(),
                'combat_lock': threading.RLock(),
                # The client's map clock (scene+0xF1C): seeded by the first 0x03/0x08 this
                # session sends and followed from every C2S 0x0D (F5 `clock`).
                'clock': GameServer.START_CLOCK_MS,
                # monotonic() of the last accepted MapTransfer; the portal cooldown guard
                # (world-portal-guards) measures against it.
                'last_transfer_t': 0.0,
                # monotonic() of the last C2S packet, for the idle reaper (F13). Seeded
                # here so a connection that never sends anything is reaped too.
                'last_rx': time.monotonic(),
                # world-registry outbound path (F5): every S2C to this client is encoded
                # under send_lock and queued here; this connection thread writes its own
                # replies at once, any other thread only queues and wakes the writer, so
                # it never blocks on this socket (world.Outbox, _send_encrypted).
                'outbox': worldmod.Outbox(sock, label=f'{addr[0]}:{addr[1]}'),
                'conn_thread': threading.get_ident(),
                # world-presence: the players this client has spawned ({uid: session}) and the
                # lock every sender takes to change it together with the 0x04/0x05/0x06/0x1B
                # that changes the client (presence.py "What each client has spawned").
                'presence_lock': threading.RLock(),
                'spawned_players': {},
            }
            self.sessions[addr] = session

            # Step 3: Read encrypted client packets - also log any raw bytes received
            sock.settimeout(self.RECV_TIMEOUT_SECS)
            # world-presence: a connection that dies silently is found within DEAD_PEER_SECS
            # (TCP keepalive), so its peers see it despawn in ~2 s, not after the recv timeout.
            presence.set_dead_peer_timeout(sock, self.config.DEAD_PEER_SECS)
            sock_buffer = bytearray()
            while not session.get('kicked'):
                try:
                    chunk = sock.recv(4096)
                except socket.timeout as e:
                    if getattr(e, 'errno', None):
                        # WSAETIMEDOUT / ETIMEDOUT from the TCP stack, not the recv timeout
                        # (that one has no errno): the dead-peer keepalive or TCP_MAXRT gave
                        # up on a silently dropped client (presence.set_dead_peer_timeout).
                        log.info(f'[FIREWAY] {addr} connection timed out ({e}): dropped peer')
                        close_reason = 'connection timed out'
                        break
                    # Keep-alive: don't drop an idle in-world client (a 120 s drop closed
                    # the client mid-debug), but do reap a silent one: the 0x05 heartbeat is
                    # every 240 s, so nothing at all for IDLE_TIMEOUT_SECS is a dead socket.
                    # The finally branch below saves the character and drops the session.
                    idle = time.monotonic() - session.get('last_rx', 0.0)
                    if idle > self.IDLE_TIMEOUT_SECS:
                        log.info(f'[FIREWAY] {addr} reaped: {idle:.0f}s with no C2S packet '
                                 f'(keepalive is every 240s)')
                        close_reason = 'idle reap'
                        break
                    continue
                except Exception as e:
                    log.info(f'[FIREWAY] recv error: {e}')
                    close_reason = 'recv error'
                    break

                if not chunk:
                    close_reason = 'client closed'
                    log.info(f'[FIREWAY] {addr} closed connection (after {len(sock_buffer)}B recv)')
                    if sock_buffer:
                        log.info(f'[FIREWAY] Bytes before close:\n{hexdump(bytes(sock_buffer))}')
                    break

                sock_buffer.extend(chunk)
                log.info(f'[FIREWAY] Got {len(chunk)}B from client (total {len(sock_buffer)}B):\n{hexdump(bytes(chunk))}')

                # Try to parse any complete packets (none once a duplicate login closed this
                # session: its buffered requests must not act on the account any more)
                while len(sock_buffer) >= HEADER_SIZE and not session.get('kicked'):
                    size_dword = struct.unpack_from('<I', sock_buffer, 0)[0]
                    pkt_size = size_dword & SIZE_MASK
                    if pkt_size < HEADER_SIZE or pkt_size > MAX_PKT:
                        log.warning(f'[FIREWAY] Invalid size in header: {pkt_size}')
                        sock_buffer.clear()
                        break
                    if len(sock_buffer) < pkt_size:
                        break  # need more data

                    raw = bytearray(sock_buffer[:pkt_size])
                    del sock_buffer[:pkt_size]

                    no_enc = bool(size_dword & NO_ENCODE_FLAG)
                    if no_enc:
                        # NoEncode bit set → use DecodebyArray (static table XOR)
                        valid = enc.decode_by_array(raw)
                        if not valid:
                            log.warning(f'[FIREWAY] DecodebyArray checksum FAILED from {addr}')
                        log.info(f'[FIREWAY] DecodebyArray result:\n{hexdump(bytes(raw))}')
                    else:
                        # No NoEncode bit → use MT-based Decode
                        valid = enc.decode(raw)
                        if not valid:
                            log.warning(f'[FIREWAY] Decode checksum FAILED from {addr}')
                        log.info(f'[FIREWAY] Decode result:\n{hexdump(bytes(raw))}')

                    ps, seq, _, opcode, payload = parse_packet(raw)
                    # Any C2S packet is proof of life for the idle reaper (F13 step 2), so
                    # the 0x05 keepalive is what holds an idle client's session open without
                    # ever being answered (NEVER_REPLY).
                    session['last_rx'] = time.monotonic()
                    # Remembered per session so packets.send() can encode pushes to this
                    # client in the mode it last used (roadmap F1/F5: target session's no_enc).
                    session['no_enc'] = no_enc
                    log.info(f'[FIREWAY] Pkt: opcode=0x{opcode:02X} size={ps} seq={seq} no_enc={no_enc} payload={len(payload)}B')
                    self._dispatch(sock, session, opcode, payload, no_enc)

        except Exception as e:
            log.error(f'[FIREWAY] Error for {addr}: {e}')
            traceback.print_exc()
            close_reason = f'error: {e}'
        finally:
            gone = self.sessions.pop(addr, None)
            try:
                if gone is not None:
                    self._session_closed(gone, gone.get('kicked') or close_reason)
            finally:
                sock.close()                    # even when a disconnect step raised

    def _session_closed(self, session, reason):
        """The F5 disconnect path, on the connection's own thread (its finally), exactly once.

        In order: on_leave_world when the session was on a map (its peers are told there,
        world-presence), the identity indexes (identity-checked, so a session closed by a
        duplicate login or a 2009 relogin never removes its replacement), on_disconnect (the
        groups' party / trade / friend cleanup), then the save and the timers. The outbox
        closes last: the hooks may still push to OTHER sessions, never to this one.

        Every step runs even when an earlier one raised (logged): a failed save must not
        leave the account in the online indexes or the map timers armed, and the outbox
        closes in the finally, so its writer thread never waits for ever."""
        if session.get('closed'):
            return
        user = session.get('username')

        def save():
            # world-persistence: the live map, position and HP/MP go into the record
            # before the save, so a relog puts the character back where it logged
            # out with the HP it left with (P2 exit criterion 2).
            self._save_world_state(session, reason='disconnect')
            self.store.mark_dirty(f'disconnect {user}')

        steps = (
            ('leave world', lambda: self._leave_world(session, reason)),
            ('drop indexes', lambda: self.world.drop(session)),
            ('mark closed', lambda: session.__setitem__('closed', reason)),
            ('on_disconnect', lambda: self.world.hooks.fire(worldmod.ON_DISCONNECT, self, session,
                                                            reason=reason)),
            ('save', save if user else None),
            # Off its map's shared monsters (world-shared-monsters): their timers stay with
            # the map and never send to this closed socket (cs-monster-death).
            ('clear map monsters', lambda: self._clear_map_monsters(session)),
        )
        try:
            for label, step in steps:
                if step is None:
                    continue
                try:
                    step()
                except Exception:
                    log.exception(f'[WORLD] disconnect of {user!r}: {label} failed; continuing')
        finally:
            session['closed'] = session.get('closed') or reason
            outbox = session.get('outbox')
            if outbox is not None:
                outbox.close()
        if user:
            log.info(f'[WORLD] {session["username"]}/{session.get("char_name") or "-"} '
                     f'uid={session.get("uid")} disconnected ({reason}); in world now: '
                     f'{self.world.describe()}')

    def _leave_world(self, session, reason):
        """Take the session off its map for good (disconnect, kick, idle reap, character
        deleted) and fire on_leave_world once, with the map it was on. A map CHANGE is not
        this: MapTransfer departs and fires on_map_change instead. Returns the map code, or
        None when the session was on no map."""
        session['in_world'] = False
        left = self.world.depart(session)
        if left is not None:
            self.world.hooks.fire(worldmod.ON_LEAVE_WORLD, self, session, map_code=left,
                                  reason=reason, superseded=self.world.superseded(session))
        return left

    def _send_encrypted(self, sock, session, opcode, payload=b'', use_by_array=False, flush=None):
        """Send a packet. Use EncodebyArray if use_by_array=True (for pre-login packets).

        Outbound path (roadmap F5, world-registry / party-mp-registry `_send_to` /
        trade-mp-registry): the packet is encoded under the RECEIVER's send_lock and appended
        to its world.Outbox, so the cipher order (send_seq, the MT stream) is the queue
        order. The receiver's own connection thread then writes it at once, so a reply is
        on the wire before its handler returns. Any other thread - another player's handler,
        the tick scheduler, the combat driver, a lifecycle hook - only queues it and wakes
        the receiver's writer thread: it never blocks on a socket it does not own while
        holding its own session's combat lock or the world lock (a stalled peer cannot
        freeze the sender). flush=True writes on the calling thread anyway (the admin
        injector, which holds no lock). A session without an outbox (a bare dict in unit
        tests) or a socket that is not the session's is written directly, as before."""
        build = getattr(self, 'client_build', None)     # a bare GameServer.__new__ in tests: 2008
        why = P.forbidden_reason(opcode, build)
        if why:
            # packets.FORBIDDEN_S2C (spec_2009 0xC5): also refused for raw sends, so the admin
            # injector cannot drop a 2009 client either.
            raise P.PacketError(f'S2C 0x{opcode:02X} must never be sent to a {build} client: {why}')
        if HEADER_SIZE + 1 + len(payload) > MAX_PKT:
            # The header's size field is 11 bits (PROTOCOL.md framing): make_raw_packet would
            # mask a longer length and the client would read a corrupt stream from here on.
            # Refused before send_seq / the cipher advance, so the connection stays in step.
            log.error(f'[FIREWAY] S2C 0x{opcode:02X} refused: {len(payload)} B payload > '
                      f'{MAX_PKT - HEADER_SIZE - 1} B (one frame) -> {session.get("char_name")!r}')
            raise P.PacketError(f'S2C 0x{opcode:02X} payload of {len(payload)} B does not fit one '
                                f'Fireway frame ({MAX_PKT - HEADER_SIZE - 1} B max)')
        outbox = session.get('outbox')
        if outbox is not None and sock is not session.get('sock'):
            outbox = None
        with session.setdefault('send_lock', threading.Lock()):
            enc = session['enc']
            seq = session['send_seq']
            session['send_seq'] = seq + 1

            body = struct.pack('<B', opcode) + payload
            pkt = make_raw_packet(body, seq=seq, no_encode=use_by_array)
            if use_by_array:
                enc.encode_by_array(pkt)
            else:
                enc.encode(pkt)

            log.info(f'[FIREWAY] Send: opcode=0x{opcode:02X} seq={seq} size={len(pkt)} by_array={use_by_array}')
            log.debug(f'[FIREWAY] Encoded:\n{hexdump(pkt)}')
            if outbox is None:
                sock.sendall(bytes(pkt))
            else:
                outbox.put(pkt)                 # OSError once the connection is closed
            # Marks the C2S request being dispatched on this thread as answered, so the
            # registry knows whether a must-reply request still needs its refusal.
            registry.note_send(session, opcode)
        if outbox is not None:
            if flush or (flush is None and self._is_connection_thread(session)):
                outbox.flush()
            else:
                outbox.kick()

    @staticmethod
    def _is_connection_thread(session):
        """True on the thread that runs this session's receive loop (or for a session that
        has none: unit-test dicts), the only thread that writes to its socket itself."""
        owner = session.get('conn_thread')
        return owner is None or owner == threading.get_ident()

    # ========================================================================
    # C2S handler registry (roadmap 1.3 F2, arch-handler-registry).
    # opcode -> Route. Opcodes not listed are logged with hex + decoded fields.
    # The reply policy per opcode (MUST_REPLY refusals, NEVER_REPLY) lives in
    # registry.py and applies to every route, listed or not. A log-only route on a
    # MUST_REPLY opcode is an interim stub: the policy sends the minimal refusal.
    # ========================================================================
    ROUTES = {
        0x01: Route('_handle_login'),
        # Character select (lc-create / lc-delete). Both open the waiting modal, so both
        # are must-reply: 0x0E -> 0x1C, 0x12 -> 0x1F (B11, B12).
        0x0E: Route('_handle_create_character', style=STYLE_REC),
        0x12: Route('_handle_delete_character', style=STYLE_REC),
        # Password dialog 0x201 OK -> S2C 0x80 (shop_storage-password-gate + premium_cash-
        # password-gate): the allowlist 0x1A7 bank / 0x1F9 cash gift / 0x235 delete confirm.
        0x51: Route('_handle_password_verify', style=STYLE_REC),
        # Movement state on every step. Never echo it to the sender (cipher desync); the
        # peers that hold the mover get it as S2C 0x1B (world-move-relay, presence.relay).
        0x0D: Route('_handle_world_sync', quiet=True),
        0x2B: Route('_handle_enter_world', style=STYLE_REC),
        0x03: Route('_handle_chat', style=STYLE_REC),
        # Whisper /w (chat_mail_gm-whisper, F2): S2C 0x09 to the sender (echo 0x65, not found
        # 0x66, refused 0x67) + S2C 0x0A to the target. Not must-reply: the client opens no
        # box and prints nothing of its own whisper (spec 0x445ADD/0x02, 2009 0x4475C7/0x02).
        0x02: Route('_handle_whisper', style=STYLE_REC),
        # Friend chat /f (chat_mail_gm-friend-chat-relay, F3): S2C 0x91 to the stored friends
        # that are online; NEVER answered (the client printed its own line, registry).
        0x6B: Route('_handle_friend_chat', style=STYLE_REC),
        # GM commands (chat_mail_gm-gm-dispatch). Six send sites share this opcode and some
        # share a length, so the handler picks the grammar by payload[0] itself
        # (gm.SUBCOMMANDS) instead of letting parse() guess; hence STYLE_RAW. The client
        # waits for nothing after a GM command, so there is no must-reply entry.
        0x06: Route('_handle_gm'),
        0x04: Route('_handle_set_stats', style=STYLE_REC),
        # Legacy Yahoo ID transfer: unreachable while 0x02 carries transfer_status 3, but
        # answered if it arrives (lc-id-transfer-stub, F11).
        0x65: Route('_handle_id_transfer', style=STYLE_REC),
        0x66: Route('_handle_id_transfer', style=STYLE_REC),
        # shop_storage-npc-buy (F1): always answered - 0x18 {gold, victy, item, qty} or the
        # 0x18 {gold, victy, 0, 0} resync + 0x15; the policy sends the same refusal for a
        # payload that does not decode or a handler that raises.
        0x0B: Route('_handle_buy_item', style=STYLE_REC),
        # shop_storage-sell-parse: always answered (0x19 or the 0x18 resync + 0x15 refusal); a
        # payload that does not decode gets the same refusal from the policy.
        0x0C: Route('_handle_sell_item', style=STYLE_REC),
        # Consumables (0x25) and skill casts (cs-skill-cast: 0x25 / 0x3B to self / 0x5F). The
        # MUST_REPLY 0x5F stays the backstop that clears scene+0x258 if a cast handler raises.
        0x15: Route('_handle_use_item'),
        0x7E: Route('_handle_change_map'),
        # Garan Maria's village transfer (world-village-transfer, F5): 0x81 {1, gold} + the
        # MapTransfer, or 0x81 {0} (the MUST_REPLY refusal is the exception backstop).
        0x5D: Route('_handle_village_transfer', style=STYLE_REC),
        # client keepalive (~4 min). Consume, NO reply (a reply resets in-game state -> kick).
        0x05: Route(log='[0x05] client heartbeat - consumed', quiet=True),
        0x0F: Route('_handle_equip_item', style=STYLE_REC),
        # Unequip: an Equipment-window double-click or a drag into the bag (item_inventory-
        # unequip). It had no branch at all, so it logged "Unhandled opcode 0x11" and the
        # item stayed worn (B9). The client waits for nothing, so an invalid request is
        # dropped in silence rather than refused.
        0x11: Route('_handle_unequip_item', style=STYLE_REC),
        # Ground items (P4 stage 2, item_inventory.md F7-F9). Live item_inventory#07/#18/#19
        # logged all three as "Unhandled opcode". None opens a waiting box, so a refused
        # request gets no packet (0x13 / 0x14: a 0x15 warning line). 2009: the same grammars
        # at 0x4737D9/0x13, 0x4738B8/0x14, 0x43DA4E/0x1F and the pet auto-loot 0x42EA76/0x1F.
        # Pickup key W (C2S 0x1F u16 ground id) -> S2C 0x13 to the whole map.
        0x1F: Route('_handle_ground_pickup', style=STYLE_REC),
        # Bag item dragged to the ground (C2S 0x13) -> S2C 0x23 to self + 0x12.
        0x13: Route('_handle_drop_bag_item', style=STYLE_REC),
        # Worn item dragged from the Equipment window to the ground (C2S 0x14) -> 0x24 + 0x12.
        0x14: Route('_handle_drop_worn_item', style=STYLE_REC),
        # The quest loop (quest doc F1/F5/F6). 0x16 is ALSO the outbound chat opcode.
        # None of the three is must-reply: the client opens no modal and waits for nothing
        # (it opens the Finish dialog on 0x17 before any reply, live #03), so a refusal is
        # simply no quest packet.
        0x16: Route('_handle_accept_quest'),
        0x17: Route('_handle_turn_in_quest'),
        0x1E: Route('_handle_abandon_quest'),

        # --- sent after every S2C 0x03 map load: 0x2F then 0x63 (spec 0x44EF4F/0x2F, 0x44EF67/0x63) ---
        # MessengerFriendListRequest, fire-and-forget; it was logged as "arena query" (S2-03).
        # The client sends it after EVERY S2C 0x03 (whose messenger reset wiped its lists):
        # S2C 0x0B always, 0x7E online mentees, 0x78 memos (social_friend-friend-list-sync,
        # F1; messenger.py). Same bytes in 2009 (spec_2009 0x45353B/0x2F "identical").
        0x2F: Route('_handle_friend_list'),
        # CardDeckListRequest -> S2C 0x8A. It used to get an EMPTY S2C 0x63 (S2-01, D19).
        0x63: Route('_handle_card_deck_list'),
        # Card Deck register (P4 stage 4, quest_cards_misc-card-register, F10): the client
        # waits behind "Waiting for the server to response." until S2C 0x8B, so the handler
        # answers EVERY request; registry.MUST_REPLY 0x8B {3} stays the exception backstop.
        # 2009: the same bytes at 0x45FB50/0x64 (spec_2009 "identical").
        0x64: Route('_handle_card_register', style=STYLE_REC),

        # --- combat (cs-dispatch-fix). The EN basic attack sends no packet: swings come from
        # the memory-driven _combat_driver, never from a C2S opcode. ---
        # Death dialog (window 0x79, opened by S2C 0x3E) "Revived" button -> MapTransfer to
        # the revive point (cs-player-death, F8 steps 7-9). The policy's refusal re-opens the
        # dialog for a dead player if the revive fails.
        0x2E: Route('_handle_revive_request'),
        # Booby Trap fired; the client waits for nothing. The victim follows in the next C2S
        # 0x0D interact-0xC tail (cs-traps, F7; spec 0x417DB0/0x6C, C28).
        0x6C: Route('_handle_trap_triggered', style=STYLE_REC),

        # --- PvP rooms and battlefield (pvp-dispatch-cleanup, pvp-modal-guard) ---
        # T key: BattlefieldInfoRequest -> S2C 0x63. It used to run _handle_melee on the
        # nearest monster (S1-12).
        0x38: Route('_handle_battlefield_info'),
        0x2C: Route('_handle_room_query'),
        # List-window closes. Never answered (roadmap 1.3); 0x2D was labelled "scene-finalize ack".
        0x2D: Route(log='[0x2D] arena room list close - consumed'),
        0x75: Route(log='[0x75] play room list close - consumed'),
        # "Waiting for the server to response." modals: log-only stubs, refused by the policy
        # until the room handlers exist. 0x1A was consumed as "mob query" with no reply (S1-09).
        0x18: Route(log='[0x18] room create - refused, no rooms yet (pvp-room-create)'),
        0x1A: Route(log='[0x1A] room join from the arena/play room list - refused (pvp-room-join)'),
        0x1B: Route(log='[0x1B] room join by number - refused (pvp-room-join)'),
        0x1C: Route(log='[0x1C] room password join - refused (pvp-room-join)'),
        0x39: Route(log='[0x39] battlefield subscribe - refused (pvp-battlefield-queue)'),
        0x3A: Route(log='[0x3A] battlefield summon ready - refused (pvp-battlefield-queue)'),
        # Room-server broadcast code compiled into the client; unreachable (scene+0xF24 is
        # never set, scene+0xF98 is NULL). S2C 0x93/0x94/0x95/0x97/0x99 are unrelated messages.
        **{op: Route('_warn_dead_host_code') for op in (0x93, 0x94, 0x95, 0x97, 0x99)},

        # --- the player right-click popup, window 0x50 (P5 stage 4) ---
        # The popup itself is client-only (2008 FUN_0044C4B0 / 2009 FUN_00450130: a type-3
        # remote entity - every 0x04/0x05 record - with another uid, in field mode, clicked
        # within ~51 px of its anchor); what the server has to send for it is the presence
        # record. Its entries send these fire-and-forget requests, and none opens a waiting
        # box (spec 0x4484CC / 0x44A9D6 and 0x46FF17 / 0x47AE8B: the client prints its own
        # "You requested ..." line first). 0x20 Trade is the trade request since P7 stage 1
        # (below), 0x30 Add as Friend the messenger's own request since P6 stage 2, 0x27 /
        # 0x29 the party's since P6 stage 3; _handle_player_popup stays the logging consumer
        # for an entry whose owner item has not landed yet (POPUP_REQUESTS).
        # --- parties (P6 stage 3; party.md F1-F8, party.py). The same bytes in both builds
        # (spec_2009 0x44A9D6/0x27..0x29, 0x4476CB/0x6A "identical"). None opens a waiting
        # box and none is never-reply: the sender's own frames (0x51) and party chat line
        # (0x90, no local echo) come from the server. ---
        0x27: Route('_handle_party_invite', style=STYLE_REC),     # Make Party -> 0x4E / refusal
        0x28: Route('_handle_party_accept', style=STYLE_REC),     # window 0x71 Accept -> 0x4F
        0x29: Route('_handle_party_leave', style=STYLE_REC),      # Break Party -> 0x51
        0x6A: Route('_handle_party_chat', style=STYLE_REC),       # /p -> orange 0x90 to all
        # Char. Info of a player the client does not hold (lc-player-info, P6 stage 1; F9):
        # S2C 0x52 opens the Player Info window, 0x53 "<name>is not in server.". On the same
        # map the client fills window 0x72 from its own entity and sends nothing, so the
        # 0x04/0x05 record carries the same values (records.player_record / player_info).
        0x2A: Route('_handle_player_info', style=STYLE_REC),

        # --- trade (P7 stage 1; trade.md 2.1-2.9, trade.py). The same bytes in both builds
        # (spec_2009 0x44A9D6/0x20, 0x47394E/0x21, 0x473E14/0x22 + 0x476BF5/0x22, 0x473ABA/0x23,
        # 0x473B5B/0x24, 0x473E14/0x25, 0x476E62/0x26 "identical"). 0x24 / 0x25 are
        # MUST_REPLY: their handlers always answer (0x49 / 0x4A, or the first confirm's
        # deliberate wait, registry.defer_reply) and the refusal is the exception backstop.
        # 0x25 used to reach _handle_attack, which turned item ids into mob uids (S1-12). ---
        0x20: Route('_handle_trade_request', style=STYLE_REC),   # popup 0x50 Trade -> 0x45 / 0x47
        0x21: Route('_handle_trade_accept', style=STYLE_REC),    # window 0x70 OK -> 0x46 both
        0x22: Route('_handle_trade_add', style=STYLE_REC),       # item on 0x4E -> 0x4B both / 0x4D
        0x23: Route('_handle_trade_lock', style=STYLE_REC),      # OK + gold -> 0x48 both
        0x24: Route('_handle_trade_cancel', style=STYLE_REC),    # End / Cancel -> 0x49 both
        0x25: Route('_handle_trade_confirm', style=STYLE_REC),   # 0x291 Trade -> wait / 0x4A both
        0x26: Route('_handle_trade_remove', style=STYLE_REC),    # item back to the bag -> 0x4C

        # --- reputation (P7 stage 3; social_friend F10 / F11, chat_mail_gm F9; reputation.py).
        # The same bytes in both builds (spec_2009 0x4815C8/0x6D, 0x4819CD/0x6E "identical").
        # Neither opens a waiting box (window 0x435 stays open, 0x434 hides itself), so there
        # is no MUST_REPLY row; the handlers answer every case the client has a text for. ---
        0x6D: Route('_handle_compliment', style=STYLE_REC),      # popup Praise -> 0x94 (+ 0x96 / 0x97)
        0x6E: Route('_handle_report', style=STYLE_REC),          # Report window 0x434 -> 0x95 (+ GM 0x15)

        # --- the messenger (P6 stage 2; social_friend F2-F12, chat_mail_gm F6/F7; messenger.py).
        # Every request below has the same grammar in both builds (spec_2009 0x47AE8B/0x30 ..
        # 0x481A9F/0x73 "identical"). None opens a waiting box except the Note (0x4B), whose
        # handler always answers 0x77 (registry.MUST_REPLY stays the exception backstop). ---
        # Add Friend: window 0x179 (uid 0) and the player popup 0x50 (the entity's uid) ->
        # S2C 0x0D to the target or a 0x0C refusal.
        0x30: Route('_handle_friend_add', style=STYLE_REC),
        0x31: Route('_handle_friend_reply', style=STYLE_REC),      # prompt 0x17C -> 0x0C both
        0x32: Route('_handle_friend_delete', style=STYLE_REC),     # -> 0x0E (directed)
        # Messenger chat rooms: invite -> 0x10, reply -> 0x0F, line -> 0x61 to every member,
        # leave (window 0x177 hidden) -> 0x62 to the rest.
        0x33: Route('_handle_room_invite', style=STYLE_REC),
        0x34: Route('_handle_room_reply', style=STYLE_REC),
        0x35: Route('_handle_room_chat', style=STYLE_REC),
        0x36: Route('_handle_room_leave'),
        # Mentors: register (a Novice) -> 0x7A + 0x7B to the mentor; remove -> 0x7C.
        0x5C: Route('_handle_mentor_register', style=STYLE_REC),
        0x50: Route('_handle_mentor_remove'),
        # Frenaiga's friend slot expansion -> 0x9D {1, capacity, gold} / {0}.
        0x73: Route('_handle_friend_slot_expand'),
        # Notes: item 1894/3320 -> memo + 0x77; 9999 gift thank-you -> memo, no reply.
        0x4B: Route('_handle_note_send', style=STYLE_REC),

        # --- bank (shop_storage-bank-gold / -bank-items; shop_storage.md F4-F7). None of the
        # four opens a waiting box, so there is no must-reply row: every refusal is its own
        # resync (0x65 for items, 0x68/0x69 {0, ...} for gold). ---
        0x3C: Route('_handle_bank_deposit_item', style=STYLE_REC),
        0x3D: Route('_handle_bank_withdraw_item', style=STYLE_REC),
        0x3E: Route('_handle_bank_deposit_gold', style=STYLE_REC),
        0x3F: Route('_handle_bank_withdraw_gold', style=STYLE_REC),

        # --- item stalls on the flea market 9701 (P7 stage 2; shop_storage.md F9-F14,
        # market.py). The same bytes in both builds (spec_2009 0x475A25/0x5E, 0x473E14/0x5F,
        # 0x473E14/0x60, 0x4507FC/0x61, 0x473E14/0x62 "identical"). All five are MUST_REPLY:
        # every handler answers itself and the registry refusal is the exception backstop.
        # 0x5E is raw: a list whose item_count counts NULL nodes does not decode and must
        # still get its 0x82 {2} (F9 3.3). ---
        0x5E: Route('_handle_stall_open'),                          # Start -> 0x82 + 0x85
        0x5F: Route('_handle_stall_stop_edit'),                     # Stop -> 0x83 {1} (+ 0x86)
        0x60: Route('_handle_stall_close'),                         # Close -> 0x84 {1} (+ 0x86)
        0x61: Route('_handle_stall_visit', style=STYLE_REC),        # sign click -> 0x87
        0x62: Route('_handle_stall_buy', style=STYLE_REC),          # Purchase -> 0x88 + 0x89

        # --- crafting completions (P4 stage 3, item_inventory.md F11-F13; crafting.py) ---
        # Client-timed: sent after the 5 s progress bar, with a busy flag set that only the
        # result clears, so every one gets exactly one S2C 0x8D / 0x8E / 0x8F. The same send
        # sites and grammars in both builds (spec_2009 0x4724C7/0x67, 0x472478/0x68,
        # 0x472602/0x69: "identical"). The registry.MUST_REPLY rows stay the backstop for a
        # handler that raises.
        0x67: Route('_handle_craft_complete', style=STYLE_REC),
        0x68: Route('_handle_reinforce_complete', style=STYLE_REC),
        0x69: Route('_handle_gather_complete', style=STYLE_REC),
        # Elemental stone extraction (P8 stage 4, item_inventory-stone-extraction, F14): window
        # 0x473 -> 0x9C (+ the 0x18 stone grant), or 0x9C {0}. Same bytes in both builds
        # (spec_2009 0x4765EE/0x72 "identical"); the MUST_REPLY 0x9C {0} row is the backstop
        # for a handler that raises or a request from outside the world.
        0x72: Route('_handle_stone_extract', style=STYLE_REC),

        # --- the Item Mall / Spark Shop (P8 stage 2; premium_cash.md F3-F7, mall.py). The client
        # can only be put INTO the mall by the server (`!mall` -> S2C 0x6A); these are the
        # requests its windows send once it is there. Both builds: 0x42 / 0x45 / 0x46 are
        # wire-identical, the 2009 0x43 / 0x47 carry a u8 option per item (spec_2009 0x466BDE /
        # 0x468D84 / 0x467D8C) - the handlers decode with the server's build. 0x42 / 0x43 / 0x45
        # are raw so a payload that does not decode is still answered (0x42: the world replay
        # always runs); 0x46 / 0x47 keep their registry.MUST_REPLY rows as the backstop. ---
        0x42: Route('_handle_mall_close'),                          # EXIT -> moves, 0x6B, map-load replay
        0x43: Route('_handle_mall_buy'),                            # Buy / cart / slot ext -> 0x6C per item
        0x45: Route('_handle_mall_delete'),                         # box Delete -> 0x6E (30% mileage)
        0x46: Route('_handle_mall_refresh'),                        # after charge / restore -> ONE 0x70
        0x47: Route('_handle_mall_gift', style=STYLE_REC),          # gift dialog 0x1F9 -> 0x71 (+ 0x79 / 0x6D)

        # --- using cash items (P8 stage 3; premium_cash.md F8-F11, F13, F14; cashuse.py). A
        # double-click in the Spark Items / cash bag tab opens the item's window, whose OK sends
        # one of these (wire-identical in both builds: spec_2009 0x469072/0x48 /0x49, 0x469382/
        # 0x4A, 0x468F05/0x4C /0x71, 0x469C54/0x70). 0x48 / 0x49 / 0x4A open the waiting box, so
        # each always gets its 0x72 / 0x73 / 0x76 (registry.MUST_REPLY stays the backstop for a
        # handler that raises or a payload that does not decode); 0x70 / 0x71 always get their
        # 0x9A / 0x9B; 0x4C shows no box and gets no reply of its own. ---
        0x48: Route('_handle_cash_item_use', style=STYLE_REC),      # window 0x3F4 OK -> 0x72 (owner + holders)
        0x49: Route('_handle_cash_rename', style=STYLE_REC),        # window 0x3F1 -> 0x73 + 0x74
        0x4A: Route('_handle_cash_stat_reset', style=STYLE_REC),    # window 0x3FA -> 0x76 18 B / 12 B
        0x4C: Route('_handle_cash_megaphone', style=STYLE_REC),     # window 0x3FF -> orange 0x90 to all
        0x70: Route('_handle_cash_region_warp', style=STYLE_REC),   # world map -> 0x471 -> 0x9A + map load
        0x71: Route('_handle_cash_friend_warp', style=STYLE_REC),   # window 0x472 -> 0x9B + map load

        # Never-reply requests (roadmap 1.3) that had no branch: consume with a log line.
        # Privacy refuse options (chat_mail_gm-privacy-flags; D13): same 5 u8 in both builds.
        # NEVER_REPLY - the handler only stores them.
        0x40: Route('_handle_privacy_flags', style=STYLE_REC),
        # Messenger status Online/Busy/AFK -> 0x60 to the watchers (never to the sender).
        0x37: Route('_handle_messenger_status', style=STYLE_REC),
        # Memo window closed -> the memos that client was shown are deleted (no reply).
        0x44: Route('_handle_memo_delete'),
        0x7C: Route(log='[0x7C] dungeon exit - consumed (no instances yet)'),
    }

    # EN 2009 routes (client-2009-world): the 2008 table with the C2S opcodes the 2009 client
    # re-used, dropped or added (spec_2009 C2S keys; corpus_2009 c2s_format_diff.json). Every
    # C2S the 2009 spec lists has a route here, so nothing it sends logs as "Unhandled"
    # (test_world2009.Routes2009). Picked per server in __init__ (self.routes).
    ROUTES_2009 = routes_2009(ROUTES)
    # C2S send sites the client can never reach but whose opcode byte a real request shares
    # (registry.dispatch drops them before the route runs). spec_2009 0x418A6B/0x0C and
    # 0x418A6B/0x0E are the room-host effect/buff expiry broadcasts renumbered from 2008
    # 0x97/0x99: they need scene+0xF40 != 0, which CreateSockets (0x44082A) only ever writes 0.
    DEAD_C2S_KEYS = {
        cfgmod.BUILD_2009: {
            '0x418A6B/0x0C': 'room-host effect-expired broadcast (2008 0x97), unreachable in the EN 2009 exe',
            '0x418A6B/0x0E': 'room-host buff-expired broadcast (2008 0x99), unreachable in the EN 2009 exe',
        },
    }

    def _dispatch(self, sock, session, opcode, payload, no_enc=False):
        registry.dispatch(self, self.routes, sock, session, opcode, payload, no_enc)

    # ========================================================================
    # P0 misroute fixes and interim stubs (roadmap section 3, P0 steps 5 and 7).
    # Stub replies are the MUST_REPLY refusals in registry.py (registry.send_refusal),
    # so a handler and the exception fallback always send the same bytes.
    # ========================================================================
    @staticmethod
    def _client_item_type(item_id):
        """Item Type as the EN client sees it (registry.is_skill_cast uses it)."""
        return en_item_type(item_id)

    def _handle_card_deck_list(self, sock, session, payload, no_enc=False):
        """C2S 0x63 CardDeckListRequest (0 B, sent by the client after every S2C 0x03)
        -> S2C 0x8A, then one S2C 0x59 per non-empty quest slot
        (quest_cards_misc-stop-0x63-echo + -03-quest-card-fields, quest doc F7 step 3).

        S2C 0x63 is the Battlefield window counter (u16 + u8); the old EMPTY 0x63 reply
        caused Fireway short reads and garbage "Join(?/12)" labels (S2-01).

        The 0x59 burst re-arms the ready flags. S2C 0x03 carries the active slots and their
        progress but ZEROES the three ready flags at scene+0x2A7 and cannot send them, so
        without this a quest whose kills are done loses its green "ready to complete" state
        on every portal and relog. Doing it on 0x63 is what guarantees it lands after the
        0x03/0x07 the client is still digesting. The client re-checks demand items and gold
        itself (FUN_00426500), so the progress value goes back unchanged.

        2009 (client-2009-world): the same pair. S2C 0x8A is handled by the 2009 SubHandler5
        FUN_0045fb80 with the 2008 bytes (spec_2009 0x8A, added in this stage: the port's
        switch-table triage missed that if/else handler) and 0x59 reads progress for slots
        1..5 (spec_2009 0x59 slot < 6); the quest model's three slots all fit."""
        char = self._session_char(session)
        if char is None:
            P.send(self, sock, session, '0x8A', {'deck_count': 0})
            return
        deck = questmod.deck_fields(char)
        P.send(self, sock, session, '0x8A', deck)
        rows = questmod.progress_rows(char)
        for slot, progress in rows:
            P.send(self, sock, session, '0x59', {'slot': slot, 'progress': progress})
        log.info(f'[CARD] 0x63 -> 0x8A {deck["deck_count"]} card(s), '
                 f'0x59 re-arm for {len(rows)} quest slot(s): {rows}')
        self._send_channel_notice(sock, session)
        # ev-e1 / ev-e3 / ev-e4 (events.py): an active event's once-per-login announcement,
        # login gift and Event News popup are the tail of this reply (the arch09-resync-bundle
        # slot after 0x99 sub 8); nothing without an event, nothing again on a portal.
        self.events.after_resync(sock, session)

    def _handle_card_register(self, sock, session, rec, no_enc=False):
        """C2S 0x64 CardDeckRegisterRequest {u16 card_item_id} -> exactly one S2C 0x8B
        (P4 stage 4: quest_cards_misc-card-register, quest doc F10; spec 0x45A4F8/0x64,
        2009 0x45FB50/0x64 "identical").

        The request comes from dragging a Monster Card onto the Card Deck window (hotkey B)
        and confirming window 0x292 (live quest_cards_misc#07); the client then waits behind
        "Waiting for the server to response." until the 0x8B, which closes that box for
        every result (live #14). cards.register decides it in the F10 order and applies the
        client's own success change to the record: one card out of the bag model (NO 0x23 /
        0x19 - the client removes its card itself, a packet would take a second one) and
        the id appended to char['card_deck'] (persisted; every later 0x03 carries the count
        and every 0x63 the list, so the deck survives a portal and a relog)."""
        char = self._session_char(session)
        with self._combat_lock(session):
            outcome = cardsmod.register(char, rec.get('card_item_id', 0))
            if outcome.registered:
                self.store.mark_dirty(f'card {outcome.card_id} registered')
            P.send(self, sock, session, '0x8B', cardsmod.result_fields(outcome))
        name = EC.item_name(outcome.card_id) or f'item {outcome.card_id}'
        line = (f'[CARD] {session.get("char_name")!r} register {name} ({outcome.card_id}) -> 0x8B '
                f'{outcome.result}: {outcome.why}; deck {outcome.deck_count}/{questmod.MAX_DECK}')
        if outcome.result == cardsmod.RESULT_FAILED and 'bag model' in outcome.why:
            # The client only sends 0x64 for a card in ITS bag: the two bags disagree. The
            # next map load's 0x03 lists the model's bag and puts the client back in step.
            log.warning(line)
        else:
            log.info(line)
        if outcome.registered:
            # The card left the bag: it may be a quest Demand item (the unchanged-progress
            # 0x59 re-check, like any other bag change).
            self._quest_credit_item(sock, session, outcome.card_id, 1, no_enc)

    # ---- system notices (quest_cards_misc-system-notice-0x99, quest doc F16) ----
    def _channel_no(self, session):
        """The channel this connection is on: session['channel'] once a channel model sets
        it, else the first CHANNELS entry (one game server = one channel today)."""
        channels = self.config.channels()
        no = session.get('channel') or (channels[0][0] if channels else 1)
        return max(1, min(0xFF, int(no)))

    def _send_channel_notice(self, sock, session):
        """S2C 0x99 sub 8 {channel_no}: the client prints "In channel %u." (live
        quest_cards_misc#15; spec_2009 0x99 "identical"). Once per connection, on the first
        C2S 0x63: the client sends that at the very end of its S2C 0x03 handler, so the line
        lands after the whole enter-world burst - the same reason F7 re-arms the quest
        slots there - and the enter-world sequence itself stays 0x03 0x07 0x15 [0x65] 0x28
        0x44. A 2009 channel change is a new connection, so it prints the new channel.
        Config CHANNEL_NOTICE false turns it off."""
        if session.get('channel_noticed') or not self.config.get('CHANNEL_NOTICE', True):
            return
        if not session.get('in_world'):
            return
        session['channel_noticed'] = True
        channel = self._channel_no(session)
        P.send(self, sock, session, '0x99', {'sub_type': 8, 'channel_no': channel})
        log.info(f'[NOTICE] 0x99 sub 8: "In channel {channel}." (first 0x63 of the connection)')

    def grant_item(self, session, item, count=1, what='grant'):
        """Give an item WITHOUT touching the wallet: S2C 0x99 sub 9 {item_id, count} (quest
        doc F16; GM/event grants). The client adds it with its own stacking rules and no
        ack - consumables merge into stacks of 999, etc items 99, equipment ONE per packet
        whatever the count, and a full tab drops it silently (spec 0x99 hazards) - and prints
        "you've received <name>. (Count:n)" with the gold label untouched (live #15). S2C
        0x18 GetItem would rewrite gold and Victy with absolute values.

        So the whole grant is checked against the bag model first (inventory.fits mirrors
        the client's tab capacities and stack sizes) and refused as a whole if it would not
        fit; then it goes out in stack-sized packets (999 / 99 / 1) and each is mirrored in
        the model. Types 3/4/5 are refused: the sub-9 handler adds nothing for them (a skill
        book is learned through !learn / !give). Returns (ok, why)."""
        item, count = int(item), int(count)
        sock = session.get('sock')
        if sock is None or not session.get('in_world'):
            return False, 'not in world'
        if not self._grantable_item(item, what):
            return False, f'item {item} is not in the EN client catalog'
        bag = self._bag(session)
        if bag is None:
            return False, 'no character in this session'
        tab = bag.tab_of(item)
        if tab is None:
            return False, (f'{EC.item_name(item)} ({item}) has Type {en_item_type(item)}: '
                           f'0x99 sub 9 only adds bag items (Types 0/1/2)')
        if not 1 <= count <= invmod.STACK_MAX[tab] * invmod.MAX_CAPACITY:
            return False, f'count {count} is outside 1..{invmod.STACK_MAX[tab] * invmod.MAX_CAPACITY}'
        with self._combat_lock(session):
            why = bag.fits(item, count)
            if why is not None:
                return False, why
            left = count
            while left > 0:
                chunk = min(left, invmod.STACK_MAX[tab])
                bag.add(item, chunk)
                P.send(self, sock, session, '0x99', {'sub_type': 9, 'item_id': item, 'count': chunk})
                left -= chunk
            self.store.mark_dirty(f'{what} {item}')
        log.info(f'[ITEM] {what}: {EC.item_name(item)} ({item}) x{count} -> 0x99 sub 9 '
                 f'({-(-count // invmod.STACK_MAX[tab])} packet(s)); bag {tab} now {bag.count(item)}')
        self._quest_credit_item(sock, session, item, count, session.get('no_enc', True))
        return True, None

    def _handle_battlefield_info(self, sock, session, payload, no_enc=False):
        """C2S 0x38 BattlefieldInfoRequest (T key opens Battlefield window 0x14; spec
        0x446628/0x38, pvp_arena.md flow step 2) -> S2C 0x63 {player_count, join_count},
        rendered as "N Players" and "Join(n/12)". Always all 3 B: a short 0x63 formats the
        labels from stale stack data. Nothing is awaited and no modal is open. No battle
        rooms or Death Match queue exist before pvp-battlefield-queue, so both are 0."""
        P.send(self, sock, session, '0x63', {'player_count': 0, 'join_count': 0})
        log.info('[BATTLE] 0x38 battlefield window -> 0x63 {0 players, join 0/12}')

    def _warn_dead_host_code(self, sock, session, payload, no_enc=False):
        """C2S 0x93/0x94/0x95/0x97/0x99 are battle-host broadcasts of the room server compiled
        into the client (FUN_00428090, FUN_0041ac20, FUN_00418294). They need scene+0xF24 != 0
        and a CSNSockMan at scene+0xF98, neither of which the EN 2008 exe ever sets, so a real
        client never sends them to 7022 (pvp-stale-cleanup). The registry already logged the
        opcode and decoded fields; this makes the event a warning."""
        log.warning(f'[DISPATCH] room-host dead-code opcode ({len(payload)}B) from '
                    f'{session.get("username")!r}: the EN client cannot send it; '
                    f'suspect a modified client or a framing error')

    def _handle_note_send(self, sock, session, rec, no_enc=False):
        """C2S 0x4B SendNote (social_friend-memos + chat_mail_gm-memo-delivery /
        -note-reply-9999; messenger.Messenger.note). The Note item send site (2008 0x461064,
        2009 0x467D8C) closes window 0x3FD and waits behind "Waiting for the server to
        response." until S2C 0x77, which the handler always sends: {1, serial} after the memo
        is stored for the recipient (online or offline), {0} otherwise. Item 9999 is the gift
        thank-you reply (0x46098E / 0x46832E): stored the same way, never answered (no wait
        box; registry._note_refusal is empty for it)."""
        self.messenger.note(sock, session, rec)

    # ---- item stalls (P7 stage 2: shop_storage.md F9-F14; market.py owns the logic) ----
    def _handle_stall_open(self, sock, session, payload, no_enc=False):
        """C2S 0x5E StallOpen (Start) -> S2C 0x82 {1} and 0x85 to the map, or 0x82 {2|9}
        (F9). The selling mirror is set whatever the outcome: a failed Start leaves the
        client's +0x20 at 1, so its Close must still get 0x84 {1} (F13 step 5)."""
        self.market.open(session, payload)

    def _handle_stall_stop_edit(self, sock, session, payload, no_enc=False):
        """C2S 0x5F ItemStallStopForEdit -> 0x83 {1} (F12 step 4) while the selling mirror is
        set or a stall is open - one that crossed our 0x83 / 0x84 is ignored (C8); an open
        stall closes (escrow back in the bag, 0x86 to the map) - the client is back in setup
        mode, whose close is local (market.py docstring)."""
        self.market.stop_edit(session)

    def _handle_stall_close(self, sock, session, payload, no_enc=False):
        """C2S 0x60 ItemStallClose (F13) -> 0x84 {1} while the selling mirror is set (the
        client re-sends 0x60 by itself after S2C 0x08 and from FUN_0042cf20: only the first
        is answered); an open stall closes (escrow back, 0x86)."""
        self.market.close_request(session)

    def _handle_stall_visit(self, sock, session, rec, no_enc=False):
        """C2S 0x61 {stall_owner_uid} (a click on a remote player showing a stall sign) ->
        S2C 0x87 {1, list} / {1, 0} / {0} / {6} (F10)."""
        self.market.visit(session, rec.get('stall_owner_uid', 0))

    def _handle_stall_buy(self, sock, session, rec, no_enc=False):
        """C2S 0x62 {seller_uid, id, qty, descriptor} -> S2C 0x88 {1} to the buyer + 0x89 to
        the seller, or 0x88 {0} and a fresh 0x87 to the buyer (F11)."""
        self.market.buy(session, rec)

    def _hook_stall_close(self, server, session, map_code=None, reason=None, **_):
        """`before_server_map_load` hook (shop_storage F14.1; P4 stage 4 exit criterion 7
        "portal away works", P7 stage 2): market.Market.map_load. After a Start the client
        is in selling mode (ctx+0x20 = 1) with the listed items OUT of its bag. Left alone,
        the map load's 0x08 handler fires the stall window's close event, which in selling
        mode only sends C2S 0x60, and the 0x84 {1} answering it would land AFTER the 0x03,
        closing window 0x259 through FUN_004665e0, which puts the listed items back a second
        time on top of the reloaded bag (client-side duplicate). So the stall closes here,
        BEFORE the lead: its escrow goes back into the bag model (and session['rebuild_03']
        makes _map_transfer rebuild the 0x03 it built before this hook), the old map's
        holders get 0x86, and 0x84 {1} closes the window while the old bag is loaded. A
        stall in setup mode only (never Started, ctx+0x20 = 0) needs nothing: the 0x08
        close event closes it locally before the 0x03. Every map change (portal, !warp,
        revive, village transfer) runs through _map_transfer, so all of them are covered."""
        self.market.map_load(session, map_code, reason)

    def _handle_world_sync(self, sock, session, payload, no_enc=False):
        """C2S 0x0D = local-player MOVEMENT (sent on every input change, ~every 210 ms while
        moving, a 15 s keepalive while idle; spec 0x42CE94/0x0D, 2009 0x42E704/0x0D
        "identical"), NOT a heartbeat. NEVER answered to the sender: the old code echoed it
        (MT-encrypted), which desynced the client's cipher stream into garbage the client
        parsed as random opcodes -> spurious menus, 'death', and loading-screen/map-warp
        (registry NEVER_REPLY 0x0D).

        P5 stage 2 (presence.py), for a packet of the map the session is in:
        - world-position-estimate: session['pos'] follows the walk. Only the interact branch
          ((state_lo >> 16) & 0xF != 0) carries pos_x/pos_y - a plain walk step is 18 bytes
          of state bits - so the server dead-reckons from the state words at the measured
          walk speed along the EN floor lines and takes every interact tail as a fix
          (presence.advance). Saves, drops, skill origins and the monster AI read it.
        - world-move-relay: the same words go to the OTHER players whose clients hold the
          mover, as one S2C 0x1B node each (presence.relay), never back to the mover.
        Combat (the retail 61 B hit report) is handled below from the same decoded packet."""
        rec, err = registry.decode(0x0D, payload, self.client_build)
        if rec is not None:
            # The client's own logic clock, in the same ms the next 0x03/0x08 must carry
            # back (item_inventory.md 1.6): every step reports the time since the last one.
            self._advance_clock(session, rec.get('logic_elapsed_ms', 0))
        elif err:
            log.debug(f'[MOVE] 0x0D ({len(payload)}B) not decoded: {err}')
        if rec is not None and self._move_is_current(session, rec):
            # A packet naming another map was sent before a map load finished: it neither
            # moves the estimate nor reaches the new map's players (world F2 step 2).
            # The first one naming the current map proves the client finished loading it:
            # only from then on may the dev driver's memory samples move session['pos']
            # (_track_driver_position).
            session['map_confirmed'] = session.get('current_map')
            if presence.advance(session, rec,
                                dash_knock=bool(self.config.get('POSITION_ESTIMATE_DASH_KNOCK', True))):
                pos = session.get('pos') or (0.0, 0.0)
                log.info(f'[MOVE] {session.get("char_name")!r} stops at ({pos[0]:.0f}, {pos[1]:.0f}) '
                         f'(estimate; fix: {presence.move_state(session).get("fix")})')
            presence.relay(self, session, rec)
        if rec is not None:
            # cs-regen: the client's HP regen timer only runs while the player stands idle
            # (+0x904 == 8) and restarts whenever he does anything else. 0x0D is sent on every
            # input change, so its input bits say when he started and stopped moving; a
            # non-zero action event (bits 12-15, +0x94C) restarts the window. 0xD is the
            # client's own death (combat_skill.md F8 step 6): a corpse is never idle, so the
            # HP timer stays stopped until he moves again (the revive flow is cs-player-death).
            lo = int(rec.get('state_lo', 0))
            event = (lo >> 12) & 0xF
            now = time.monotonic()
            period = self.config.REGEN_SECS
            with self._combat_lock(session):
                hpmp.regen_activity(session, hpmp.moving_state(lo) or event == self.ACTION_DIE,
                                    now, period)
                if event and event != self.ACTION_DIE:
                    hpmp.regen_interrupt(session, now, period)
        # P3 stage 4: facing, monster position estimates, the trap victim, the death watch and
        # (config) monster contact damage - all read from the same decoded packet.
        self._on_move_state(sock, session, rec)
        # After _on_move_state (it moved the mob to the reported victim position).
        if rec is not None and ((rec.get('state_lo', 0) >> 16) & 0xF) == HIT_REPORT_EVENT:
            # The client's own hit detection (FUN_00416380) caught a connecting swing: its
            # +0x950 = 7 goes out in bits 16-19 with the victim in the interact tail. This is
            # the damage source (the retail flow); the reply releases the victim's hit-lock.
            target = int(rec.get('target_uid', 0))
            log.info(f'[ATK] hit report -> uid={target:#x} event={rec.get("target_action_event")}')
            # The 0x2A release / the kill's 0x29 0x21 (0x12) 0x18 are events this report
            # caused, not an echo of the movement packet: registry.detached(), as for the trap
            # and death events of _on_move_state (the NEVER_REPLY check logged every retail
            # kill as a "policy violation").
            with registry.detached():
                self._resolve_hit(sock, session, 0, target, no_enc=True, ack=True)
        elif rec is not None and ((rec.get('state_lo', 0) >> 16) & 0xF) in self.SKILL_HIT_EVENTS \
                and 'target_uid' in rec and self._move_is_current(session, rec):
            target = int(rec.get('target_uid', 0))
            event = (rec.get('state_lo', 0) >> 16) & 0xF
            log.info(f'[ATK] skill/strong hit report -> uid={target:#x} event={event}')
            with registry.detached():
                self._resolve_skill_report(sock, session, target, event)

    # C2S 0x0D interact events of a strong attack or an attack skill landing (bits 16-19 = the
    # attacker's +0x9E0: 9 = hit, 4 = guarded; the combo HUD RE, 0x417A89/0x41722A). The client's
    # own detection caught the victim and set its hit-lock, exactly as for a basic swing (7).
    SKILL_HIT_EVENTS = frozenset((9, 4))
    # A report this soon after the caster's attack-skill cast (S2C 0x25) belongs to that cast.
    SKILL_REPORT_SECS = 2.0

    def _resolve_skill_report(self, sock, session, mob_uid, event):
        """Answer a strong-attack / skill hit the client caught itself (C2S 0x0D event 9/4).

        Live 2026-09-24: an Ice Spear drew its hit on a Ssiyo 128 px ahead while the server's
        box (from its estimate of the mob) found nothing; the report was ignored, no 0x2A ever
        cleared the client's hit-lock, and the Ssiyo stood flashing in its hurt/frozen pose for
        good. So every caught victim is answered:
        - hit by the server's box in this cast already: only the hit-lock release;
        - missed by the box (the client's view of the mob differs): the cast's skill damage,
          trusting the client's detection as for basic swings, then the release;
        - no attack-skill cast in SKILL_REPORT_SECS: the strong attack (key D), 1.5x basic.
        A kill needs no release (its 0x29 clears the hit-lock on every client)."""
        now = time.monotonic()
        cast = session.get('last_cast')
        fresh = cast is not None and now - float(cast['t']) <= self.SKILL_REPORT_SECS
        with self._combat(session):
            mons = session.get('monsters') or {}
            mob = mons.get(mob_uid)
            if mob is None:
                mob = next((m for m in mons.values() if (m.uid & 0xFFFF) == (mob_uid & 0xFFFF)), None)
            if mob is None or not mob.alive:
                return
            if fresh and mob.uid in cast['hit']:
                self._release_hit_lock(sock, session, mob, event)
                return
            if fresh:
                sd = SK.skill_def(int(cast['skill']))
                dmg, what = self._skill_hit_damage(session, sd, mob, event)
                what += ', client-caught'
                cast['hit'].add(mob.uid)
            else:
                dmg, what = self._compute_damage(session, 1, mob), f'strong attack (event {event})'
            killed = self._damage_monster(sock, session, mob, dmg, what, take_control=False)
            if not killed:
                self._release_hit_lock(sock, session, mob, event)

    # C2S 0x0D action_event (+0x94C) the client writes for its own death (case 0xD).
    ACTION_DIE = 0xD

    @staticmethod
    def _move_is_current(session, rec):
        """A decoded C2S 0x0D the world may act on: the session is in world (its own 0x07 is
        out) and the packet names the map it is on (world F2 step 2)."""
        return (bool(session.get('in_world'))
                and int(rec.get('map_code', -1)) == int(session.get('current_map') or -1))

    # C2S 0x2B bytes 0-4 -> session['refuse'] (D13: decoded once here, the C2S 0x40 handler
    # updates it later; consumers ask refuses(target, kind), never read the packet again).
    REFUSE_KINDS = privacy.KINDS

    def _handle_privacy_flags(self, sock, session, rec, no_enc=False):
        """C2S 0x40 SetPrivacyRefuseOptions {u8 x5: whisper, exchange, party, talk, friend}
        (2008 0x4414BA/0x40, 2009 0x44206A/0x40 identical), sent when the player applies the
        Options window (chat_mail_gm-privacy-flags + party-privacy, trade-privacy-flags,
        quest_cards_misc-privacy-flags). The live flags change at once and the character
        record keeps them (F4 schema `refuse`). Never answered: the client waits for
        nothing, and roadmap 1.3 lists 0x40 under "never reply" (registry.NEVER_REPLY)."""
        flags = privacy.from_record(rec)
        before = session.get('refuse')
        session['refuse'] = flags
        # Only a character this session entered with: _session_char falls back to the first
        # slot at character select, which would file the flags under the wrong character.
        char = self._session_char(session) if session.get('char_name') else None
        if char is not None:
            self.store.save_refuse(char, flags, reason='0x40 refuse flags')
        log.info(f'[PRIVACY] {session.get("char_name") or session.get("username")!r} refuses '
                 f'{privacy.describe(flags)} (was {privacy.describe(before) if before is not None else "unset"})')

    def _handle_enter_world(self, sock, session, rec, no_enc=False):
        """
        Enter-world request (C2S 0x2B, spec 0x42F904/0x2B, 42 bytes after opcode):
          u8 x5   refuse_whisper/exchange/party/talk/friend: the privacy flags the client
                  read from HKCU\\Software\\Hamelin\\WindSlayer\\<name> (D13)
          str[16] p2p_ip: the client's own P2P address. Empty with the patched exe, whose
                  post-login LAN probe is NOPed (roadmap 1.1); it is not a constant
                  "10.5.0.2" (S3-10)
          u32     p2p_udp_port (42907 = 0xA79B, the client's UDP bind; not a magic)
          str[17] char_name
        2009 (spec_2009 0x4315D7/0x2B, 43 B): + u8 account_flag_152, the echo of the S2C 0x02
        byte (always 0 from this server; logged, not used). The automatic relogin sender
        0x452133/0x2B (S2C 0x02 success while gs+0x408 is set, i.e. after a channel change)
        is byte-identical, so parse() matches both keys (rec.candidates) and this handler
        takes it the same way: the character still named in scene+0x20C enters on the new
        connection, spawned with the same account uid (the relogin 0x02 is not read, so the
        client keeps scene+0x224 - the registration gate needs that uid).

        Flow F5. The name must be one of this account's characters: an unknown one gets
        the S2C 0x02 character list back (the registry's must-reply refusal), which closes
        window 0x16 and reloads the select screen. Returning silently, as the old handler
        did, leaves the patched client on "Waiting for the server to response." forever,
        because its timer-2 fallback is patched out (B14, S1-09).

        On success: the SAVED map and position (char['map'/'x'/'y'], not a constant 101),
        S2C 0x03 (world state, loads that map), then the own 0x07 record from the store,
        the 0x15 welcome (first entry of the connection only), 0x28/0x44 HP/MP and the
        map's 0x1A monsters.
        """
        char_name_str = names.clean(rec.get('char_name', ''))
        session['refuse'] = privacy.from_record(rec)
        session['p2p'] = (str(rec.get('p2p_ip', '')), int(rec.get('p2p_udp_port', 0)))
        log.info(f'[ENTER_WORLD] Character "{char_name_str}" refuse={session["refuse"]} '
                 f'p2p={session["p2p"][0]!r}:{session["p2p"][1]}'
                 + (f' flag_152={rec.get("account_flag_152")} via {"/".join(rec.candidates)}'
                    if self.client_build == cfgmod.BUILD_2009 else ''))

        username = session.get('username')
        char = self.store.find_character(username, char_name_str)

        if not char:
            # F5 step 2: re-send the 0x02 list (registry.MUST_REPLY[0x2B] owns those bytes,
            # so this refusal and the login reply are built by the same builder).
            log.warning(f'[ENTER_WORLD] Character "{char_name_str}" is not on account '
                        f'{username!r}; re-sending the character list')
            registry.send_refusal(self, sock, session, 0x2B, rec)
            return
        # F3/F5: index the in-world character name. False only when a duplicate login
        # closed this session while the request was in flight: answer nothing, the socket
        # is already closed.
        if not self.world.enter(session, char_name_str):
            log.info(f'[ENTER_WORLD] {username!r} no longer owns uid {session.get("uid")}; ignored')
            return

        # The 0x03 below destroys every client entity; monsters of an earlier map (a second
        # entry on this connection) and their despawn/respawn timers go first, so no stale
        # 0x1A can land on the new map before _spawn_map_monsters (cs-monster-death).
        self._clear_map_monsters(session)
        # Persist identity so portal/map-change (0x7E) can find the character
        # and use the correct source map for portal lookups.
        session['char'] = char
        session['char_name'] = char_name_str
        session['level'] = R.level_of(char)
        # chat_mail_gm-privacy-flags: the flags this character's client just sent are its
        # current ones; the record keeps them for checks while it is offline.
        self.store.save_refuse(char, session['refuse'], reason='0x2B refuse flags')
        # GM identity for this life of the session (chat_mail_gm-gm-flag-manner, F10.0 step
        # 3): the 0x06 authorization and the '!' dev-command router read it, and the 0x07
        # record below carries the same gm_level / gm_hidden to the client. A change to the
        # stored flag therefore takes effect on the next enter world or map change.
        with self.store.lock:
            session['gm'] = int(char.get('gm') or 0)
            session['gm_hidden'] = int(char.get('gm_hidden') or 0)
            current_map = int(char.get('map') or self.config.START_MAP)
            # The point the 0x07 below spawns the player on: the session tracks it from
            # here (world-persistence), so a disconnect before any portal or 0x0D saves
            # what the client was actually shown.
            session['pos'] = (float(char.get('x', self.config.START_X)),
                              float(char.get('y', self.config.START_Y)))
        if session['gm']:
            log.info(f'[ENTER_WORLD] "{char_name_str}" is a GM (gm_level={session["gm"]}, '
                     f'gm_hidden={session["gm_hidden"]})')
        session['current_map'] = current_map

        # Set the local player's current HP/MP before the map load so the character is NOT
        # spawned "dead" (0 HP): the 0x07 record and the 0x28/0x44 the transfer sends all
        # read them through records.vitals. Without this the Status sheet reads 0/max and
        # the player cannot attack.
        #
        # cs-hp-mp-model: the store holds the CURRENT values only; the maxima come from the
        # client's own formula (hpmp.derive: FUN_00427d40/FUN_00427f40 over job, level,
        # stats and equipment). The old `max(cur, 100)` made the maximum whatever the record
        # last stored, so a potion above it was clamped away and relog kept the loss (the
        # P2 live finding). A relog never refills: only values above the maximum are cut.
        cur_hp = int(char.get('hp', storemod.DEFAULT_HP))
        cur_mp = int(char.get('mp', storemod.DEFAULT_MP))
        with self._combat_lock(session):
            session['dead'] = session['reviving'] = False
            if cur_hp <= 0:
                # A 0 in the 0x07 record spawns a corpse with no death dialog and no way out
                # (combat_skill.md 1.6). A character that logged out dead (hp 0 is saved at
                # the death, cs-player-death) enters the way the dialog's "Revived" would
                # have taken him: at the revive point of the map he died on, with the revive
                # HP - never a free refill.
                rmap, rx, ry = self._revive_point(current_map)
                cur_hp = self._revive_hp(hpmp.derive(session, char).max_hp)
                ax, ay = self._arrival_point(rmap, rx, ry)          # where the map load lands him
                log.warning(f'[DEATH] "{char_name_str}" was saved dead on map {current_map}; entering '
                            f'revived on map {rmap} at ({ax:g}, {ay:g}) with {cur_hp} HP')
                current_map = session['current_map'] = rmap
                session['pos'] = (rx, ry)
            session['hp'], session['mp'] = cur_hp, max(0, cur_mp)
            # cs-buffs: the persisted slots run again for the time they had left at logout
            # (the 0x07 the transfer below sends carries them); an aura's mHP then counts in
            # the maximum computed next. cs-skill-cast: the server cooldowns belong to the
            # character for the life of the server process, so a relog is no way around a
            # 60 s CT (they are not persisted: monotonic stamps mean nothing after a restart).
            restored = buffmod.restore(session, char.get('buffs'), time.monotonic())
            # (dict.setdefault is atomic; the world lock may not be taken under a combat lock.)
            session[self.SKILL_CD_KEY] = self.skill_cooldowns.setdefault(char_name_str.lower(), {})
            d = hpmp.refresh(session, char)
        if restored:
            log.info(f'[BUFF] "{char_name_str}" enters with '
                     f'{[(b["id"], buffmod.remaining_ms(b, time.monotonic())) for b in restored]} (id, ms left)')
        log.info(f'[HPMP] "{char_name_str}" Lv{d.level} job {d.job}: HP {session["hp"]}/{d.max_hp} '
                 f'MP {session["mp"]}/{d.max_mp}, regen +{d.regen_hp}/+{d.regen_mp} per '
                 f'{self.config.REGEN_SECS:g} s')

        # The map load itself is the F6 primitive (world-maptransfer): 0x03 world state from
        # the store, the own 0x07 record, current 0x28/0x44 and the map's 0x1A monsters,
        # with the welcome line hooked in after the 0x07 (on_enter_world).
        #
        # lead=None: no 0x08 here. 2026-06-09 Phase 4 step 7 REVERTED - an 0x08 at enter
        # world made the client never transition to in-game (no heartbeats after 0x2F+0x63,
        # connection closed in 16 s). "PySlayer doesn't send 0x08 on enter-world; only on a
        # client-initiated change. The 0x03's mapcode handles the initial map load."
        #
        # The map code and point are the character's saved ones (world-persistence): the old
        # call left the builder's default 101, so a relog always loaded the town whatever
        # the store said. No S2C 0x2B follows the 0x07: its 2000-byte padded row overwrote
        # the record 0x07 had just set.
        if session.pop('in_cash_shop', False):
            # An enter-world of a session the server still had in the mall (its client went
            # back to character select without the 0x42 EXIT): this 0x2B is a fresh world
            # entry, so the stale mall state goes - _map_transfer refuses mall sessions
            # (premium_cash-presence, P8 stage 4).
            session.pop('mall', None)
            log.warning(f'[MALL] "{char_name_str}" enters the world while the server had its session in '
                        f'the mall: mall state dropped')
        x, y = session['pos']
        self._map_transfer(sock, session, current_map, x, y, lead=None, reason='enter_world',
                           no_enc=no_enc)

        # Never send UDP to a field client (roadmap F11): the old UDP 0x11 "map server"
        # experiment makes the client overwrite its own p2p_ip (scene+0x224), and the
        # field simulation runs on the client's UDP loopback with no server. The 2026-06
        # "activation" packets (0x6F cash list, 0x19 sell result) were built on a disproven
        # theory and are gone; the local player comes from the own 0x07 record.

        # Do not send, in world: S2C 0x2C (a room roster add; froze the client in a
        # 2026-04 test), 0x08 here (re-map-load loop, see above) or 0x02 (resets the game
        # state to character select). The patched exe removed the timer-2 "No response
        # from the server" fallback (0x43ED30), so no keepalive is needed.

    # =================================================================
    # In-game packet handlers (ported from PySlayer game_server.py)
    # =================================================================

    def _handle_chat(self, sock, session, rec, no_enc):
        """C2S 0x03 NormalChat {u8 msg_len, bytes message} -> S2C 0x16 ChatMessage
        {str[17] sender_name, u8 text_len, str text} (chat_mail_gm-chat-builders, F1).

        - No leading count byte: the client reads the name first (0x451B0D), so the old
          `u8 1` shifted everything and the line showed as "test :" (S2-05, B1).
        - The sender is the character name, not the account name (S2-06, B3).
        - Text stays raw bytes (cp949 on the client): NUL-cut and clamped to 60 bytes for
          the 61-byte stack buffer (S1-14, S3-03). A mode-5 "/a hi" arrives as
          msg_len 5 "hi\\0hi"; the NUL cut keeps "hi".
        - '/' lines are ordinary chat (emotes must reach other clients); '!' is the GM
          dev-command hook (plus a GM's exact `/bank`, shop_storage-bank-fallback, and
          `/warp`, shop_storage-flea-warp).

        Map fan-out (chat_mail_gm-map-chat-broadcast, F1 step 3; P5 stage 4): the SAME 0x16
        goes to the sender and to every other in-world session on the sender's map, once
        each. The sender needs it too: the client has no local echo (spec 0x44790E/0x03 "the
        line appears only when the server broadcasts S2C 0x16", both builds) and an echoed
        emote plays on the sender's own character (C34). A receiver whose client holds the
        sender's entity also shows the bubble / emote on it (spec 0x16: by name). 2009
        receivers drop a blacklisted sender themselves (FUN_00484190), so the server does not.
        No privacy flag gates map chat: 'talk' is the messenger chat room (privacy.py)."""
        text = P.cut_text(rec.get('message', b''), CHAT_TEXT_MAX)
        name = session.get('char_name')
        if not text or not name or not session.get('in_world'):
            log.info(f'[CHAT] dropped {len(text)}B line from {session.get("username")!r}: '
                     f'{"empty" if not text else "not in world"}')
            return
        # F1 step 2.2: the client blocks chat below -40 manner itself (live verify 0x97),
        # so this is the server mirror that makes a GM mute stick against a patched client
        # (chat_mail_gm-gm-flag-manner). A dropped line gets no reply, as on the client, and
        # the gate is before the '!' router exactly as F1 orders its steps - a GM who mutes
        # their own account loses the dev commands too (their client blocks the line first).
        manner = self.store.manner(session.get('username'))
        if manner <= gm.MANNER_CHAT_LIMIT:
            log.info(f'[CHAT] dropped line from {name!r}: manner {manner} <= {gm.MANNER_CHAT_LIMIT}')
            return
        if self._chat_command(sock, session, text):
            return
        # F1 step 2.2 rate limit, applied to the lines that reach other players only: a '!'
        # dev command is answered to the GM alone (and the offline rigs type them back to
        # back). The real client's own 700 ms gate keeps a player under 5 lines / 3 s, so
        # only a patched client flooding the map is ever dropped - silently, like the
        # client's own "Do not Spam." refusal, which sends nothing.
        if not self._chat_rate_ok(session):
            log.info(f'[CHAT] dropped line from {name!r}: more than {self.CHAT_RATE_LINES} lines '
                     f'in {self.CHAT_RATE_SECS:g} s')
            return
        fields = {'sender_name': name, 'text': text}
        P.send(self, sock, session, '0x16', fields)
        peers = self.world.peers(session)
        heard = sum(1 for peer in peers if self._push(peer, '0x16', fields, 'CHAT'))
        log.info(f'[CHAT] {name}: {_log_text(text, CHAT_TEXT_MAX)!r} (map {session.get("current_map")}, '
                 f'{heard} other player(s))')

    # chat_mail_gm F1 step 2.2: at most 5 broadcast lines per session in any 3 s window.
    CHAT_RATE_LINES = 5
    CHAT_RATE_SECS = 3.0

    def _chat_rate_ok(self, session, now=None):
        """True (and the line counted) while the session sent fewer than CHAT_RATE_LINES
        map lines in the last CHAT_RATE_SECS (session['chat_times'], chat_mail_gm 3.x).
        Only the session's own connection thread calls it, so the deque needs no lock.
        Friend chat (C2S 0x6B) counts here too: the client's 700 ms anti-spam timer is
        shared by normal, party and friend chat (F1.1 step 7)."""
        return self._rate_ok(session, 'chat_times', self.CHAT_RATE_LINES, self.CHAT_RATE_SECS, now)

    def _chat_command(self, sock, session, text):
        """GM '!' dev-command hook (chat_mail_gm F1 step 2.3): a GM's line starting with '!'
        is a command and is never broadcast. A non-GM '!' line is ordinary chat, and '/'
        is never intercepted (emotes must reach other clients) - with two exceptions: a GM's
        line that is exactly `/bank` is the shop_storage-bank-fallback command (F3b names it
        so) and runs `!bank`, and a GM's `/warp <map> [x] [y]` runs `!warp` (shop_storage-
        flea-warp). Returns True when the line was consumed."""
        if session.get('gm') and text.strip().lower() == self.BANK_SLASH_COMMAND:
            self._gm_chat_command(sock, session, b'!bank')
            return True
        # shop_storage-flea-warp: the design names the flea-market dev command `/warp 9701
        # <x> <y>`. No client prefix owns it - the whisper test is `/w ` WITH the space
        # (2009 FUN_004462e0 __strnicmp(text, "/w ", 3); 2008 the same), and no GM prefix
        # matches - so the client sends the line as ordinary chat and it runs `!warp`.
        lowered = text.strip().lower()
        if session.get('gm') and (lowered == self.WARP_SLASH_COMMAND
                                  or lowered.startswith(self.WARP_SLASH_COMMAND + b' ')):
            self._gm_chat_command(sock, session, b'!' + text.strip()[1:])
            return True
        if text.startswith(b'!') and not session.get('gm') and gm.split(text)[0] in self.PLAYER_COMMANDS:
            # A player's command line costs a chat-rate slot like a map line (P8 review): it
            # reaches no one else, but `!mall` is a whole mall entry and its exit a map load,
            # so a patched client typing it in a loop is dropped the same silent way.
            if not self._chat_rate_ok(session):
                log.info(f'[CHAT] dropped command line from {session.get("char_name")!r}: more than '
                         f'{self.CHAT_RATE_LINES} lines in {self.CHAT_RATE_SECS:g} s')
                return True
            self._player_command(session, text)
            return True
        if not (session.get('gm') and text.startswith(b'!')):
            return False
        self._gm_chat_command(sock, session, text)
        return True

    # shop_storage.md F3b: the dev chat line that opens the bank when the NPC path is gated.
    BANK_SLASH_COMMAND = b'/bank'
    # shop_storage.md F8 / roadmap P4 exit criterion 7: the dev line that reaches the flea
    # market 9701 (no field portal leads there; the towns' market portals do).
    WARP_SLASH_COMMAND = b'/warp'

    # ---- system lines (chat_mail_gm-system-notices, F4) ----
    @staticmethod
    def notice_fields(text, kind='info'):
        """S2C 0x15 fields for a system line of `kind` (NOTICE_KINDS). packets.build clamps
        the text, prefix included, to the 88-byte client buffer."""
        msg_type, prefix = NOTICE_KINDS[kind]
        return {'msg_type': msg_type, 'text': P.to_bytes(prefix) + P.to_bytes(text)}

    def _notice(self, sock, session, text, kind='info'):
        """One system line with no sender prefix (S2C 0x15). Replaces the blank
        "Server :" 0x16 lines of _send_chat_line (S2-05, B2)."""
        P.send(self, sock, session, '0x15', self.notice_fields(text, kind))

    def _hook_welcome(self, server, session, map_code=None, reason=None, **_):
        """`on_enter_world` hook (F5): one S2C 0x15 [Announce] line on the FIRST map load of
        a connection (chat_mail_gm-system-notices, D17, S3-02). It used to be a fake 0x0A
        whisper from "Server", which added "Server" to the whisper list and repeated on
        every portal; the 0x0A handler only writes the chat log, so nothing needs it."""
        if session.get('welcomed') or session.get('sock') is None:
            return
        session['welcomed'] = True
        self._notice(session['sock'], session, WELCOME_TEXT, 'announce')
        log.info(f'[ENTER_WORLD] 0x15 welcome notice (first entry, {reason})')

    # C2S opcode -> (popup entry, owner item) of the popup 0x50 requests nothing answers yet
    # (0x27 Make Party / 0x29 Break Party: the party handlers since P6 stage 3; 0x20 Trade:
    # the trade request since P7 stage 1). Empty until another entry needs a logging consumer.
    POPUP_REQUESTS = {}

    def _handle_player_popup(self, sock, session, rec, no_enc=False):
        """C2S 0x20 from the player popup window 0x50 (see the
        ROUTES comment): consumed with a log line naming the target - the uid the popup took
        from the clicked entity (+0x84, 2009 +0x88) or the name - and the in-world session it
        resolves to. No reply: none of them waits for one, and the refusals and results
        belong to the owner items (POPUP_REQUESTS)."""
        try:
            op = int(str(rec.key).rsplit('/', 1)[1], 16)        # '0x44A9D6/0x27' -> 0x27
        except (IndexError, ValueError):
            op = None
        entry, owner = self.POPUP_REQUESTS.get(op, ('?', '?'))
        uid = rec.get('target_uid')
        name = rec.get('target_name')
        target = None
        if uid:
            target = self.world.find(int(uid))
        elif name:
            target = self.world.find(P.cut_text(name, 16).decode('cp949', 'replace'))
        named = ' '.join(part for part in (
            f'uid {int(uid)}' if uid else '',
            f'name {P.cut_text(name, 16).decode("cp949", "replace")!r}' if name else '') if part) or 'no target'
        found = (f'-> {target.get("char_name")!r} on map {target.get("current_map")}'
                 if target is not None else '-> not in world')
        log.info(f'[POPUP] {session.get("char_name")!r}: {entry} ({named}) {found}; consumed '
                 f'(C2S 0x{op or 0:02X}, owner {owner})')

    def _chat_line(self, key, sessions, text, tag='CHAT'):
        """One pre-formatted line to one session or a list, each through _push: the
        receivers are usually OTHER players, whose closed socket must not raise into the
        sender's handler. Returns the number of receivers it was queued for."""
        if isinstance(sessions, dict):
            sessions = [sessions]
        return sum(1 for s in sessions if self._push(s, key, {'text': text}, tag))

    def _orange(self, sessions, text):
        """Pre-formatted orange line (S2C 0x90, text clamped to 87) to one session or a list:
        party chat (party.md F8) and megaphones (premium_cash.md F11)."""
        return self._chat_line('0x90', sessions, text)

    def _green(self, sessions, text):
        """Pre-formatted light-green line (S2C 0x91, text clamped to 87) to one session or a
        list: friend chat relay (chat_mail_gm F3)."""
        return self._chat_line('0x91', sessions, text, 'FRIEND')

    # ---- whisper (chat_mail_gm-whisper, F2; P6 stage 1) ----
    def _handle_whisper(self, sock, session, rec, no_enc=False):
        """C2S 0x02 WhisperChat {target_name, msg_len, message} (2008 0x445ADD/0x02, 2009
        0x4475C7/0x02, identical). The client prints nothing of its own whisper, so every
        accepted request is answered to the sender with S2C 0x09 (chat.py):

          target in world          0x0A {0x65, sender, text} to the target ("<From: A> text")
                                   + 0x09 {0x65, target, text} to the sender ("<To: B> text")
          offline / unknown / at   0x09 {0x66, name} "<name>can not be found." (a GM in
          select / GM in shadow    shadow is invisible to a non-GM, presence.visible_to)
          target refuses whispers  0x09 {0x67, target} "<target>is rejecting whispers." - the
                                   privacy flag (privacy.refuses 'whisper'); a GM is exempt
          target is the sender     0x15 [Warning] (a case variant of the own name; the client
                                   blocks the exact one itself)

        The target is found on ANY map (world.find: in-world, case-insensitive); the text is
        clamped once for both packets (chat.whisper_limit). Dropped with no reply, as the
        client does itself: an empty name or text (the whisper-tab path can send zeros), a
        sender not in world, sender manner <= -20 (the client's own whisper gate, mirrored
        so a GM mute sticks against a patched client), more than 10 whispers in 5 s."""
        target_name, text = chatmod.whisper_request(rec)
        name = session.get('char_name')
        wanted = target_name.decode('cp949', 'replace')
        if not target_name or not text or not name or not session.get('in_world'):
            log.info(f'[WHISPER] dropped {len(text)}B whisper from {session.get("username")!r} to '
                     f'{wanted!r}: {"not in world" if not name or not session.get("in_world") else "empty"}')
            return
        manner = self.store.manner(session.get('username'))
        if manner <= gm.MANNER_WHISPER_LIMIT:
            log.info(f'[WHISPER] dropped whisper from {name!r}: manner {manner} <= {gm.MANNER_WHISPER_LIMIT}')
            return
        if not self._whisper_rate_ok(session):
            log.info(f'[WHISPER] dropped whisper from {name!r}: more than {chatmod.WHISPER_RATE_COUNT} '
                     f'in {chatmod.WHISPER_RATE_SECS:g} s')
            return
        target = self.world.find(wanted)
        if target is not None and not presence.visible_to(target, session):
            target = None
        if target is not None and (target is session or target.get('uid') == session.get('uid')):
            self._notice(sock, session, chatmod.SELF_WHISPER_TEXT, 'warn')
            log.info(f'[WHISPER] {name!r} -> {wanted!r}: that is the sender')
            return
        if target is not None and self.refuses(target, 'whisper', actor=session):
            P.send(self, sock, session, '0x09',
                   chatmod.whisper_failed(chatmod.WHISPER_REFUSED, target.get('char_name')))
            log.info(f'[WHISPER] {name!r} -> {target.get("char_name")!r}: refused (0x09 0x67)')
            return
        if target is not None:
            to_name = target.get('char_name')
            text = chatmod.whisper_text(name, to_name, text)
            if self._push(target, '0x0A', chatmod.whisper_received(name, text), 'WHISPER'):
                P.send(self, sock, session, '0x09', chatmod.whisper_echo(to_name, text))
                log.info(f'[WHISPER] {name} -> {to_name}: {_log_text(text, chatmod.WHISPER_TEXT_MAX)!r} '
                         f'(map {session.get("current_map")} -> {target.get("current_map")})')
                return
        # Not in world, or its connection closed between the lookup and the send.
        P.send(self, sock, session, '0x09', chatmod.whisper_failed(chatmod.WHISPER_NOT_FOUND, target_name))
        log.info(f'[WHISPER] {name!r} -> {wanted!r}: not found (0x09 0x66)')

    def _whisper_rate_ok(self, session, now=None):
        """chat_mail_gm F2 step 3.4: at most WHISPER_RATE_COUNT whispers per session in any
        WHISPER_RATE_SECS window (session['whisper_times']; own connection thread only)."""
        return self._rate_ok(session, 'whisper_times', chatmod.WHISPER_RATE_COUNT,
                             chatmod.WHISPER_RATE_SECS, now)

    @staticmethod
    def _rate_ok(session, key, count, secs, now=None):
        """True (and this event counted) while the session had fewer than `count` events
        in the sliding window `session[key]` over the last `secs` seconds."""
        now = time.monotonic() if now is None else now
        times = session.get(key)
        if times is None:
            times = session[key] = collections.deque()
        while times and now - times[0] >= secs:
            times.popleft()
        if len(times) >= count:
            return False
        times.append(now)
        return True

    # ---- friend chat (chat_mail_gm-friend-chat-relay, F3; P6 stage 1) ----
    def _handle_friend_chat(self, sock, session, rec, no_enc=False):
        """C2S 0x6B FriendChatMessage {recipient_count, (channel, friend_id) x n, msg_len,
        "<name> : text"} (2008 0x46FE14/0x6B, 2009 0x47ACE9/0x6B, identical) -> S2C 0x91
        (green, the colour of the sender's own local copy) to each recipient that is
        - the in-world session of a friend_id the client listed (world.find(uid)), and
        - a character in the sender's STORED friend list (chat.stored_friends; the client's
          list is never trusted - social_friend-persistence owns `char['friends']`).
        The line is re-prefixed with the real name when it does not start with it and
        clamped to 87 (chat.friend_line). Nothing goes back to the sender: the client
        already printed the line (spec 0x6B semantics; registry.NEVER_REPLY 0x6B).
        The client gates are mirrored (F1.1 steps 3 and 7): manner <= -40 and the chat rate
        limit (shared with map chat, as the client's 700 ms timer is) drop the line."""
        name = session.get('char_name')
        text = P.to_bytes(rec.get('message', b'')).split(b'\x00', 1)[0]
        if not text or not name or not session.get('in_world'):
            log.info(f'[FRIEND] dropped {len(text)}B friend line from {session.get("username")!r}: '
                     f'{"empty" if not text else "not in world"}')
            return
        manner = self.store.manner(session.get('username'))
        if manner <= gm.MANNER_CHAT_LIMIT:
            log.info(f'[FRIEND] dropped friend line from {name!r}: manner {manner} <= {gm.MANNER_CHAT_LIMIT}')
            return
        if not self._chat_rate_ok(session):
            log.info(f'[FRIEND] dropped friend line from {name!r}: chat rate limit')
            return
        with self.store.lock:           # social_friend's add/delete edit the list under it
            friends = chatmod.stored_friends(self._session_char(session))
        line = chatmod.friend_line(name, text)
        heard, skipped = [], []
        for uid in chatmod.friend_ids(rec):
            target = self.world.find(uid)
            if target is None or target is session or target.get('uid') == session.get('uid'):
                skipped.append(f'uid {uid} not in world')
                continue
            to_name = str(target.get('char_name') or '')
            if to_name.lower() not in friends:
                skipped.append(f'{to_name!r} is no stored friend')
                continue
            if self._green(target, line):
                heard.append(to_name)
        log.info(f'[FRIEND] {_log_text(line, chatmod.FRIEND_LINE_MAX)!r} -> {heard or "nobody"}'
                 + (f' (skipped: {"; ".join(skipped)})' if skipped else ''))

    # ---- messenger room display (chat_mail_gm-room-builders, F5 display half) ----
    # social_friend-chat-room (P6 stage 2) decides WHEN; these build the client-safe packets
    # (chat.py: subtypes, name order, clamps) and queue them on the target's outbox.
    # flush: passed to _push (the messenger queues under its lock: flush=False).
    def _room_joined(self, target, room_id, names, flush=None):
        """S2C 0x0F subtype 1 {room_id, names} to `target`, its own name first so a two-member
        room prints "<other> has entered." (spec correction C52). No names = room id only."""
        return self._push(target, '0x0F', chatmod.room_joined(room_id, names, receiver=target.get('char_name')),
                          'ROOM', flush=flush)

    def _room_refused(self, target, subtype, name=b'', flush=None):
        """S2C 0x0F failure popup (subtype 2..6 or 8, chat.ROOM_REFUSALS) to `target`."""
        return self._push(target, '0x0F', chatmod.room_refused(subtype, name), 'ROOM', flush=flush)

    def _room_line(self, target, sender_name, text, flush=None):
        """S2C 0x61 room line to `target` (a room member: the packet is read only while the
        client's room id is set). An empty text (after the NUL cut) is not sent."""
        fields, assume = chatmod.room_line(sender_name, text)
        if not fields['message']:
            return False
        return self._push(target, '0x61', fields, 'ROOM', assume=assume, flush=flush)

    # ---- the messenger (P6 stage 2: social_friend F1-F12; messenger.py owns the logic) ----
    def _handle_friend_list(self, sock, session, payload, no_enc=False):
        """C2S 0x2F -> S2C 0x0B (+0x7E, +0x78): the resync after every S2C 0x03 (F1)."""
        self.messenger.friend_list(sock, session)

    def _handle_friend_add(self, sock, session, rec, no_enc=False):
        """C2S 0x30 {target_uid, target_name} -> S2C 0x0D to the target / 0x0C refusal (F2)."""
        self.messenger.friend_add(sock, session, rec)

    def _handle_friend_reply(self, sock, session, rec, no_enc=False):
        """C2S 0x31 {requester_id, requester_name, accept} -> S2C 0x0C to both (F2)."""
        self.messenger.friend_reply(sock, session, rec)

    def _handle_friend_delete(self, sock, session, rec, no_enc=False):
        """C2S 0x32 {friend_id, friend_name} -> S2C 0x0E (F3, directed)."""
        self.messenger.friend_delete(sock, session, rec)

    def _handle_messenger_status(self, sock, session, rec, no_enc=False):
        """C2S 0x37 {status} -> S2C 0x60 to the watchers only (F4; NEVER_REPLY)."""
        self.messenger.set_status(session, rec)

    def _handle_room_invite(self, sock, session, rec, no_enc=False):
        """C2S 0x33 {target_uid, target_name} -> S2C 0x10 to the invitee / 0x0F refusal (F5)."""
        self.messenger.room_invite(sock, session, rec)

    def _handle_room_reply(self, sock, session, rec, no_enc=False):
        """C2S 0x34 {inviter_id, accept} -> S2C 0x0F to the members (F5)."""
        self.messenger.room_reply(sock, session, rec)

    def _handle_room_chat(self, sock, session, rec, no_enc=False):
        """C2S 0x35 {text_len, text} -> S2C 0x61 to every member, sender included (F5)."""
        self.messenger.room_chat(sock, session, rec)

    def _handle_room_leave(self, sock, session, payload, no_enc=False):
        """C2S 0x36 -> S2C 0x62 to the remaining members (F5)."""
        self.messenger.room_leave(session)

    def _handle_mentor_register(self, sock, session, rec, no_enc=False):
        """C2S 0x5C {mentor_name} -> S2C 0x7A (+ 0x7B to the mentor) (F6)."""
        self.messenger.mentor_register(sock, session, rec)

    def _handle_mentor_remove(self, sock, session, payload, no_enc=False):
        """C2S 0x50 -> S2C 0x7C to the mentor (F7)."""
        self.messenger.mentor_remove(session)

    def _handle_memo_delete(self, sock, session, payload, no_enc=False):
        """C2S 0x44 -> the memos shown since the last 0x03 are deleted (F9, chat F6.5)."""
        self.messenger.memo_delete(session)

    def _handle_friend_slot_expand(self, sock, session, payload, no_enc=False):
        """C2S 0x73 -> S2C 0x9D {1, capacity, gold} / {0} (F12)."""
        self.messenger.slot_expand(sock, session)

    # ---- parties (P6 stage 3: party.md F1-F8; party.py owns the logic) ----
    def _handle_party_invite(self, sock, session, rec, no_enc=False):
        """C2S 0x27 {target_uid} (popup 0x50 Make Party) -> S2C 0x4E to the invitee, or a
        0x15 / 0x50 / 0x56 refusal to the inviter (F1)."""
        self.party.invite(session, rec.get('target_uid', 0))

    def _handle_party_accept(self, sock, session, rec, no_enc=False):
        """C2S 0x28 {inviter_name} (window 0x71 Accept) -> S2C 0x4F both ways, or a 0x15 /
        0x53 / 0x56 refusal (F2)."""
        self.party.accept(session, rec.get('inviter_name', b''))

    def _handle_party_leave(self, sock, session, rec, no_enc=False):
        """C2S 0x29 (Break Party: always "the sender leaves") -> S2C 0x51 (F4)."""
        self.party.leave(session, 'leave')

    def _handle_party_chat(self, sock, session, rec, no_enc=False):
        """C2S 0x6A {text_len, "<name> : msg"} (/p or the party chat mode) -> S2C 0x90 to
        every member, sender included (F8; party.Parties.chat). The client's own gates are
        mirrored, as for friend chat: manner <= -40 and the chat rate limit (the client's
        700 ms anti-spam timer is shared by normal, party and friend chat) drop the line."""
        name = session.get('char_name')
        text = P.to_bytes(rec.get('text', b'')).split(b'\x00', 1)[0]
        if not text or not name or not session.get('in_world'):
            log.info(f'[PARTY] dropped {len(text)}B party line from {session.get("username")!r}: '
                     f'{"empty" if not text else "not in world"}')
            return
        manner = self.store.manner(session.get('username'))
        if manner <= gm.MANNER_CHAT_LIMIT:
            log.info(f'[PARTY] dropped party line from {name!r}: manner {manner} <= {gm.MANNER_CHAT_LIMIT}')
            return
        if not self._chat_rate_ok(session):
            log.info(f'[PARTY] dropped party line from {name!r}: chat rate limit')
            return
        self.party.chat(session, text)

    # ---- trade (P7 stage 1: trade.md 2.1-2.9; trade.py owns the logic) ----
    def _handle_trade_request(self, sock, session, rec, no_enc=False):
        """C2S 0x20 {target_uid} (popup 0x50 Trade) -> S2C 0x45 to the target (window 0x70),
        or 0x47 {4 refusing | 6 busy} / a 0x15 line to the requester (trade.md 2.1)."""
        self.trade.request(session, rec.get('target_uid', 0))

    def _handle_trade_accept(self, sock, session, rec, no_enc=False):
        """C2S 0x21 {requester_name} (window 0x70 OK) -> S2C 0x46 to both (2.2)."""
        self.trade.accept(session, rec.get('requester_name', b''))

    def _handle_trade_add(self, sock, session, rec, no_enc=False):
        """C2S 0x22 (either send site) -> S2C 0x4B to both, 0x4D, or a 0x15 line (2.4)."""
        item, qty, count, opts, extra = trademod.descriptor(rec)
        self.trade.add(session, item, qty, opts, extra, count)

    def _handle_trade_remove(self, sock, session, rec, no_enc=False):
        """C2S 0x26 -> S2C 0x4C {index} to the partner (2.5)."""
        item, qty, _count, opts, extra = trademod.descriptor(rec)
        self.trade.remove(session, item, qty, opts, extra, rec.get('trade_slot_index'))

    def _handle_trade_lock(self, sock, session, rec, no_enc=False):
        """C2S 0x23 {u64 gold} -> S2C 0x48 {uid, gold} to both, or 0x49 both (2.6)."""
        self.trade.lock_offer(session, rec.get('gold', 0))

    def _handle_trade_cancel(self, sock, session, rec, no_enc=False):
        """C2S 0x24 -> S2C 0x49 to both, or to the sender alone with no trade (2.8)."""
        self.trade.cancel_request(session)

    def _handle_trade_confirm(self, sock, session, rec, no_enc=False):
        """C2S 0x25 {the whole deal} -> nothing (first), S2C 0x4A to both (commit) or 0x49 to
        both (echo mismatch / commit refused) (2.7)."""
        self.trade.confirm(session, rec)

    # ---- reputation (P7 stage 3: social_friend-compliment, chat_mail_gm-report) ----
    def _handle_compliment(self, sock, session, rec, no_enc=False):
        """C2S 0x6D {target_id, target_name} (popup 0x50 Praise -> window 0x435 confirm) ->
        S2C 0x94 {1, name} + 0x96 to the target + 0x97 to its holders, or 0x94 {2|5|6|7}."""
        self.reputation.compliment(sock, session, rec)

    def _handle_report(self, sock, session, rec, no_enc=False):
        """C2S 0x6E {target_uid, target_name, report_fee, category, content_len, content}
        (popup 0x50 Report / the HUD Report button -> window 0x434) -> the fee paid,
        reports.jsonl, S2C 0x95 {1, gold} and a GM alert, or 0x95 {2|7}."""
        self.reputation.report(sock, session, rec)

    # ---- remote player info (lc-player-info, login_character F9; P6 stage 1) ----
    def _handle_player_info(self, sock, session, rec, no_enc=False):
        """C2S 0x2A CharacterInfoRequest {target_name} (2008 0x4484CC/0x2A, 2009 0x44A9D6/0x2A,
        identical): the popup's Char. Info on a player the client does not hold (another map,
        a party frame). The client waits for nothing.

          online (entered the world, any map)  S2C 0x52: window 0x72 "Player Info." with the
                                               target's record (records.player_info)
          offline / unknown / GM in shadow     S2C 0x53 "<name>is not in server."

        A player on the requester's map never reaches here: the client fills window 0x72
        from its own entity, i.e. from the 0x04/0x05 record, which carries the same values
        (karma = manner, level, rank_icon = fame rank, class, stats, grid)."""
        if not session.get('in_world'):
            log.info(f'[INFO] Char. Info request from {session.get("username")!r} not in world - dropped')
            return
        wanted = chatmod.name_text(rec.get('target_name', b''))
        target = self.world.find(wanted, in_world=False) if wanted else None
        if target is not None and (target.get('kicked') or target.get('closed') or not target.get('char_name')
                                   or not presence.visible_to(target, session)):
            target = None
        char = self._session_char(target) if target is not None else None
        if char is None:
            P.send(self, sock, session, '0x53', {'char_name': chatmod.name_bytes(wanted)})
            log.info(f'[INFO] {session.get("char_name")!r}: Char. Info of {wanted!r} -> 0x53 not in server')
            return
        fields = R.player_info(target, char, self._session_account(target), self.client_build)
        P.send(self, sock, session, '0x52', fields)
        worn = sum(1 for e in fields['repeat[equip_count]'] if e['item_id'])
        log.info(f'[INFO] {session.get("char_name")!r}: Char. Info of {char.get("name")!r} -> 0x52 '
                 f'(Lv.{fields["level"]}, class {fields["job1"]}/{fields["job2"]}, rank {fields["rank"]}, '
                 f'manner {fields["manner_points"]}, {worn} item(s))')

    # ========================================================================
    # GM identity, C2S 0x06 commands and the '!' dev commands
    # (chat_mail_gm-gm-flag-manner / -gm-dispatch / -dev-commands; F10 / F11)
    # ========================================================================
    @property
    def gm_audit_path(self):
        """server/gm_audit.log, next to the accounts file (F10.0 step 5). Offline tests run
        on a temp accounts.json, so their audit lines land in the temp directory."""
        return gm.audit_path(self.store.path)

    def _gm_audit(self, session, action, detail=''):
        """Append one accepted GM action and log it. Only accepted commands are audited;
        a refused one is a warning in the server log."""
        actor = f'{session.get("username") or "?"}/{session.get("char_name") or "-"}'
        gm.audit(self.gm_audit_path, actor, action, detail)
        log.info(f'[GM] {actor}: {action} {detail}'.rstrip())

    def _gm_reply(self, session, text, kind='info'):
        """One S2C 0x15 line back to the GM who typed the command."""
        sock = session.get('sock')
        if sock is not None:
            self._notice(sock, session, text, kind)

    # ---- session sets and slots ----
    def _logged_in_sessions(self):
        """Every session that passed login, in slot order (character select included)."""
        sessions = [s for s in list(self.sessions.values())
                    if s.get('username') and s.get('sock') is not None and not s.get('kicked')]
        return sorted(sessions, key=self._slot_of)

    def _in_world_sessions(self):
        """Sessions whose own 0x07 is out: the audience of a broadcast (F5 `in_world`)."""
        return self.world.in_world_sessions()

    def _slot_of(self, session):
        """A stable per-connection slot number (chat_mail_gm 3.2). `!who` prints it and
        `/kick N` takes it, because the client's /kick sends a u8 number, not a name."""
        with self._slot_lock:
            slot = session.get('slot')
            if slot is None:
                self._next_slot += 1
                slot = session['slot'] = self._next_slot
        return slot

    def _session_by_slot(self, slot):
        for s in self._logged_in_sessions():
            if s.get('slot') == slot:
                return s
        return None

    def _target_session(self, name):
        """The online session of a character name, else of an account name (both
        case-insensitive). Used by /kick, !kick and !gm to find a live client."""
        found = self.world.by_char_name(name)          # World's own leaf lock
        if found is not None:
            return found
        wanted = str(name).lower()
        for s in self._logged_in_sessions():
            if str(s.get('username') or '').lower() == wanted:
                return s
        return None

    # ---- broadcasts and moderation ----
    def _announce(self, text, kind='announce'):
        """S2C 0x15 to every in-world session (F10 sub 0x01 /not). Returns the number of
        clients that got the line. A closed socket is skipped, never raised."""
        sent = 0
        for s in self._in_world_sessions():
            try:
                self._notice(s['sock'], s, text, kind)
                sent += 1
            except OSError as e:                          # noqa: PERF203 - one dead peer must not stop the rest
                log.warning(f'[GM] announce to {s.get("char_name")!r} failed: {e}')
        return sent

    def _adjust_manner(self, session, target_name, delta):
        """F10 sub 0x0A / F11 `!manner`: move the TARGET ACCOUNT's manner by `delta`,
        persist it, and tell the target's client.

        Manner is per account (D1/Q9: S2C 0x02 carries it before character select), so the
        penalty follows every character of that account. The target's client applies the
        delta itself from S2C 0x97 {uid, manner_delta} (spec 0x97: entity+0x15D8 and, for
        the local uid, the Status window's Manner) and re-colours the name tag.
        chat_mail_gm-gm-manner (P6 stage 4): uids are unique per account now (lc-uid-online),
        so the same 0x97 also goes to every client that HOLDS the target (presence.to_holders:
        its copy's +0x15D8 - the red name tag and the Manner line of their Player Info window
        come from it) - never to a client without the entity, where the uid could be nobody.
        touch() makes a spawn record in flight rebuild with the new karma. Everyone else gets
        the absolute value from the next 0x02 / 0x04 / 0x05 / 0x07 karma or 0x52.
        Returns (canonical character name, new value), or None when no such character
        exists."""
        found = self.store.character_by_name(target_name)
        if found is None:
            # Pink floating notice "<name>is not in server." (live verify 0x53).
            self._gm_packet(session, '0x53', {'char_name': names.clean(target_name)})
            log.info(f'[GM] manner: no character {target_name!r} in the store (0x53 sent)')
            return None
        username, _acc, char = found
        value = self.store.adjust_manner(username, delta)
        target = self._target_session(char['name'])
        seen = 0
        if delta and target is not None and worldmod.reachable(target):
            fields = {'uid': P.session_uid(target) or 0, 'manner_delta': delta}
            self._push(target, '0x97', fields, 'GM')
            seen = presence.to_holders(self, target, '0x97', fields, 'GM')
        self._gm_reply(session, f'Manner {char["name"]}: {delta:+d} (now {value})')
        log.info(f'[GM] manner {char["name"]} ({username}) {delta:+d} -> {value}'
                 + (f': 0x97 to it and {seen} holder(s)' if target is not None and delta else ' (offline)'))
        return char['name'], value

    def _gm_packet(self, session, key, fields):
        sock = session.get('sock')
        if sock is not None:
            P.send(self, sock, session, key, fields)

    def _gm_kick(self, session, target, why):
        """Disconnect `target` (F10 sub 0x0C / F11 `!kick`): the kick dialog, then the socket
        (_disconnect_with_notice, KICK_CLOSE_SECS: the client closes itself about 5 s after
        the 0x5D, so the socket is dropped a second later instead of under its feet)."""
        if target is session:
            self._gm_reply(session, 'You cannot kick yourself.', 'warn')
            return False
        self._disconnect_with_notice(target, why, close_after=gm.KICK_CLOSE_SECS)
        self._gm_reply(session, f'Kicked {target.get("char_name") or target.get("username")}')
        return True

    def _disconnect_with_notice(self, target, why, *, reason=None, close_after=gm.KICK_CLOSE_SECS,
                                save=False):
        """chat_mail_gm-gm-stop-kick / quest_cards_misc-admin-kick-maintenance: S2C 0x5D
        {reason} - the modal kick dialog, after which the client exits by itself within ~5 s
        with no click (live quest_cards_misc#17) - then, `close_after` seconds later on the
        tick thread, the socket (_kick). reason None = the build's kick text (gm.kick_reason:
        2009 reason 4 "...shut down as requested by administrator.", 2008 reason 0
        "Connection to Server got disconnected"). NEVER an S2C 0x17 first: it latches the
        client's "disconnect handled" flag for good, and the 0x5D would then show nothing and
        never exit (live quest_cards_misc#18). save: write the world state now (the
        disconnect path saves again when the socket goes). Returns the reason sent."""
        reason = gm.kick_reason(self.client_build) if reason is None else int(reason) & 0xFF
        if save:
            try:
                self._save_world_state(target, reason='kick')
                self.store.mark_dirty(f'kick {target.get("username")}')
            except Exception:                               # noqa: BLE001 - the kick still goes out
                log.exception(f'[GM] kick: saving {target.get("char_name")!r} failed')
        if not self._push(target, '0x5D', {'reason': reason}, 'GM'):
            log.warning(f'[GM] kick: 0x5D to {target.get("char_name")!r} not sent (socket gone)')
        target['kick_pending'] = why
        self.ticks.call_later(close_after, self._kick, target, why, name='gm-kick')
        log.info(f'[GM] kick {target.get("username")!r}/{target.get("char_name")!r}: 0x5D reason {reason}, '
                 f'socket closed in {close_after:g} s ({why})')
        return reason

    # ---- /go (chat_mail_gm-gm-go, F10 sub 0x08; P6 stage 4) ----
    def _go_target(self, name):
        """The session /go may land next to: a character in the world (any map, a map load in
        flight counts - its current_map and arrival point are already set), not kicked or
        closed. A GM sees shadowed players too. None otherwise."""
        target = self.world.find(name, in_world=False) if name else None
        if target is None or target.get('kicked') or target.get('kick_pending') or target.get('closed') \
                or not target.get('char_name') or not target.get('entered_once') \
                or target.get('current_map') is None:
            return None
        return target

    def _go_point(self, target):
        """(map, x, y) next to `target`: its server position (the estimate on the floor,
        presence.floor_point), GO_OFFSET_PX to the side - the right, or the left where the
        map ends - ON the floor there. It used to be 30 px above it for the GM's own 0x07 to
        fall onto, but the 2009 client does not drop an idle local player: _map_transfer
        settles every arrival onto the floor now (livetest bug 5), and this is that point."""
        code = int(target['current_map'])
        tx, ty = presence.floor_point(target)
        x, y = presence.settle(code, tx + gm.GO_OFFSET_PX, ty, gm.GO_OFFSET_PX + presence.SLOPE_SLACK_PX)
        if abs(x - tx) < gm.GO_OFFSET_PX / 2:                   # clamped at the right edge
            x, y = presence.settle(code, tx - gm.GO_OFFSET_PX, ty, gm.GO_OFFSET_PX + presence.SLOPE_SLACK_PX)
        return code, x, y

    def _gm_go(self, session, name):
        """F10 sub 0x08 /go (and `!go`): the GM lands next to `name`.

        Target not in the world (offline, at character select, unknown) -> S2C 0x53 "<name>is
        not in server." to the GM. Otherwise the world group's MapTransfer takes the GM to the
        target's map at _go_point - also on the SAME map: the GM's own client can only be
        moved by a map load (0x2A / 0x9E are never sent about the receiver's own uid: its queue
        is never consumed), and a same-map load is a full reload (0x08 frees every entity
        whatever the code, live same-map revive), never a bare second 0x07 (duplicate-entity
        risk). The other clients see the GM leave (0x06) and arrive (0x05) through presence.
        Refused with a warning: oneself, a dead GM (the death dialog owns the revive), a PvP
        room map (rooms belong to the pvp group). Returns True when the GM was moved."""
        target = self._go_target(name)
        if target is None:
            self._gm_packet(session, '0x53', {'char_name': chatmod.name_bytes(name)})
            log.info(f'[GM] /go {name!r} by {session.get("char_name")!r}: not in the world (0x53)')
            return False
        if target is session or target.get('uid') == session.get('uid'):
            self._gm_reply(session, 'You are already there.', 'warn')
            return False
        if session.get('dead'):
            self._gm_reply(session, 'You cannot /go while dead.', 'warn')
            return False
        # premium_cash-presence (P8 stage 4): a player in the mall is hidden from his map and
        # comes back only through his own EXIT (mall.py "Presence"), so neither side of a /go
        # may be inside: the target's current_map is only his way back.
        shop = mallmod.mall_name(self.client_build)
        if session.get('in_cash_shop'):
            self._gm_reply(session, f'Not while you are in the {shop} (close it with EXIT).', 'warn')
            return False
        if target.get('in_cash_shop'):
            self._gm_reply(session, f'{target["char_name"]} is in the {shop}.', 'warn')
            log.info(f'[GM] /go {name!r} by {session.get("char_name")!r}: the target is in the {shop}')
            return False
        code, x, y = self._go_point(target)
        if code in hpmp.ROOM_MAPS:
            self._gm_reply(session, f'{target["char_name"]} is in a PvP room (map {code}).', 'warn')
            return False
        same = code == session.get('current_map')
        self._gm_reply(session, f'Going to {target["char_name"]} on map {code} ({x:.0f}, {y:.0f}).')
        log.info(f'[GM] /go {session.get("char_name")!r} -> {target["char_name"]!r} on map {code} at '
                 f'({x:.0f}, {y:.0f}){" (same map: reload in place)" if same else ""}')
        return self._map_transfer(session['sock'], session, code, x, y, reason='gm go',
                                  no_enc=session.get('no_enc', True))

    # ---- maintenance (chat_mail_gm-gm-stop-kick, quest_cards_misc-admin-kick-maintenance) ----
    def maintenance_state(self):
        """'off', 'locked' (logins refused, nobody being shut down) or 'countdown' (a /stop or
        admin shutdown is running)."""
        if self._maintenance:
            return 'countdown'
        return 'locked' if self.config.MAINTENANCE else 'off'

    def _start_maintenance(self, why, actor=None):
        """F10 sub 0x07 /stop N and the admin `shutdown` (P6 stage 4):

          1. the login lock: config MAINTENANCE on (shared with the version server), so a new
             login gets S2C 0x02 result 0x0E "Server is under maintenance" and a new launch
             version_code 0xEA61 (lc-login-errors);
          2. every in-world client: the S2C 0x15 announce line and S2C 0x3A (red "[Announcement]
             Server will be down in 3 minutes for the maintenance." + centre notice; the client
             closes itself ~170 s later, C26);
          3. MAINTENANCE_SAVE_SECS later every world state is saved and the store flushed,
             MAINTENANCE_CLOSE_SECS later every connection still open is closed (a character-
             select session got no 0x3A: its screen has no chat log to print it).
        A countdown already running is not restarted. Returns True when one started."""
        if self._maintenance:
            if actor is not None:
                self._gm_reply(actor, 'A maintenance shutdown is already running.', 'warn')
            return False
        self.config['MAINTENANCE'] = True
        sent = self._announce(gm.MAINTENANCE_NOTICE)
        warned = 0
        for s in self._in_world_sessions():
            warned += bool(self._push(s, '0x3A', {}, 'GM'))
        self._maintenance = {
            'why': why, 't': time.monotonic(),
            'save': self.ticks.call_later(gm.MAINTENANCE_SAVE_SECS, self._maintenance_save, name='maintenance-save'),
            'close': self.ticks.call_later(gm.MAINTENANCE_CLOSE_SECS, self._maintenance_close,
                                           name='maintenance-close'),
        }
        log.warning(f'[GM] MAINTENANCE started ({why}): logins locked (0x02 0x0E / 0xEA61), '
                    f'0x15 to {sent} and 0x3A to {warned} in-world client(s); save at '
                    f'+{gm.MAINTENANCE_SAVE_SECS:g} s, close at +{gm.MAINTENANCE_CLOSE_SECS:g} s')
        if actor is not None:
            self._gm_reply(actor, f'Maintenance shutdown started: {warned} client(s) warned, logins locked.')
        return True

    def _maintenance_save(self):
        """The countdown's save, before the clients' own exit (~170 s, C26): every in-world
        session's map / position / HP / buffs into its record, then the store to disk."""
        saved = 0
        for s in self._in_world_sessions():
            try:
                saved += bool(self._save_world_state(s, reason='maintenance'))
            except Exception:                               # noqa: BLE001 - save the others
                log.exception(f'[GM] maintenance save of {s.get("char_name")!r} failed')
        self.store.tick_flush()                             # a tick callback: never the ~3 s backoff
        self.bosses.tick_flush()                            # P13: the boss ledger too, same rule
        log.warning(f'[GM] MAINTENANCE: {saved} world state(s) saved, store flushed')
        return saved

    def _maintenance_close(self):
        """The end of the countdown: every connection still open is closed (the clients that
        got 0x3A normally left by themselves ~10 s ago). The login lock stays on until an
        admin lifts it (`{"maintenance": 0}` on the admin port, or `!maintenance off`)."""
        left = [s for s in list(self.sessions.values()) if s.get('sock') is not None and not s.get('kicked')]
        for s in left:
            self._kick(s, 'maintenance shutdown')
        self._maintenance = None
        log.warning(f'[GM] MAINTENANCE: {len(left)} connection(s) closed; logins stay locked')
        return len(left)

    def _lift_maintenance(self):
        """Admin / `!maintenance off`: cancel a countdown (clients that already got 0x3A
        still exit by themselves - nothing can cancel their timer 9) and unlock the logins."""
        pending = self._maintenance
        if pending:
            for key in ('save', 'close'):
                pending[key].cancel()
        self._maintenance = None
        self.config['MAINTENANCE'] = False
        log.warning('[GM] MAINTENANCE lifted' + (' (countdown cancelled)' if pending else ''))
        return bool(pending)

    def _reload_content(self):
        """F10 sub 0x00 (/업데이트): drop every cached content table so the next lookup
        re-reads it. accounts.json is NOT reloaded - live sessions hold references into it
        (chat_mail_gm F10 sub 0x00)."""
        EC.reload()
        return 'EN items, quests, cards, portals, gamedef (en_content caches)'

    # ---- C2S 0x06 ----
    def _handle_gm(self, sock, session, payload, no_enc=False):
        """C2S 0x06 GM command (chat_mail_gm.md 1.5, F10).

        One opcode, nine sub-commands, and the send site is chosen by payload[0] - the six
        0x06 grammars share the opcode byte and some share a length, so a generic
        packets.parse(0x06) can only guess (gm.SUBCOMMANDS holds the byte -> key map).

        A 0x06 from a session without the GM flag is logged and dropped (F10.0 step 4): the
        client only shows these commands to an entity whose 0x07 said gm_level 1, but a
        patched client can send the packet anyway. The client waits for nothing after a GM
        command, so a drop leaves it in no bad state.
        """
        who = f'{session.get("username") or "?"}/{session.get("char_name") or "-"}'
        if not payload:
            log.warning(f'[GM] empty C2S 0x06 from {who}: dropped')
            return
        sub = payload[0]
        table = gm.subcommands(self.client_build)       # 2009: its own send sites (gm.py)
        sub_spec = table.get(sub)
        if not session.get('gm'):
            log.warning(f'[GM] C2S 0x06 sub 0x{sub:02X} from NON-GM session {who} '
                        f'({len(payload)}B {payload.hex(" ")}): dropped (F10.0 step 4)')
            return
        if sub_spec is None:
            log.warning(f'[GM] unknown C2S 0x06 sub-command 0x{sub:02X} from {who}: '
                        f'{payload.hex(" ")} (known: {sorted(table)})')
            return
        try:
            rec = P.parse(sub_spec.key, payload, direction='C2S', allow_trailing=sub_spec.allow_trailing,
                          client_build=self.client_build)
        except P.PacketError as e:
            if not sub_spec.allow_trailing:
                log.warning(f'[GM] {sub_spec.typed} from {who} does not decode as {sub_spec.key}: {e}')
                return
            # /not only: its u8 length is the client's own and can wrap, and the text is
            # read from the payload anyway (F10 sub 0x01), so the line is still handled.
            log.info(f'[GM] {sub_spec.typed} from {who}: body does not match {sub_spec.key} ({e}); '
                     f'using the payload text')
            rec = P.Record()
        handler = getattr(self, f'_gm_sub_{sub_spec.name}', None)
        if handler is None:
            # Decoded and audited, but the item that acts on it is not in this phase.
            self._gm_audit(session, f'0x06/{sub_spec.name}', registry.format_fields(rec))
            self._gm_reply(session, f'{sub_spec.typed} is not implemented yet ({sub_spec.owner}).', 'warn')
            return
        handler(session, rec, payload)

    def _gm_sub_notice(self, session, rec, payload):
        """Sub 0x01 /not <text> -> S2C 0x15 "[Announce] <text>" to every in-world session."""
        text = gm.notice_text(payload)
        if not text:
            log.info(f'[GM] /not with no text from {session.get("char_name")!r}: dropped')
            return
        text = P.cut_text(text, gm.ANNOUNCE_TEXT_MAX)
        sent = self._announce(text)
        self._gm_audit(session, '0x06/notice', f'{_log_text(text, gm.ANNOUNCE_TEXT_MAX)!r} -> {sent} session(s)')

    def _gm_sub_update(self, session, rec, payload):
        """Sub 0x00 /업데이트: reload the content caches."""
        what = self._reload_content()
        self._gm_audit(session, '0x06/update', what)
        self._gm_reply(session, 'Content reloaded.')

    def _gm_sub_manner(self, session, rec, payload):
        """Sub 0x0A /manner <name> <delta>. The client sends nothing for delta 0 and
        uninitialised stack bytes for a non-numeric one, so the decoded i32 is used as is."""
        target = names.clean(rec.get('target_name', ''))
        delta = int(rec.get('manner_delta', 0))
        done = self._adjust_manner(session, target, delta)
        if done is not None:
            self._gm_audit(session, '0x06/manner', f'{done[0]} {delta:+d} -> {done[1]}')

    def _gm_sub_kick(self, session, rec, payload):
        """Sub 0x0C /kick N, where N is the slot number `!who` prints (chat_mail_gm.md 1.5:
        the client sends a NUMBER, not a name)."""
        slot = int(rec.get('arg', 0))
        target = self._session_by_slot(slot)
        if target is None:
            self._gm_reply(session, f'No session in slot {slot} (see !who).', 'warn')
            return
        if self._gm_kick(session, target, f'/kick {slot} by {session.get("char_name")!r}'):
            self._gm_audit(session, '0x06/kick', f'slot {slot} = {target.get("char_name")!r}')

    def _gm_sub_go(self, session, rec, payload):
        """Sub 0x08 /go <name> {str[17] name} (2008 0x444FC6, 2009 0x4467B6): land next to
        that character (chat_mail_gm-gm-go, _gm_go). The client sends a 17+ character token
        without its NUL; names.clean cuts it."""
        name = names.clean(rec.get('target_name', ''))
        if self._gm_go(session, name):
            self._gm_audit(session, '0x06/go', name)

    def _gm_sub_stop(self, session, rec, payload):
        """Sub 0x07 /stop N {u8 atol(N)} (chat_mail_gm-gm-stop-kick): the 3-minute maintenance
        shutdown (_start_maintenance). N == 0 is a typo (/stop x: atol gives 0) and does
        nothing but a usage line; the client text hardcodes 3 minutes, so N is only logged."""
        n = int(rec.get('arg', 0))
        if not n:
            self._gm_reply(session, '/stop needs a number: /stop <1-255> (3-minute maintenance).', 'warn')
            return
        if self._start_maintenance(f'/stop {n} by {session.get("char_name")!r}', actor=session):
            self._gm_audit(session, '0x06/stop', f'N={n}')

    def _gm_sub_additem(self, session, rec, payload):
        """2009 sub 0x09 /additem <id> <n> {u16 item_id, u16 count} (spec_2009 0x44691E/0x06):
        the client's own GM item grant, answered with S2C 0x99 sub 9 (grant_item: the gold
        label is not rewritten, quest_cards_misc-system-notice-0x99). The client sends it even
        with missing or non-numeric tokens (zeros), so 0 / unknown ids get the usage line."""
        item, count = int(rec.get('item_id', 0)), int(rec.get('count', 0))
        if not item or not count:
            self._gm_reply(session, f'/additem {item} {count}: needs an item id and a count - '
                                    f'/additem <item_id> <count>', 'warn')
            return
        ok, why = self.grant_item(session, item, count, '/additem')
        if not ok:
            self._gm_reply(session, f'/additem {item} {count}: {why}', 'warn')
            return
        self._gm_audit(session, '0x06/additem', f'{item} x{count}')
        self._gm_reply(session, f'Granted {count} x {EC.item_name(item)} ({item}).')

    # ---- '!' dev commands (F11) ----
    def _gm_chat_command(self, sock, session, text):
        """Run one '!' line from a GM session. Never broadcast; the GM always gets an
        S2C 0x15 line back - the command's own answer, or its usage when an argument is
        missing or unusable - so a command is never typed into silence."""
        name, args = gm.split(text)
        cmd = gm.lookup(name, self.DEV_COMMANDS, gm.COMMANDS)
        if cmd is None:
            self._gm_reply(session, f'Unknown command !{name} (try !help).', 'warn')
            log.info(f'[GM] unknown dev command {name!r} from {session.get("char_name")!r}')
            return
        try:
            getattr(self, cmd.handler)(session, args)
        except gm.DevCommandError as e:
            self._gm_reply(session, f'{e} - {cmd.usage}', 'warn')
            # logged too, so a refused command never leaves the server log silent (polish
            # 2026-09-28: a bare `!hp` showed nothing in the log)
            log.info(f'[GM] !{name}{" " + args if args else ""} from {session.get("char_name")!r} refused: {e}')
        else:
            self._gm_audit(session, f'!{name}', args)

    # '!' commands every PLAYER may type, not only a GM (never broadcast either). `!mall` is
    # the only way into the Item Mall / Spark Shop: the client has no request for it - its HUD
    # button, the Luxary "Shop Assistant" NPC and the "Enter the cash shop?" prompt all end in
    # a no-op (mall.py "How the client enters the mall", RE 2026-09-28).
    PLAYER_COMMANDS = frozenset({'mall'})

    def _player_command(self, session, text):
        """Run a PLAYER_COMMANDS line of a non-GM session: the same handler a GM reaches, no
        audit line (gm_audit.log records GM actions), a refusal as one 0x15 line."""
        name, args = gm.split(text)
        cmd = self.DEV_COMMANDS[name]
        try:
            getattr(self, cmd.handler)(session, args)
        except gm.DevCommandError as e:
            self._gm_reply(session, f'{e} - {cmd.usage}', 'warn')
        log.info(f'[CHAT] player command !{name} {args} from {session.get("char_name")!r}'.rstrip())

    # name -> DevCommand. Other groups add theirs with gm.register() instead of editing
    # this table (F11: login_character's !job, premium_cash's !mall / !cash).
    DEV_COMMANDS = {
        'help': gm.DevCommand('_dev_help', '!help [command]', 'list the dev commands'),
        'who': gm.DevCommand('_dev_who', '!who', 'online sessions with their /kick slot numbers'),
        'notice': gm.DevCommand('_dev_notice', '!notice <text>', 'announce to every player',
                                aliases=('not',)),
        'kick': gm.DevCommand('_dev_kick', '!kick <name|slot>', 'disconnect a session'),
        'go': gm.DevCommand('_dev_go', '!go <name>', 'land next to a character (the /go path)',
                            owner='chat_mail_gm-gm-go'),
        'stop': gm.DevCommand('_dev_stop', '!stop <n>',
                              'the /stop path: 3-minute maintenance shutdown of every client',
                              owner='chat_mail_gm-gm-stop-kick'),
        'maintenance': gm.DevCommand('_dev_maintenance', '!maintenance [off|lock]',
                                     'the maintenance state; off lifts the login lock and a countdown, '
                                     'lock refuses new logins only', owner='chat_mail_gm-gm-stop-kick'),
        'keyframe': gm.DevCommand('_dev_keyframe', '!keyframe [name] [stop]',
                                  "re-sync the other clients' copy of a player to the server's point "
                                  '(S2C 0x2A; stop: 0x9E)', owner='world-keyframe-2a'),
        'manner': gm.DevCommand('_dev_manner', '!manner <name> <n>', 'add n to an account manner'),
        'gm': gm.DevCommand('_dev_gm', '!gm <name> [0|1]', 'set the GM flag of a character'),
        'mail': gm.DevCommand('_dev_mail', '!mail <name> <text>',
                              'store a memo for a character (delivered now if online)'),
        'msgr': gm.DevCommand('_dev_msgr', '!msgr [name]',
                              "the server's messenger view: friends, mentor, memos, status, room",
                              owner='social_friend (P6 stage 2)'),
        'party': gm.DevCommand('_dev_party', '!party [invite|accept <name> | say <text> | leave]',
                               "the server's party view; invite / accept / say / leave run the "
                               'C2S 0x27 / 0x28 / 0x6A / 0x29 paths', owner='party (P6 stage 3)'),
        'trade': gm.DevCommand('_dev_trade',
                               '!trade [all | request|accept <name> | offer <item> [n] | lock <gold> '
                               '| confirm | cancel]',
                               "the server's trade view (all: every live trade); the rest run the "
                               'C2S 0x20 / 0x21 / 0x22 / 0x23 / 0x25 / 0x24 paths',
                               owner='trade (P7 stage 1)'),
        'stall': gm.DevCommand('_dev_stall', '!stall [all]',
                               "the server's view of your flea-market stall (title, entries, "
                               'viewers); all: every open stall', owner='shop_storage stalls (P7 stage 2)'),
        'rep': gm.DevCommand('_dev_rep', '!rep [name] | reset [name] | praise <name> | report <name> [0-6] <text>',
                             "a player's manner and today's compliment / report limits; reset clears "
                             'the limits; praise / report run the C2S 0x6D / 0x6E paths',
                             owner='social_friend-compliment / chat_mail_gm-report (P7 stage 3)'),
        'reports': gm.DevCommand('_dev_reports', '!reports [n]', 'the last n player reports (reports.jsonl)',
                                 owner='chat_mail_gm-report (P7 stage 3)'),
        'note': gm.DevCommand('_dev_note', '!note [count] [1894|3320]',
                              'put Notes in your cash bag (a cash inventory record, S2C 0x6F)',
                              owner='social_friend-memos / premium_cash-cash-inventory-api'),
        'cash': gm.DevCommand('_dev_cash', '!cash [<n|+n|-n> | mileage <n|+n|-n> | item <item_id> [count] '
                                           '| box <item_id> [count] [name] | active | expire <serial|item_id> [secs]]',
                              'your Wind Cash / Mileage (S2C 0x70) and owned cash items; item: '
                              'put a Type 5 cash item (or a 2009 pet, bound) in your cash bag (S2C 0x6F; '
                              '<= 70 records); box: an EVENT record (origin 3) into your / that '
                              "character's account box, S2C 0x6C to an online owner (C7); "
                              'active: your activated period items and their effects; expire: let '
                              'an activated period item run out in secs (0 = now: S2C 0x93 at once)',
                              owner='premium_cash-wallet-model (P8 stage 1) / premium_cash-expiry (stage 3)'),
        'mall': gm.DevCommand('_dev_mall', '!mall [status]',
                              'enter the Item Mall / Spark Shop (any player; the client has no entry '
                              'request): S2C 0x6F, 0x6D, 0x6A. status: your wallet, box and gifts',
                              owner='premium_cash-mall-enter (P8 stage 2)'),
        'mileage': gm.DevCommand('_dev_mileage', '!mileage [<n> [name|all] | event <pct|off|config>]',
                                 'a mileage event: credit n event Mileage (S2C 0x70 + the 0x98 "Mileage '
                                 "Event\" line) to you, a character's account or every online account; "
                                 'event: the purchase event rate (% of each Wind Cash price, '
                                 'MILEAGE_EVENT_PCT) until a restart; no argument: both',
                                 owner='premium_cash-mileage-event (P8 stage 4)'),
        'gift': gm.DevCommand('_dev_gift', '!gift <name> <item_id> <msg>',
                              "send a free gift: a record in the account's mall box + its 0x6D popup",
                              owner='premium_cash-gift / chat_mail_gm-gift-inbox (P8 stage 2)'),
        'level': gm.DevCommand('_dev_level', '!level <1-99>', 'set your own level through exp'),
        'exp': gm.DevCommand('_dev_exp', '!exp <n|+n|-n>', 'set or add your own exp'),
        'give': gm.DevCommand('_dev_give', '!give <item_id> [count]', 'give yourself an EN item'),
        'socket': gm.DevCommand('_dev_socket', '!socket <equip_id> <stone_id> [stone_id ...]',
                                'put an equipment with these socket stones (1..5) in your bag; your map '
                                'reloads in place so the client lists it (S2C 0x03)',
                                owner='item_inventory-stone-extraction (P8 stage 4 test aid)'),
        'warp': gm.DevCommand('_dev_warp', '!warp <map> [x] [y]',
                              'load another map at a point (none: where a portal into it lands); '
                              'a GM can also type /warp'),
        'hp': gm.DevCommand('_dev_hp', '!hp [n|+n|-n]', 'set or change your current HP (1..max); '
                            'no value fills it', owner='cs-hp-mp-model'),
        'mp': gm.DevCommand('_dev_mp', '!mp [n|+n|-n]', 'set or change your current MP (0..max); '
                            'no value fills it', owner='cs-hp-mp-model'),
        'vitals': gm.DevCommand('_dev_vitals', '!vitals', 'your HP/MP, maxima and regen per tick',
                                owner='cs-hp-mp-model'),
        'learn': gm.DevCommand('_dev_learn', '!learn <skill_id> [force]',
                               'learn a skill (force skips the level/class gates)',
                               owner='cs-skill-learn'),
        'unlearn': gm.DevCommand('_dev_unlearn', '!unlearn <skill_id>',
                                 'forget a skill (the client drops it at the next map load)',
                                 owner='cs-skill-learn'),
        'skills': gm.DevCommand('_dev_skills', '!skills', 'your learned skills and running buffs',
                                owner='cs-skill-learn'),
        'damage': gm.DevCommand('_dev_damage', '!damage <n>',
                                'take n damage through the server model (0 HP = death dialog)',
                                owner='cs-player-death', aliases=('dmg', 'hurt')),
        'die': gm.DevCommand('_dev_die', '!die', 'take all your HP as damage (death dialog)',
                             owner='cs-player-death'),
        'revive': gm.DevCommand('_dev_revive', '!revive',
                                'revive at the revive point without the dialog', owner='cs-player-death'),
        'trap': gm.DevCommand('_dev_trap', '!trap [monster uid]',
                              'fire your placed Booby Trap on the nearest (or given) monster',
                              owner='cs-traps'),
        'job': gm.DevCommand('_dev_job', '!job <job item id>|novice',
                             'change class with a job item (180, 203-207, 3076-3088)',
                             owner='lc-class-change'),
        'gold': gm.DevCommand('_dev_gold', '!gold <n|+n|-n>', 'set or change your own gold',
                              owner='world-village-transfer'),
        'bank': gm.DevCommand('_dev_bank', '!bank (or /bank)',
                              'open your bank window anywhere (0x65, 0x80 {1, 0x1A7}, 0x65)',
                              owner='shop_storage-bank-fallback'),
        'loot': gm.DevCommand('_dev_loot', '!loot <item_id> [count]',
                              'drop an EN item on the ground at your feet (pickup key W)',
                              owner='item_inventory-ground-loot-pickup'),
        'grant': gm.DevCommand('_dev_grant', '!grant <item_id> [count]',
                               'give yourself a bag item with S2C 0x99 sub 9 (gold untouched)',
                               owner='quest_cards_misc-system-notice-0x99'),
        'deck': gm.DevCommand('_dev_deck', '!deck [clear]',
                              'list your Card Deck (clear: empty it and re-send the S2C 0x8A list)',
                              owner='quest_cards_misc-card-register'),
        'where': gm.DevCommand('_dev_where', '!where [all]',
                               "the server's position estimate of each player on your map (all: "
                               'every map) and who each client holds (spike S-1, decision G1)',
                               owner='world-position-estimate'),
        'mobs': gm.DevCommand('_dev_mobs', '!mobs',
                              'the shared monsters of your map: uid, HP, position, chase target, '
                              'how many clients hold each', owner='world-shared-monsters'),
    }

    def _dev_help(self, session, args):
        name = gm.split(args)[0] if args else ''
        if name:
            cmd = gm.lookup(name, self.DEV_COMMANDS, gm.COMMANDS)
            if cmd is None:
                raise gm.DevCommandError(f'no command !{name}')
            self._gm_reply(session, f'{cmd.usage} - {cmd.help}')
            return
        for chunk in gm.wrap_list(sorted({**gm.COMMANDS, **self.DEV_COMMANDS}), 'Commands: '):
            self._gm_reply(session, chunk)

    def _dev_who(self, session, args):
        """`slot: name map` per session (F11), plus the account and its uid. The slot is
        what /kick N takes. `c:N` is the client exe by its P2P port (party-test-harness:
        c:1 WindSlayer_patched.exe, c:2 WindSlayer_p2.exe; `wsdev send ... --to c:2`)."""
        sessions = self._logged_in_sessions()
        self._gm_reply(session, f'{len(sessions)} session(s) online:')
        for s in sessions:
            where = f'map {s.get("current_map")}' if s.get('in_world') else 'select'
            client = worldmod.client_number(s)
            self._gm_reply(session, f'{self._slot_of(s)}: {s.get("char_name") or "-"} {where} '
                                    f'[{s.get("username")} uid {P.session_uid(s)}]'
                                    + (f' c:{client}' if client else '')
                                    + (' GM' if s.get('gm') else ''))

    def _dev_where(self, session, args):
        """`!where [all]` (world-position-estimate, spike S-1): per player the estimate, the
        floor y a remote record would carry, the last fix and its age, the 0x0D count (with
        ae / interact tails) and - while the dev memory driver samples that client - the
        estimate's error against its memory (last / max / mean px), then the players its
        client holds. Compare with `wsview state` on both clients for decision G1."""
        everywhere = args.strip().lower() == 'all'
        mine = session.get('current_map')
        players = [s for s in self._in_world_sessions()
                   if everywhere or s.get('current_map') == mine]
        players.sort(key=lambda s: (s.get('current_map') or 0, s.get('uid') or 0))
        self._gm_reply(session, f'{len(players)} player(s)' + ('' if everywhere else f' on map {mine}') + ':')
        for s in players:
            for line in presence.describe(s):
                self._gm_reply(session, line)
            held = presence.spawned(s)
            names_held = sorted(str(h.get('char_name') or uid) for uid, h in held.items())
            self._gm_reply(session, f'  holds {len(held)}: {", ".join(names_held) or "-"}')

    def _dev_mobs(self, session, args):
        """`!mobs` (world-shared-monsters, P5 exit criterion 5): the monsters of your map, one
        line each - uid, name, HP, alive / corpse / gone, the server's x/y, the chase target
        and how many of the clients on the map hold it. `wsview state` on every client there
        must list exactly these uids."""
        mons = self._mob_home(session)
        if mons is None:
            raise gm.DevCommandError('not on a map (no monsters attached)')
        with self._combat(session):
            head = (f'map {mons.map_code}: {len(mons)} monster(s), {len(mons.viewers)} '
                    f'client(s) attached')
            lines = []
            for uid in sorted(mons):
                mob = mons[uid]
                held = sum(1 for h in mons.held.values() if uid in h)
                state = f'{mob.hp}/{mob.max_hp}' if mob.alive else ('gone' if mob.despawned else 'corpse')
                chase = f' -> uid {mob.aggro_uid}' if mob.aggro_uid else ''
                lines.append(f'{uid:#x} {mob.name} {state} ({mob.x:.0f},{mob.y:.0f}){chase} held {held}')
        self._gm_reply(session, head)
        for line in lines:
            self._gm_reply(session, line)

    def _dev_notice(self, session, args):
        if not args:
            raise gm.DevCommandError('no text')
        text = P.cut_text(args, gm.ANNOUNCE_TEXT_MAX)
        sent = self._announce(text)
        log.info(f'[GM] !notice -> {sent} session(s)')

    def _dev_kick(self, session, args):
        target = None
        if args.strip().isdigit():
            target = self._session_by_slot(int(args.strip()))
        if target is None:
            target = self._target_session(args.strip())
        if target is None:
            raise gm.DevCommandError(f'{args.strip()!r} is not online')
        self._gm_kick(session, target, f'!kick by {session.get("char_name")!r}')

    def _dev_go(self, session, args):
        if not args.strip():
            raise gm.DevCommandError('no character name')
        self._gm_go(session, args.split()[0])

    def _dev_stop(self, session, args):
        n = gm.parse_int(args or '0', 'n', lo=0, hi=0xFFFF) & 0xFF
        if not n:
            raise gm.DevCommandError('needs a number 1-255 (the client sends /stop N as a u8)')
        self._start_maintenance(f'!stop {n} by {session.get("char_name")!r}', actor=session)

    def _dev_maintenance(self, session, args):
        word = args.strip().lower()
        if word in ('off', '0', 'lift'):
            cancelled = self._lift_maintenance()
            self._gm_reply(session, 'Maintenance off: logins open' + (', countdown cancelled.' if cancelled else '.'))
            return
        if word in ('lock', 'on', '1'):
            self.config['MAINTENANCE'] = True
            log.warning(f'[GM] MAINTENANCE login lock on (!maintenance by {session.get("char_name")!r})')
        elif word:
            raise gm.DevCommandError(f'{word!r}: expected off or lock')
        state = self.maintenance_state()
        extra = ''
        if self._maintenance:
            extra = f' ({time.monotonic() - self._maintenance["t"]:.0f} s into the countdown)'
        self._gm_reply(session, f'Maintenance: {state}{extra}.')

    def _dev_keyframe(self, session, args):
        parts = args.split()
        stop = bool(parts) and parts[-1].lower() == 'stop'
        if stop:
            parts = parts[:-1]
        target = session if not parts else self.world.find(parts[0])
        if target is None:
            raise gm.DevCommandError(f'{parts[0]!r} is not in the world')
        if stop:
            sent = presence.send_stop(self, target)
            self._gm_reply(session, f'0x9E stop of {target["char_name"]} to {sent} client(s).')
            return
        x, y = presence.floor_point(target)
        sent = presence.send_keyframe(self, target, x, y)
        self._gm_reply(session, f'0x2A keyframe of {target["char_name"]} at ({x:.0f}, {y:.0f}) to {sent} client(s).')

    def _dev_manner(self, session, args):
        name, delta = gm.args(args, 2, self.DEV_COMMANDS['manner'].usage)
        self._adjust_manner(session, name, gm.parse_int(delta, 'n', lo=-0x80000000, hi=0x7FFFFFFF))

    def _dev_gm(self, session, args):
        parts = args.split()
        if not parts:
            raise gm.DevCommandError('no character name')
        level = gm.parse_int(parts[1], '0|1', lo=0, hi=0xFFFF) if len(parts) > 1 else gm.GM_LEVEL_ON
        found = self.store.set_gm(parts[0], level)
        if found is None:
            raise gm.DevCommandError(f'no character named {parts[0]!r}')
        username, char = found
        live = self._target_session(char['name'])
        if live is not None:
            # The '!' router reads session['gm'], so the flag works at once; the client's
            # own /commands need the gm_level in a fresh 0x07 (relog or a map change).
            live['gm'] = int(level)
        self._gm_reply(session, f'{char["name"]} ({username}) gm = {level}; '
                                f'the client sees it after the next map load or relog.')

    def _dev_mail(self, session, args):
        """Store a memo on a character (test aid for F6): the same store as a Note
        (messenger.store_memo) - kept until the recipient's memo window deletes it, delivered
        at once (S2C 0x78) when the recipient is online, else on its next C2S 0x2F."""
        name, text = gm.args(args, 2, self.DEV_COMMANDS['mail'].usage)
        if self.store.character_by_name(name) is None:
            raise gm.DevCommandError(f'no character named {name!r}')
        stored = self.messenger.store_memo(name, session.get('char_name') or 'GM', text)
        if stored is None:
            raise gm.DevCommandError('empty text')
        who, memo, delivered = stored
        with self.store.lock:
            total = len(self.store.character_by_name(who)[2].get('memos') or [])
        self._gm_reply(session, f'Memo {memo["id"]} stored for {who} ({total} total; '
                                f'{"delivered now" if delivered else "delivered at its next friend-list sync"}).')

    def _dev_msgr(self, session, args):
        """`!msgr [name]`: the server's messenger view of a character (yours by default) -
        friends with online state, capacity, mentor / mentees, memos, status and room."""
        target = session
        if args.strip():
            target = self.messenger.find(args.strip())
            if target is None:
                raise gm.DevCommandError(f'{args.strip()!r} is not online')
        self._gm_reply(session, f'{self.messenger.name_of(target)} (uid {P.session_uid(target)}):')
        for line in self.messenger.describe(target):
            self._gm_reply(session, line)

    def _dev_party(self, session, args):
        """`!party [invite <name> | accept <name> | say <text> | leave]` (party.py): with no
        argument the server's view of your party (members, HP/MP as the frames get them,
        your frame mirror, invites waiting for you); otherwise the same path the client's
        C2S 0x27 / 0x28 / 0x6A / 0x29 takes, for live checks without clicking (`wsdev dev
        <char> '!party invite Watcher'`). The GM gate and chat gates of 0x6A do not apply."""
        sub, _, rest = args.strip().partition(' ')
        sub, rest = sub.lower(), rest.strip()
        name = session.get('char_name')
        if not sub:
            self._gm_reply(session, f'{name} (uid {P.session_uid(session)}):')
            for line in self.party.describe(session):
                self._gm_reply(session, line)
            return
        if sub == 'invite':
            if not rest:
                raise gm.DevCommandError('no character name')
            target = self.world.find(rest)
            if target is None:
                raise gm.DevCommandError(f'{rest!r} is not online')
            result = self.party.invite(session, P.session_uid(target))
            self._gm_reply(session, f'Make Party on {target.get("char_name")}: {result}')
        elif sub == 'accept':
            if not rest:
                raise gm.DevCommandError('no inviter name')
            result = self.party.accept(session, rest)
            self._gm_reply(session, f'Accept {rest}: {result}')
        elif sub == 'say':
            if not rest:
                raise gm.DevCommandError('no text')
            heard = self.party.chat(session, rest.encode('cp949', 'replace'))
            self._gm_reply(session, f'Party chat reached {len(heard)} member(s).')
        elif sub == 'leave':
            left = self.party.leave(session, 'leave')
            self._gm_reply(session, 'Left the party.' if left else 'Not in a party (frames cleared).')
        else:
            raise gm.DevCommandError(f'unknown sub-command {sub!r}')

    def _dev_trade(self, session, args):
        """`!trade [all | request <name> | accept <name> | offer <item> [n] | lock <gold> |
        confirm | cancel]` (trade.py): with no argument the server's view of your trade (both
        sides' offers, gold, locked / final) and the prompt waiting for you; `all` is the admin
        dump of every live trade (trade-audit-log); the rest take the same path as the client's
        C2S 0x20 / 0x21 / 0x22 / 0x23 / 0x25 / 0x24, for live checks without clicking or
        dragging (`wsdev --build 2009 dev TestHero '!trade offer 179'`). `offer` takes the
        first bag instance not yet offered (equipment: its stored block); `confirm` echoes
        the server's own view of the deal, so it only proves the commit path."""
        sub, _, rest = args.strip().partition(' ')
        sub, rest = sub.lower(), rest.strip()
        name = session.get('char_name')
        if not sub:
            self._gm_reply(session, f'{name} (uid {P.session_uid(session)}):')
            for line in self.trade.describe(session):
                self._gm_reply(session, line)
            return
        if sub == 'all':
            for line in self.trade.describe_all():
                self._gm_reply(session, line)
            return
        if sub in ('request', 'accept'):
            if not rest:
                raise gm.DevCommandError('no character name')
            if sub == 'request':
                target = self.world.find(rest)
                if target is None:
                    raise gm.DevCommandError(f'{rest!r} is not online')
                result = self.trade.request(session, P.session_uid(target))
            else:
                result = self.trade.accept(session, rest)
            self._gm_reply(session, f'Trade {sub} {rest}: {result}')
        elif sub == 'offer':
            parts = rest.split()
            try:
                item = int(parts[0])
                count = int(parts[1]) if len(parts) > 1 else 1
            except (IndexError, ValueError):
                raise gm.DevCommandError('no item id / count') from None
            opts, extra = [], 0
            bag = self._bag(session)
            if bag is not None and bag.tab_of(item) == 'equip':
                count = 1
                offered = self.trade.offered(session, item)
                instances = [e for e in bag.slots('equip') if e['id'] == item]
                if len(instances) > offered:
                    words = instances[offered]['w']
                    opts, extra = invmod.wire_words(words), invmod.pack_words(words)[5]
            result = self.trade.add(session, item, count, opts, extra)
            self._gm_reply(session, f'Offer {EC.item_name(item) or item} x{count}: {result}')
        elif sub == 'lock':
            try:
                gold = int(rest or 0)
            except ValueError:
                raise gm.DevCommandError('gold must be a number') from None
            self._gm_reply(session, f'Lock {gold} gold: {self.trade.lock_offer(session, gold)}')
        elif sub == 'confirm':
            t = self.trade.trade_of(session)
            if t is None:
                raise gm.DevCommandError('no trade')
            mine, theirs = t.side(session), t.other(session)
            rec = {'my_gold': mine.gold, 'my_item_count': len(mine.offer),
                   'repeat[my_item_count]': [o.echo_row() for o in mine.offer],
                   'partner_gold': theirs.gold, 'partner_item_count': len(theirs.offer),
                   'repeat[partner_item_count]': [o.echo_row() for o in theirs.offer]}
            self._gm_reply(session, f'Confirm: {self.trade.confirm(session, rec)}')
        elif sub == 'cancel':
            self._gm_reply(session, f'Cancel: {self.trade.cancel_request(session)}')
        else:
            raise gm.DevCommandError(f'unknown sub-command {sub!r}')

    def _dev_stall(self, session, args):
        """`!stall [all]` (market.py): with no argument the server's view of your stall - title,
        map, age, viewers and every entry with its quantity and unit price - so a live check can
        compare it with window 0x259 after a sale; `all` lists every open stall (admin view)."""
        sub = args.strip().lower()
        if sub == 'all':
            lines = self.market.describe_all()
        elif not sub:
            lines = [f'{session.get("char_name")} (uid {P.session_uid(session)}):'] + self.market.describe(session)
        else:
            raise gm.DevCommandError(f'unknown sub-command {sub!r}')
        for line in lines:
            self._gm_reply(session, line)

    def _dev_rep(self, session, args):
        """`!rep [name]`: the account manner and today's compliment / report limits of a
        character (default: your own). `!rep reset [name]` clears those limits (a live
        re-test the same day). `!rep praise <name>` and `!rep report <name> [0-6] <text>` run
        the same paths as the client's C2S 0x6D / 0x6E (uid resolved from the name, the
        server's own fee), so `wsdev --build 2009 dev TestHero '!rep praise Watcher'` needs
        no clicking."""
        sub, _, rest = args.strip().partition(' ')
        low, rest = sub.lower(), rest.strip()
        if low in ('praise', 'report'):
            if not rest:
                raise gm.DevCommandError('needs a character name')
            name, _, text = rest.partition(' ')
            target = self.world.find(name)
            uid = P.session_uid(target) or 0 if target is not None else 0
            sock = session.get('sock')
            if low == 'praise':
                self.reputation.compliment(sock, session, {'target_id': uid, 'target_name': name})
                return
            cat, _, body = text.strip().partition(' ')
            if cat.isdigit():
                text = body
            else:
                cat = '0'
            if not text.strip():
                raise gm.DevCommandError('needs the report text')
            fee = repmod.report_fee(R.level_of(self._session_char(session)))
            content = text.strip().encode('cp949', 'replace')[:repmod.REPORT_TEXT_MAX]
            self.reputation.report(sock, session, {'target_uid': uid, 'target_name': name, 'report_fee': fee,
                                                   'category': int(cat), 'content_len': len(content),
                                                   'content': content})
            return
        reset = low == 'reset'
        who = rest if reset else sub
        if who:
            found = self.store.character_by_name(who)
            if found is None:
                raise gm.DevCommandError(f'no character {who!r}')
            username, name = found[0], found[2]['name']
        else:
            username, name = session.get('username'), session.get('char_name')
        if reset:
            self.reputation.reset(username)
            self._gm_reply(session, f'Compliment / report limits of {name} cleared.')
        for line in self.reputation.describe(username, name):
            self._gm_reply(session, line)

    def _dev_reports(self, session, args):
        """`!reports [n]`: the last n (default 5, at most 20) player reports from
        reports.jsonl, one line each: number, time, reporter > target, category, text."""
        n = gm.parse_int(args, 'n', lo=1, hi=20) if args.strip() else 5
        for line in self.reputation.report_lines(n):
            self._gm_reply(session, line)

    def _dev_note(self, session, args):
        """`!note [count] [1894|3320]`: put Notes into your cash inventory (P8 stage 1). The
        Note is a real limit_type 1 record (cash.CashInventory.grant: a store-wide serial,
        persisted, merged into a Note record you already have), then the whole owned list goes
        out as S2C 0x6F. Using one opens the note window 0x3FD; the 0x77 success of a sent
        note names this serial and the client's consume-by-serial takes one off (livetest
        2026-09-25: the P6 dev record had limit_type 0, which the client never decrements)."""
        parts = args.split()
        count = gm.parse_int(parts[0], 'count', lo=1, hi=99) if parts else 1
        item = gm.parse_int(parts[1], 'item', lo=1894, hi=3320) if len(parts) > 1 else 1894
        if item not in social.NOTE_ITEMS:
            raise gm.DevCommandError(f'{item} is not a Note (1894 or 3320)')
        rec = self._dev_cash_grant(session, item, count, '!note')
        self._gm_reply(session, f'{count} x Note {item} in your cash bag (serial {rec["serial"]:#x}, '
                                f'{rec["qty"]} in that record).')

    def _dev_cash_grant(self, session, item, count, what):
        """Grant a cash item record to the caller's character and re-send the owned list."""
        char = self._session_char(session) if session.get('char_name') else None
        if char is None:
            raise gm.DevCommandError('no character in this session')
        try:
            rec = self.cash.grant(char, item, count, origin=cashmod.ORIGIN_EVENT, what=what)
        except cashmod.CashError as e:
            raise gm.DevCommandError(str(e)) from None
        if session.get('in_world'):
            self._send_owned_cash(session.get('sock'), session, reason=what, force=True)
        return rec

    def _dev_cash(self, session, args):
        """`!cash`: your account's Wind Cash and Mileage and your owned cash items (P8 stage 1).
          !cash <n|+n|-n>            set / change Wind Cash
          !cash mileage <n|+n|-n>    set / change Mileage
          !cash item <id> [count]    put a cash item in your cash bag: Type 5 (costumes are
                                     refused, cash.GRANT_TYPES) or a 2009 pet (a bound pet
                                     record, kind 3: ROADMAP_2009_ADDENDUM C1), at most
                                     cash.OWNED_MAX (70) records - one 0x6F frame
          !cash box <id> [count] [name]  an event record (origin 3) into your / that character's
                                     ACCOUNT box: mall.Mall.grant_box (C7), S2C 0x6C with the
                                     balances unchanged to an owner in the world or the mall
        A balance change is persisted and sent as S2C 0x70 {cash, mileage, 0}: it writes the
        mall's labels (mall+0x588 / +0x58C) and clears its charge-pending flag, with no popup
        (bonus 0; live T-70 ran it in world), so an open mall shows the new value."""
        acc = self.store.account(session.get('username'))
        if acc is None:
            raise gm.DevCommandError('no account in this session')
        parts = args.split()
        if not parts:
            char = self._session_char(session) if session.get('char_name') else None
            for line in self.cash.describe(acc, char):
                self._gm_reply(session, line)
            return
        sub = parts[0].lower()
        if sub in ('active', 'use'):
            for line in self.cashuse.describe(session):
                self._gm_reply(session, line)
            return
        if sub == 'expire':
            self._dev_cash_expire(session, parts[1:])
            return
        if sub == 'box':
            self._dev_cash_box(session, parts[1:])
            return
        if sub in ('item', 'give'):
            if len(parts) < 2:
                raise gm.DevCommandError('no item id')
            item = gm.parse_int(parts[1], 'item_id', lo=1, hi=EC.item_max_id())
            count = gm.parse_int(parts[2], 'count', lo=1, hi=cashmod.STACK_MAX) if len(parts) > 2 else None
            rec = self._dev_cash_grant(session, item, count, '!cash item')
            self._gm_reply(session, f'{EC.item_name(item) or item} ({item}) in your cash bag: serial '
                                    f'{rec["serial"]:#x}, kind {rec["kind"]}, x{rec["qty"]}.')
            return
        if sub in ('mileage', 'mile'):
            field, text = 'mileage', (parts[1] if len(parts) > 1 else '')
        else:
            field, text = 'cash', parts[0]
        if not text:
            raise gm.DevCommandError('no value')
        value = gm.parse_int(text.lstrip('+'), field, lo=-cashmod.CASH_MAX, hi=cashmod.CASH_MAX)
        with self.store.lock:
            current = dict(zip(('cash', 'mileage'), cashmod.balance(acc)))[field]
            new = current + value if text[0] in '+-' else value
            cash, mileage = self.cash.set_balance(acc, **{field: new}, what=f'!cash {session.get("username")}')
        if session.get('sock') is not None:
            self._gm_packet(session, '0x70', cashmod.balance_fields(acc))
        self._gm_reply(session, f'Wind Cash {cash}, Mileage {mileage}.')

    def _dev_cash_box(self, session, parts):
        """`!cash box <item_id> [count] [name]` (ROADMAP_2009_ADDENDUM C7, T-E6): an event record
        (origin 3) into the caller's - or the named character's - account box through
        mall.Mall.grant_box: S2C 0x6C {1, cash, mileage, record} with the balances unchanged to
        an owner in the world or the mall ("Congratulation. You received an event item..")."""
        if not parts:
            raise gm.DevCommandError('no item id')
        item = gm.parse_int(parts[0], 'item_id', lo=1, hi=EC.item_max_id())
        rest = parts[1:]
        count = None
        if rest and rest[0].isdigit():
            count = gm.parse_int(rest[0], 'count', lo=1, hi=cashmod.STACK_MAX)
            rest = rest[1:]
        who = ' '.join(rest)
        if who:
            found = self.store.character_by_name(who)
            if found is None:
                raise gm.DevCommandError(f'no character named {who!r}')
            username = found[0]
        else:
            username = session.get('username')
        try:
            rec, told = self.mall.grant_box(username, item, count, reason=f'!cash box by {session.get("char_name")}')
        except cashmod.CashError as e:
            raise gm.DevCommandError(str(e)) from None
        self._gm_reply(session, f'{EC.item_name(item) or item} ({item}) x{rec["qty"]} in {username}\'s box as an event '
                                f'record (serial {rec["serial"]:#x}, origin 3){"; 0x6C sent" if told else ""}.')

    def _dev_cash_expire(self, session, parts):
        """`!cash expire <serial|item_id> [secs]` (premium_cash-expiry live check, exit criterion
        4): the activated period record named by serial (0x.. or a number above 0xFFFF) or item
        id now runs out in `secs` (default 0). 0 expires it at once through the same path the
        60 s ticker takes (S2C 0x93 "[x] is expired."); later ones wait for the ticker."""
        if not parts:
            raise gm.DevCommandError('no serial or item id')
        char = self._session_char(session) if session.get('char_name') else None
        if char is None:
            raise gm.DevCommandError('no character in this session')
        wanted = gm.parse_int(parts[0], 'serial|item_id', lo=1, hi=0xFFFFFFFF)
        secs = gm.parse_int(parts[1], 'secs', lo=0, hi=86400 * 90) if len(parts) > 1 else 0
        when = cashmod.local_now() + datetime.timedelta(seconds=secs)
        with self.store.lock:
            rec = next((r for r in cashmod.ensure(char) if cashmod.is_activated(r)
                        and (r['serial'] == wanted or r['item_id'] == wanted)), None)
            if rec is not None:
                rec['expire'] = cashmod.format_expire(when)
        if rec is None:
            raise gm.DevCommandError(f'no activated period item {parts[0]} (use one first; !cash active lists them)')
        self.store.mark_dirty(f'!cash expire {char.get("name")}')
        gone = self.cashuse.expire(session) if secs <= 0 else []
        self._gm_reply(session, f'{EC.item_name(rec["item_id"]) or rec["item_id"]} ({rec["serial"]:#x}) '
                                + ('expired now.' if gone else f'expires at {rec["expire"]} (the ticker checks every '
                                                                f'{cashusemod.EXPIRY_SCAN_SECS:.0f} s).'))

    def _dev_mall(self, session, args):
        """`!mall`: enter the Item Mall / Spark Shop (premium_cash-mall-enter, F2) - for every
        player (PLAYER_COMMANDS), since the client cannot ask. The answer is the mall itself
        (S2C 0x6F / 0x6D / 0x6A); a refusal is one 0x15 line. `!mall status` lists the wallet,
        the box and the pending gifts without entering."""
        if args.strip().lower() == 'status':
            for line in self.mall.describe(session):
                self._gm_reply(session, line)
            return
        if args.strip():
            raise gm.DevCommandError(f'unknown argument {args.strip()!r}')
        refusal = self.mall.enter(session)
        if refusal is not None:
            self._gm_reply(session, refusal, 'warn')

    def _dev_mileage(self, session, args):
        """`!mileage` (premium_cash-mileage-event, F16; P8 stage 4):
          !mileage                      the purchase event rate and your Mileage
          !mileage <n> [name|all]       credit n event Mileage to you / that character's
                                        account / every logged-in account: persisted, and an
                                        owner in the world or the mall gets S2C 0x70 then 0x98
                                        (the client's "※Mileage Event※ You got bonus mileage.")
          !mileage event <pct|off|config>  the purchase event: every Wind Cash buy / gift earns
                                        pct % of its price the same way (server memory; config
                                        brings MILEAGE_EVENT_PCT back)"""
        parts = args.split()
        mall = self.mall
        if not parts:
            acc = self.store.account(session.get('username'))
            cfg = max(0, int(self.config.get('MILEAGE_EVENT_PCT', 0)))
            self._gm_reply(session, f'Mileage event: purchases earn {mall.event_pct}% '
                                    f'(config {cfg}%{", overridden" if mall.event_pct_override is not None else ""}); '
                                    f'your Mileage {cashmod.balance(acc)[1] if acc else 0}.')
            return
        if parts[0].lower() == 'event':
            if len(parts) < 2:
                raise gm.DevCommandError('no rate')
            word = parts[1].lower()
            if word == 'config':
                mall.event_pct_override = None
            elif word == 'off':
                mall.event_pct_override = 0
            else:
                mall.event_pct_override = gm.parse_int(word.rstrip('%'), 'pct', lo=0, hi=cfgmod.MILEAGE_PCT_MAX)
            self._gm_reply(session, f'Mileage event: purchases earn {mall.event_pct}%'
                                    f'{" (config)" if mall.event_pct_override is None else ""}.')
            log.info(f'[MALL] {session.get("char_name")!r}: purchase mileage event {mall.event_pct}%')
            return
        amount = gm.parse_int(parts[0].lstrip('+'), 'n', lo=1, hi=cashmod.CASH_MAX)
        who = ' '.join(parts[1:])
        if not who:
            usernames = [session.get('username')]
        elif who.lower() == 'all':
            usernames = list(dict.fromkeys(s.get('username') for s in self.world.online() if s.get('username')))
        else:
            found = self.store.character_by_name(who)
            if found is None:
                raise gm.DevCommandError(f'no character named {who!r}')
            usernames = [found[0]]
        told = credited = 0
        for username in usernames:
            got, mileage, shown = mall.mileage_event(username, amount, f'!mileage by {session.get("char_name")}')
            credited += bool(got)
            told += bool(shown)
        if len(usernames) == 1:
            note = '' if shown else ' (at the cap)' if not got else ' (not in the world or the mall: not told)'
            self._gm_reply(session, f'{usernames[0]}: +{got} event Mileage -> {mileage}{note}.')
        else:
            self._gm_reply(session, f'+{amount} event Mileage to {credited} account(s), {told} told.')

    def _dev_gift(self, session, args):
        """`!gift <name> <item_id> <msg>`: the C2S 0x47 gift for free (test aid): a record in
        the recipient ACCOUNT's mall box with an inbox entry, pushed at once to an online
        recipient (0x79 in the mall, the 0x6D queue), else on his next map load. The item must
        be one the client's gift popup can show (a cash item or a gift card 3327..3332,
        chat_mail_gm F8.2)."""
        name, item_id, message = gm.args(args, 3, self.DEV_COMMANDS['gift'].usage)
        found = self.store.character_by_name(name)
        if found is None:
            raise gm.DevCommandError(f'no character named {name!r}')
        item = gm.parse_int(item_id, 'item_id', lo=1, hi=EC.item_max_id())
        if not self._grantable_item(item, '!gift'):
            raise gm.DevCommandError(f'item {item} is not in the EN client catalog')
        try:
            username, rec = self.mall.dev_gift(session.get('char_name') or 'GM', found, item, message)
        except cashmod.CashError as e:
            raise gm.DevCommandError(str(e)) from None
        pending = sum(1 for g in (self.store.account(username) or {}).get('gift_inbox') or []
                      if not g.get('delivered'))
        self._gm_reply(session, f'Gift {item} stored for {username} (box serial {rec["serial"]:#x}; '
                                f'{pending} undelivered).')

    @staticmethod
    def _level_exp(level, current):
        """(total exp, kept, lost) of `!level <level>` from `current` total exp: the level's
        floor plus the exp the character had into his current level, at most one short of
        the next level's floor (livetest bug 9: a `!level 15` / `!level 14` round trip took
        42903 down to 42203, the Lv14 floor). `lost` is the progress that did not fit."""
        cur_lv = progression.level_for_exp(current)
        progress = max(0, current - progression.exp_for_level(cur_lv))
        floor = progression.exp_for_level(level)
        room = (progression.exp_for_level(level + 1) - floor - 1) if level < progression.LEVEL_MAX else 0
        kept = min(progress, max(0, room))
        return floor + kept, kept, progress - kept

    def _dev_level(self, session, args):
        """Set the caller's level by granting the exp difference, so the client levels
        itself from S2C 0x21 exactly as a kill does (lc-exp-persist). The exp into the
        current level is kept (clamped to what the new level holds, _level_exp) and the reply
        says so. A level DOWN shows at once too: the owner's client lowers its own level from
        the negative 0x21 (live: 15 -> 14 showed 14), and grant_exp re-sends his record to
        the observers (0x06 + 0x05 with the lower level; 0x22 would play the level-up effect)."""
        level = gm.parse_int(args, 'level', lo=1, hi=progression.LEVEL_MAX)
        char = self._session_char(session)
        if char is None:
            raise gm.DevCommandError('no character in this session')
        self._refuse_dead(session)
        with self.store.lock:
            current = progression.clamp_exp(char.get('exp', 0))
        target, kept, lost = self._level_exp(level, current)
        self.grant_exp(session, target - current)
        note = ''
        if lost:
            note = f', {kept} of the {kept + lost} exp into the level kept (the most Lv{level} holds)'
        elif kept:
            note = f', {kept} exp into the level kept'
        self._gm_reply(session, f'Level {progression.level_for_exp(target)} (exp {target}{note}).')

    def _dev_exp(self, session, args):
        """`!exp 500` sets the total, `!exp +500` / `!exp -500` add to it."""
        text = args.strip()
        if not text:
            raise gm.DevCommandError('no value')
        value = gm.parse_int(text.lstrip('+'), 'exp', lo=-progression.EXP_MAX, hi=progression.EXP_MAX)
        char = self._session_char(session)
        if char is None:
            raise gm.DevCommandError('no character in this session')
        self._refuse_dead(session)
        with self.store.lock:
            current = progression.clamp_exp(char.get('exp', 0))
        delta = value if text[0] in '+-' else value - current
        self.grant_exp(session, delta)
        with self.store.lock:
            total = progression.clamp_exp(char.get('exp', 0))
        self._gm_reply(session, f'Exp {total} (Lv.{progression.level_for_exp(total)}).')

    def _dev_socket(self, session, args):
        """`!socket <equip_id> <stone_id> [stone_id ...]` (item_inventory-stone-extraction test
        aid, P8 stage 4): an equipment instance whose socket words w0.. are these stones goes
        into the bag model. No packet adds an item WITH its option words (0x18 grants a zero
        block), so the caller's map reloads in place at his floor point (_map_transfer to the
        same map, as a same-map /go does, on the floor: livetest bug 5): the 0x03 lists the new
        instance, and the extraction window 0x473 can take it."""
        parts = args.split()
        if len(parts) < 2:
            raise gm.DevCommandError('an equipment id and 1..5 stone ids')
        if len(parts) > 1 + invmod.WIRE_OPTION_WORDS:
            raise gm.DevCommandError(f'at most {invmod.WIRE_OPTION_WORDS} stones')
        item = gm.parse_int(parts[0], 'equip_id', lo=1, hi=EC.item_max_id())
        stones = [gm.parse_int(p, 'stone_id', lo=1, hi=EC.item_max_id()) for p in parts[1:]]
        bag = self._bag(session)
        if bag is None or not session.get('in_world') or session.get('current_map') is None:
            raise gm.DevCommandError('not in the world')
        if not en_item_exists(item) or bag.tab_of(item) != 'equip':
            raise gm.DevCommandError(f'{item} is not EN equipment')
        missing = [s for s in stones if not en_item_exists(s)]
        if missing:
            raise gm.DevCommandError(f'stone(s) {missing} not in the EN client catalog')
        # Only what the client's reinforcement writes into a socket (P8 review): a typo such as
        # `!socket 179 70` put an equipment id there, which an extraction then granted.
        others = [s for s in stones if not craftmod.is_option_stone(s)]
        if others:
            raise gm.DevCommandError(f'{others}: not elemental option stones ({craftmod.OPTION_STONE_FIRST}..'
                                     f'{craftmod.OPTION_STONE_LAST})')
        words = stones + [0] * (invmod.OPTION_WORDS - len(stones))
        with self._combat_lock(session):
            if bag.add(item, 1, words) is None:
                raise gm.DevCommandError(f'no room: {bag.fits(item, 1, words)}')
        self.store.mark_dirty(f'!socket {item}')
        code = int(session['current_map'])
        x, y = presence.floor_point(session)
        self._gm_reply(session, f'{EC.item_name(item) or item} ({item}) with sockets {stones} in your bag; '
                                f'reloading map {code}.')
        self._map_transfer(session['sock'], session, code, x, y, reason='!socket',
                           no_enc=session.get('no_enc', True))

    def _dev_event(self, session, args):
        """`!event ...` (registered by events.py through gm.register): Events.dev_event."""
        self.events.dev_event(session, args)

    def _dev_expmult(self, session, args):
        """`!expmult [x|off]` (registered by events.py): Events.dev_expmult."""
        self.events.dev_expmult(session, args)

    def _dev_boss(self, session, args):
        """`!boss [list] | respawn [all|<map>|<name>]` (registered by bosses.py): Bosses.dev_boss."""
        self.bosses.dev_boss(session, args)

    def _dev_give(self, session, args):
        """Give the caller an item (S2C 0x18 GetItem) and mirror it in the bag model. The
        EN catalog gate refuses a KR-only id, which the client would silently drop (S2-11).
        A Type 3 skill book or Type 4 job item is applied on receipt instead of filed in a bag
        tab, so it goes through the learn (cs-skill-learn) or the class change
        (lc-class-change) that persists what the client does with it. A Type 5 cash item has
        no bag tab either: it is the grant of `!cash item` (a cash inventory record, S2C 0x6F;
        a Note included) with the same count as any other `!give` (1 when omitted - not the
        catalog quantity `!cash item` alone grants), and what the cash model refuses is
        refused with its reason."""
        parts = args.split()
        if not parts:
            raise gm.DevCommandError('no item id')
        item = gm.parse_int(parts[0], 'item_id', lo=1, hi=EC.item_max_id())
        count = gm.parse_int(parts[1], 'count', lo=1, hi=999) if len(parts) > 1 else 1
        if not en_item_exists(item):
            raise gm.DevCommandError(f'item {item} is not in the EN client catalog')
        if en_item_type(item) == EC.TYPE_SKILL:
            # The client learns a type-3 0x18 with no level/class check, so this is a
            # mirrored learn (persisted) rather than a bare 0x18 the next map load forgets.
            res = self._learn_skill(session, item, check=False, notify=True, what='!give')
            if not res.ok:
                raise gm.DevCommandError(f'skill {item} not learned: {res.why}')
            self._gm_reply(session, f'Learned {EC.item_name(item)} ({item}).')
            return
        if en_item_type(item) == EC.TYPE_CLASS_CHANGE:
            # The client applies a type-4 0x18 through FUN_004249E0, so a bare 0x18 changed
            # the class until the next map load rebuilt it from the record (lc-class-change).
            res = self._apply_job_item(session, item, notify=True, what='!give')
            if not res.ok:
                raise gm.DevCommandError(f'{EC.item_name(item)} ({item}) not applied: {res.why}')
            self._gm_reply(session, f'Applied {EC.item_name(item)} ({item}): '
                                    f'{CC.class_name(res.cls, res.tier)}.')
            return
        bag = self._bag(session)
        if bag is None:
            raise gm.DevCommandError('no character in this session')
        tab = bag.tab_of(item)
        if tab is None:
            # Type 5 (cash / Spark items): no bag tab, and the S2C 0x18 below adds nothing
            # for it (livetest bug 6: "Gave" was answered while nothing arrived). Since P8 it
            # lives in the cash inventory: the same grant as `!cash item`, whose CashError (a
            # pet, a slot extension, a full owned list) is the refusal - never an 0x18, never a
            # false "Gave". The count is always passed: omitted means 1 as for every `!give`
            # (livefix's `!give 1894` gave exactly 1), where `!cash item <id>` alone grants
            # the catalog quantity (3320 "11 Message Pads": 11).
            self._dev_cash(session, f'item {item} {count}')
            return
        if self._inv_add(session, item, count, '!give') is None:
            raise gm.DevCommandError(f"item {item} does not fit: the {tab} tab is full")
        self._send_drop(session['sock'], session, item=item, count=count)
        self._gm_reply(session, f'Gave {count} x {EC.item_name(item)} ({item}).')

    def _dev_warp(self, session, args):
        """Load another map at a point through the F6 MapTransfer primitive - the same
        sequence, guards and persistence a portal gets (world-maptransfer). Without a point
        the player lands where a portal into the map does (_warp_point); a GM's `/warp` line
        is the same command (shop_storage-flea-warp: `/warp 9701` for the stall checks)."""
        parts = args.split()
        if not parts:
            raise gm.DevCommandError('no map code')
        map_code = gm.parse_int(parts[0], 'map', lo=1, hi=0xFFFF)
        if session.get('in_cash_shop'):
            # premium_cash-presence: only the mall's EXIT brings its player back (_map_transfer)
            raise gm.DevCommandError(f'not while you are in the {mallmod.mall_name(self.client_build)} '
                                     f'(close it with EXIT)')
        # The KR Yahoo map table lacks maps the EN clients have - the herbal farms and mining
        # areas (241, 243, ...) the P4 gathering check needs - so a map with a stage .hmi in
        # the running build's own client files is valid too (en_maps.load_map, world-portal-
        # table-en).
        if map_code not in map_codes and not self._client_has_map(map_code):
            raise gm.DevCommandError(f'map {map_code} is not in the map table')
        # No point given: where a real portal into the map lands (shop_storage-flea-warp:
        # the start point is 101's, and on the flea market 9701 it is 1500 px above the
        # floor), so `!warp 9701` / `/warp 9701` arrive where a town's market portal does.
        px, py, _how = self._warp_point(map_code)
        x = gm.parse_float(parts[1], 'x') if len(parts) > 1 else px
        y = gm.parse_float(parts[2], 'y') if len(parts) > 2 else py
        if map_code == session.get('current_map'):
            raise gm.DevCommandError(f'already on map {map_code}')
        if self._session_char(session) is None:
            raise gm.DevCommandError('no character in this session')
        name = map_filename(map_code) or EC.map_name(map_code)
        if map_code == EC.STALL_MAP and session.get('current_map') is not None:
            self._remember_market_return(session, int(session['current_map']))
        x, y = self._arrival_point(map_code, x, y)          # the floor point he lands on
        self._gm_reply(session, f'Warping to map {map_code} ({name}) at ({x}, {y}).')
        self._map_transfer(session['sock'], session, map_code, x, y, reason='dev warp',
                           no_enc=session.get('no_enc', True))

    def _warp_point(self, map_code):
        """(x, y, how) a warp with no point lands at: the start point on START_MAP, else the
        arrival of the first portal from another map into it (combat.entry_point, the EN
        portal table's arrival rule), else the start point."""
        start_x, start_y = float(self.config.START_X), float(self.config.START_Y)
        if int(map_code) == int(self.config.START_MAP):
            return start_x, start_y, 'the start point'
        point = combat.entry_point(map_code, EC.portals())
        if point is not None:
            return float(point[0]), float(point[1]), 'where a portal into the map lands'
        return start_x, start_y, 'the start point (no portal leads there)'

    @staticmethod
    def _client_has_map(map_code):
        """True when the running build's client install has stageAA_BB.hmi for this code."""
        import en_maps                  # late, like en_content.map_spawns
        return en_maps.load_map(map_code) is not None

    def _dev_vital_value(self, session, args, key):
        """`!hp 30` -> 30, `!hp -30` / `!hp +30` -> current -/+ 30 (the regen live check needs
        a way below max that the server model sees: an injected 0x28 would leave the model
        at max, so it would never regenerate). A bare `!hp` / `!mp` -> the maximum: a fill
        (polish 2026-09-28: it only answered a usage warning, and logged nothing). A derived
        maximum of 0 (hpmp: a class >= 7, level 0 or above 99 - "never used to clamp") has
        nothing to fill to: refused, instead of setting HP 1 / MP 0."""
        text = args.strip()
        value = gm.parse_int(text.lstrip('+'), key, lo=-0xFFFF, hi=0xFFFF) if text else None
        if not session.get('in_world'):
            raise gm.DevCommandError('not in world')
        self._refuse_dead(session)
        char = self._session_char(session)
        if char is None:
            raise gm.DevCommandError('no character in this session')
        with self._combat_lock(session):
            if value is None:
                d = hpmp.refresh(session, char)
                top = int(d.max_hp if key == 'hp' else d.max_mp)
                if top <= 0:
                    raise gm.DevCommandError(f'no {key.upper()} maximum for class {d.job} Lv{d.level} '
                                             f'(the derived maximum is 0); give a value')
                return top
            current = int(session.get(key) or 0)
        return current + value if text[0] in '+-' else value

    @staticmethod
    def _refuse_dead(session):
        """A dev command that changes HP / MP or the level refuses on a corpse. livetest bug
        11: an HP / MP change on a corpse left the server model alive with the client's death
        dialog still up (!damage refuses the same way); a level-up by !level / !exp healed the
        corpse to its new maxima the same way (grant_exp, review of bug 11)."""
        if session.get('dead'):
            raise gm.DevCommandError('dead (click Revived, or !revive)')

    def _dev_hp(self, session, args):
        """Set the caller's current HP through the model (S2C 0x28). Never below 1: an 0x28
        of 0 leaves a dead player with no dialog and no way out until cs-player-death lands
        the 0x3E death flow (combat_skill.md F8 step 1)."""
        value = max(1, self._dev_vital_value(session, args, 'hp'))
        hp, _ = self._set_vitals(session, hp=value, reason='!hp')
        self._gm_reply(session, f'HP {hp}/{session.get("max_hp")}.')

    def _dev_mp(self, session, args):
        """Set the caller's current MP through the model (S2C 0x44)."""
        value = max(0, self._dev_vital_value(session, args, 'mp'))
        _, mp = self._set_vitals(session, mp=value, reason='!mp')
        self._gm_reply(session, f'MP {mp}/{session.get("max_mp")}.')

    def _dev_vitals(self, session, args):
        char = self._session_char(session)
        if char is None:
            raise gm.DevCommandError('no character in this session')
        with self._combat_lock(session):
            d = hpmp.refresh(session, char)
            hp, mp = session.get('hp'), session.get('mp')
        self._gm_reply(session, f'Lv{d.level} job {d.job}: HP {hp}/{d.max_hp} MP {mp}/{d.max_mp}, '
                                f'regen +{d.regen_hp} HP (idle) / +{d.regen_mp} MP per '
                                f'{self.config.REGEN_SECS:g} s.')

    def _dev_learn(self, session, args):
        """`!learn 292` learns a skill the way a skill master sells it (level and class
        gates, no gold); `!learn 292 force` skips the gates, which the client allows too (a
        type-3 0x18 learns with no check). Persisted, so portal and relog keep it
        (cs-skill-learn; P3 exit criterion 3)."""
        parts = args.split()
        if not parts:
            raise gm.DevCommandError('no skill id')
        skill = gm.parse_int(parts[0], 'skill_id', lo=1, hi=EC.item_max_id())
        force = len(parts) > 1 and parts[1].lower() == 'force'
        sd = SK.skill_def(skill)
        if sd is None:
            raise gm.DevCommandError(f'{skill} is not an EN skill record')
        if not session.get('in_world'):
            raise gm.DevCommandError('not in world')
        res = self._learn_skill(session, skill, check=not force, notify=True, what='!learn')
        if not res.ok:
            raise gm.DevCommandError(f'{sd.name} ({skill}): {res.why}')
        self._gm_reply(session, f'Learned {sd.name} Lv{sd.level} ({skill}): MP {sd.mp_cost}, '
                                f'CT {sd.ct / 1000:g} s'
                                + (f', lasts {sd.duration_ms / 1000:g} s' if sd.duration_ms else '')
                                + '.')

    def _dev_unlearn(self, session, args):
        """Forget a skill in the record (test aid: re-learn, reset a quest reward). The client
        keeps its copy until the next map load rebuilds its list from the 0x07."""
        skill = gm.parse_int(args, 'skill_id', lo=1, hi=EC.item_max_id())
        char = self._session_char(session)
        if char is None:
            raise gm.DevCommandError('no character in this session')
        with self.store.lock:
            removed = SK.unlearn(char, skill)
        if not removed:
            raise gm.DevCommandError(f'{skill} is not learned')
        self.store.mark_dirty(f'!unlearn {skill}')
        self._gm_reply(session, f'Forgot {EC.item_name(skill)} ({skill}); the skill window '
                                f'drops it at the next map load.')

    def _dev_skills(self, session, args):
        """The learned list (what the 0x07 skill list carries) and the running buffs with
        the time the server will end them at."""
        char = self._session_char(session)
        if char is None:
            raise gm.DevCommandError('no character in this session')
        ids = SK.learned(char)
        for line in gm.wrap_list([str(i) for i in ids] or ['none'], 'Skills: '):
            self._gm_reply(session, line)
        now = time.monotonic()
        with self._combat_lock(session):
            running = [(b['id'], buffmod.remaining_ms(b, now)) for b in buffmod.state(session)]
        text = ', '.join(f'{i} {left / 1000:.1f}s' for i, left in running) or 'none'
        self._gm_reply(session, f'Buffs: {text}')

    def _dev_damage(self, session, args):
        """`!damage 30`: a server-side damage event (cs-player-death, P3 exit criterion 5).
        Goes through damage_player like any other damage: S2C 0x28 while HP is left, the
        death (S2C 0x3E dialog) at 0. `!hp` stays the "set a value" aid and never reaches 0."""
        amount = gm.parse_int(args, 'n', lo=1, hi=0xFFFF)
        if not session.get('in_world'):
            raise gm.DevCommandError('not in world')
        if session.get('dead'):
            raise gm.DevCommandError('already dead (click Revived, or !revive)')
        hp = self.damage_player(session, amount, reason='!damage')
        if hp is None:
            raise gm.DevCommandError('no damage taken (not in world, dead, or no character)')
        self._gm_reply(session, f'HP {hp}/{session.get("max_hp")}.' if hp else 'You died.')

    def _dev_die(self, session, args):
        with self._combat_lock(session):
            hp = int(session.get('hp') or 0)
        self._dev_damage(session, str(max(1, hp)))

    def _dev_revive(self, session, args):
        """The death dialog's revive without the dialog (it closes client-side on its own, so
        this is the way out if it was ever lost)."""
        ok, info = self.revive_player(session, reason='!revive')
        if not ok:
            raise gm.DevCommandError(f'no revive: {info}')
        self._gm_reply(session, f'Revived on map {info[0]} at ({info[1]:g}, {info[2]:g}) with '
                                f'{session.get("hp")}/{session.get("max_hp")} HP.')

    def _dev_trap(self, session, args):
        """`!trap [uid]`: fire the caller's placed Booby Trap on a monster server-side (P3 exit
        criterion 8 "or a dev trigger fires") - the nearest live monster to the trap point at
        any distance, or the given uid. Same resolution as a client trigger: damage, the kill
        through the monster lifecycle, S2C 0x3C clears the trap icon."""
        uid = gm.parse_int(args, 'uid', lo=0, hi=0xFFFFFFFF) if args.strip() else None
        if not session.get('in_world') or session.get('dead'):
            raise gm.DevCommandError('not in world, or dead')
        with self._combat(session):
            trap = next((b for b in buffmod.state(session) if buffmod.is_trap(b['id'])), None)
            if trap is None:
                raise gm.DevCommandError('no Booby Trap placed (cast one first)')
            mons = session.get('monsters') or {}
            mob = mons.get(uid) if uid is not None else combat.nearest(mons.values(), (trap['x'], trap['y']))
            if mob is None or not mob.alive:
                raise gm.DevCommandError('no live monster to catch' + (f' with uid {uid:#x}' if uid else ''))
            pending = {'id': trap['id'], 'x': trap['x'], 'y': trap['y'], 't': time.monotonic()}
            session.pop('pending_trap', None)
            # client_catch=False: no distance check (the GM picked the victim) and no hit-lock
            # release (the client caught nothing, so it set none).
            hit = self._resolve_trap(session['sock'], session, pending, mob.uid, None, '!trap',
                                     client_catch=False)
        self._gm_reply(session, f'Trap {trap["id"]} fired on {hit.name} uid {hit.uid:#x} '
                                f'({"killed" if not hit.alive else f"{hit.hp}/{hit.max_hp} HP left"}).')

    def _dev_job(self, session, args):
        """`!job 3076` changes class the way a quest reward or a job item does (lc-class-change,
        P3 exit criterion 6): persisted class/job2, S2C 0x18 with the job item (popup "<class>
        is your new class." + HUD label), 0x58 to observers.

        FUN_004249E0 accepts a tier item only from its base class at tier 0 and a base item
        only from Novice, so when the character is not there yet the command jumps it to the
        prerequisite first with the silent GM fix-up (login_character F8 step 3: S2C 0x58 to
        the owner's own uid - live login_character#16 sets the two bytes and recomputes, but
        redraws nothing); the 0x18 that follows then passes the client's own check and
        redraws the HUD. From a Novice, `!job 3076` is therefore Warrior (silent) + Berserker.

        `!job novice` (or 0) goes back to Novice: 0x58 {uid, 0, 0} plus an S2C 0x14 resync of
        STR, the redraw live #16 needed before the HUD showed the new class."""
        text = args.strip().lower()
        if not text:
            raise gm.DevCommandError('no job item id')
        if not session.get('in_world') or session.get('sock') is None:
            # FUN_00440920 case 4 needs the local player (scene+0x970): at the select screen
            # the client would drop the 0x18 and the record would disagree with it.
            raise gm.DevCommandError('not in world')
        char = self._session_char(session)
        if char is None:
            raise gm.DevCommandError('no character in this session')
        if text in ('novice', '0'):
            old = CC.of(char)
            self._set_class_silently(session, char, *CC.NOVICE, what='!job novice')
            self._gm_reply(session, f'Class {CC.class_name(*old) or old} -> Novice.')
            return
        item = gm.parse_int(text, 'job item id', lo=1, hi=EN_ITEM_MAX_ID)
        if not CC.is_job_item(item):
            raise gm.DevCommandError(f'{item} ({EC.item_name(item) or "?"}) is not a job-change item')
        steps = []
        if CC.refusal(*CC.of(char), item) is not None:
            need = CC.prerequisite(item)
            self._set_class_silently(session, char, *need, what=f'!job {item} prerequisite')
            steps.append(CC.class_name(*need))
        res = self._apply_job_item(session, item, notify=True, what='!job')
        if not res.ok:
            raise gm.DevCommandError(f'{EC.item_name(item)} ({item}): {res.why}')
        steps.append(CC.class_name(res.cls, res.tier) or f'class {res.cls} tier {res.tier}')
        self._gm_reply(session, f'Class -> {" -> ".join(steps)} (class {res.cls}, tier {res.tier}).')

    def _dev_gold(self, session, args):
        """`!gold 500` / `!gold -500`: the stored wallet plus S2C 0x3F, so the live checks of
        the village fee (P3 exit criterion 7: "with insufficient gold ...") run on a balance
        the server and the client agree on - an injected 0x3F/0x81 moves only the label."""
        text = args.strip()
        if not text:
            raise gm.DevCommandError('no value')
        value = gm.parse_int(text.lstrip('+'), 'gold', lo=-invmod.GOLD_MAX, hi=invmod.GOLD_MAX)
        wallet = self._wallet_of(session)
        if wallet is None:
            raise gm.DevCommandError('no character in this session')
        with self._combat_lock(session):
            wallet.gold = wallet.gold + value if text[0] in '+-' else value
            self._wallet_commit(session, wallet, '!gold')
        if session.get('in_world'):
            self._send_gold(session['sock'], session)
        self._gm_reply(session, f'Gold {wallet.gold}.')

    def _dev_bank(self, session, args):
        """`!bank` (or a GM's `/bank` line): shop_storage-bank-fallback, F3b. The 2008 bank NPC
        shows "no SS# in this ID..." and sends nothing when scene+0x218 is 0; this opens the
        same window the password gate does (0x65, 0x80 {1, 0x1A7}, 0x65), from anywhere."""
        if not self._open_bank(session.get('sock'), session, '!bank'):
            raise gm.DevCommandError('no character in world')
        bank = self._bank(session)
        self._gm_reply(session, f'Bank opened: gold {bank.gold}, slots {bank.caps}.')

    def _dev_loot(self, session, args):
        """`!loot 5 3`: put an item on the ground at the caller's feet (S2C 0x12 with source =
        the caller, owner 0), for the pickup / despawn / map re-entry live checks of P4 stage 2
        without waiting for a monster's drop roll. Nothing leaves the bag."""
        parts = args.split()
        if not parts:
            raise gm.DevCommandError('no item id')
        item = gm.parse_int(parts[0], 'item_id', lo=1, hi=EC.item_max_id())
        count = gm.parse_int(parts[1], 'count', lo=1, hi=999) if len(parts) > 1 else 1
        if not session.get('in_world'):
            raise gm.DevCommandError('no character in world')
        why = self._groundable(item)
        if why is not None:
            raise gm.DevCommandError(f'item {item} cannot lie on the ground: {why}')
        x, y = session.get('pos') or (0.0, 0.0)
        placed = self._place_ground_item(session, item, count, None, x=x, y=y, owner_uid=0,
                                         source_uid=P.session_uid(session) or 0, what='!loot')
        self._gm_reply(session, f'Dropped {placed.qty} x {EC.item_name(item)} ({item}): ground id '
                                f'{placed.ground_id}, gone in {self.config.get("GROUND_ITEM_SECS", 60.0):g} s.')

    def _dev_grant(self, session, args):
        """`!grant 2030` / `!grant 5 30`: a GM item grant that leaves the wallet alone (S2C
        0x99 sub 9, grant_item) - e.g. a Monster Card for the Card Deck live check (P4 exit
        criterion 6). `!give` keeps the S2C 0x18 path, which also learns skill books."""
        parts = args.split()
        if not parts:
            raise gm.DevCommandError('no item id')
        item = gm.parse_int(parts[0], 'item_id', lo=1, hi=EC.item_max_id())
        count = gm.parse_int(parts[1], 'count', lo=1, hi=0xFFFF) if len(parts) > 1 else 1
        ok, why = self.grant_item(session, item, count, '!grant')
        if not ok:
            raise gm.DevCommandError(f'{EC.item_name(item) or "item"} ({item}) not granted: {why}')
        self._gm_reply(session, f'Granted {count} x {EC.item_name(item)} ({item}).')

    def _dev_deck(self, session, args):
        """`!deck`: the registered Monster Cards (quest_cards_misc-card-register). `!deck
        clear` empties the deck (test aid) and re-sends S2C 0x8A, which replaces the client's
        whole list and label; the cards do not come back to the bag, as on the client."""
        state = self._quest_state(session)
        if state is None:
            raise gm.DevCommandError('no character in world')
        word = args.strip().lower()
        if word not in ('', 'clear'):
            raise gm.DevCommandError(f'unknown argument {word!r}')
        if word == 'clear':
            with self._combat_lock(session):
                gone = len(state.deck)
                del state.deck[:]
                self.store.mark_dirty('!deck clear')
                P.send(self, session['sock'], session, '0x8A', {'deck_count': 0})
            self._gm_reply(session, f'Card Deck cleared ({gone} card(s) removed).')
            return
        if not state.deck:
            self._gm_reply(session, f'Card Deck 0/{questmod.MAX_DECK}: empty.')
            return
        names_ = [f'{EC.item_name(c)} ({c}),' for c in state.deck]
        for chunk in gm.wrap_list(names_, f'Card Deck {len(state.deck)}/{questmod.MAX_DECK}: '):
            self._gm_reply(session, chunk.rstrip(','))

    # C2S 0x04 stat_index -> the character record's key (+0xE6/+0xE8/+0xEA/+0xEC).
    STAT_KEYS = ('str', 'dex', 'int', 'spr')

    def _handle_set_stats(self, sock, session, rec, no_enc=False):
        """
        C2S 0x04 StatPointAllocate {u8 stat_index} -> S2C 0x14 SetBaseStat
        {u32 uid, u8 stat_index, u16 value}, flow F7.

        The client never checks the free points itself: it just sends the index of the +
        button (0 STR, 1 DEX, 2 INT, 3 SPR) and writes whatever absolute value comes back.
        So the server owns the rule: free = stat_total(level) - (str+dex+int+spr), with
        stat_total(L) = 9 + 4*min(L-1,28) + 5*max(0,L-29) (progression, the client's own
        FUN_00440E10). The old handler answered every index with a hard-coded 4, which
        overwrote the stat and lost every allocation at the next spawn (B10, S2-28).

        - index > 3, or not in world: ignored (nothing to resync, and 0x04 is not a
          must-reply opcode). Index 7 is the harmless stat-window redraw the live
          verification used, so an unknown index must stay silent.
        - no free points: the current value is sent back, which re-syncs a client that
          raced ahead (or a tampered packet) instead of granting a point.
        - otherwise: +1, persisted (debounced save), and the new absolute value goes back.
        """
        index = int(rec.get('stat_index', 0xFF))
        if not session.get('in_world'):
            log.info(f'[STATS] index {index} ignored: not in world')
            return
        if not 0 <= index < len(self.STAT_KEYS):
            log.info(f'[STATS] index {index} ignored: only 0..3 are stat buttons')
            return
        key = self.STAT_KEYS[index]
        char = self._session_char(session)
        if char is None:
            log.warning('[STATS] no character on this session; ignored')
            return
        with self.store.lock:
            spent = sum(int(char.get(k, 0) or 0) for k in self.STAT_KEYS)
            total = progression.stat_total(R.level_of(char))
            free = total - spent
            if free > 0:
                char[key] = int(char.get(key, 0) or 0) + 1
                granted = True
            else:
                granted = False
            value = int(char.get(key, 0) or 0)
        if granted:
            self.store.mark_dirty(f'stat {key} {char.get("name")}')
            log.info(f'[STATS] {char.get("name")!r} {key} -> {value} '
                     f'({free - 1} free of {total} at level {R.level_of(char)})')
            # INT feeds max MP and SPR max HP (FUN_00427f40 +0xEA / FUN_00427d40 +0xEC). The
            # client recomputes on the 0x14 below and keeps the current value (live: SPR 30
            # -> "HP 100/410"), so the model does the same: new maxima, no heal.
            self._refresh_vitals(session, char, reason=f'stat {key}')
        else:
            log.warning(f'[STATS] {char.get("name")!r} has no free points ({spent}/{total}); '
                        f'resyncing {key} = {value}')
        P.send(self, sock, session, '0x14',
               {'uid': P.session_uid(session) or 0, 'stat_index': index, 'value': value & 0xFFFF})

    # =================================================================
    # NPC SHOP (C2S 0x0B buy, 0x0C sell -> S2C 0x18 / 0x19) - shop_storage.md 1.2-1.4, F1/F2
    #
    # Wire formats (protocol_spec.json; spec_2009 is byte-identical for all four):
    #   C2S 0x0B NpcShopBuy  : u16 item_id, u16 qty, u16 npc_id (window 0xD +0x11C = the
    #       clicked NPC's hni idx; the old "u8 undef" was the high byte of npc_id, B14).
    #       2009 adds a second send site, 0x474327, for the guild NPC menu (same bytes).
    #   C2S 0x0C NpcShopSell : u16 item_id, u16 qty, u8 n, n x u16, u16 extra
    #       (qty is u16: the old u8 read sold 44 of 300, shop_storage B1).
    #   S2C 0x18 GetItem     : u64 gold, u32 victy, u16 item_id, u16 count - absolute wallet
    #       plus a grant; item 0 is the pure resync. For a Type-3 id the client LEARNS the
    #       skill (FUN_00440920 case 3) and for Type 4 changes class; neither is bagged.
    #   S2C 0x19 SellResult  : u64 gold, u16 item_id, u16 count, u8 n, n x u16, u16 extra
    #       (removes the item client-side; equipment by exact 12-byte option block).
    # The shop list is client-local (no packet opens window 0xD): FUN_00465a80 builds it from
    # the merchant's hni `item:` column, which is exactly what the buy is validated against
    # (en_content.shop_list, shop.offered). S2C 0x80 is the password-gate result, not a cash
    # balance (B8): see _handle_password_verify.
    #
    # The wallet is the store's char['gold'] / char['victy'] (shop_storage-wallet,
    # trade-economy-persist): one u64 and one u32 that every absolute currency value on the
    # wire is read from. The session defaults 999999/999999 and the 0x03 constant 100000
    # are gone - together they made the first 0x18/0x19 of a session jump the client's gold
    # label from 100000 to 999999 (shop_storage B2) and every portal reset it (B3).
    # =================================================================

    def _wallet_of(self, session):
        """The inventory.Wallet bound to this session's character record, or None."""
        char = self._session_char(session)
        return None if char is None else invmod.Wallet(char)

    def _wallet(self, session):
        """(gold, victy) as the client must see them. A session with no character (character
        select, a dev probe) reads 0/0: nothing to spend and nothing to show."""
        wallet = self._wallet_of(session)
        return (0, 0) if wallet is None else wallet.as_tuple()

    def _wallet_commit(self, session, wallet, reason='wallet'):
        """Persist a wallet change (F4 debounced save)."""
        self.store.mark_dirty(f'{reason} {(self._session_char(session) or {}).get("name")}')
        return wallet.as_tuple()

    def _send_currency_resync(self, sock, session):
        """S2C 0x18 {gold, victy, 0, 0}: the absolute wallet with no grant. Every shop or
        item refusal sends it so the labels go back to the server's truth (shop_storage
        F1/B5); item_id 0 grants nothing client-side. _send_shop_refusal is this plus the
        0x15 that says why, and a session with no character resyncs 0/0 - the same bytes
        _shop_refusal_replies gives registry.MUST_REPLY."""
        P.send(self, sock, session, '0x18', invmod.currency_fields(self._wallet(session)))

    # ---- gold-only and item-buff builders (item_inventory-buff-and-gold-builders) ----
    #   _send_gold       -> quest Money, the village-transfer refund and resync, `!gold`: a
    #                       gold-only change that must not name an item.
    #   _send_item_buff  -> item_inventory-use-consumable (the hii `Con` buff of a potion).
    def _send_gold(self, sock, session):
        """S2C 0x3F GoldUpdate {u64 gold}: a gold-only change (quest money, fees, refunds).
        Live-verified (T-S3F): the inventory gold label follows it with no chat line and no
        item, which 0x18 cannot do without also naming an item."""
        wallet = self._wallet_of(session)
        if wallet is None:
            return
        P.send(self, sock, session, '0x3F', invmod.gold_fields(wallet))

    def _send_item_buff(self, sock, session, item_id, target_uid=None, source_uid=0, refresh=True):
        """S2C 0x42 (owner: buff icon + stat panel) / 0x41 (observers) for an item whose hii
        `Con` is its duration in ms (def+0x1C8, proven live with item 148: Con 10000 ->
        a 10 s countdown in the entity's buff record)."""
        if target_uid is None:
            target_uid = P.session_uid(session) or 0
        fields = invmod.item_buff_fields(target_uid, item_id, source_uid)
        P.send(self, sock, session, '0x42' if refresh else '0x41', fields)

    def _handle_buy_item(self, sock, session, rec, no_enc=False):
        """C2S 0x0B NpcShopBuy {u16 item_id, u16 qty, u16 npc_id} -> S2C 0x18
        {gold, victy, item_id, qty} (shop_storage-npc-buy, F1; fixes B4/B5/B7).

        The client has already run FUN_00467680 (2009 FUN_00471450) on its own copy of every
        value; the server re-derives each answer from the same data, in the same order:
          1. npc_id is a merchant (hni `UI: 13`) and the item is the id one of its rows shows
             (shop.offered: a skill master's row sells only the NEXT level of a family the
             player knows). A 2009 guild NPC (UI 0x4BB, send site 0x474327) is refused: the
             server has no guilds.
          2. hii record exists and is not a Cash item (def+0x1F0: never sold by an NPC).
          3. quantity 1..999 (Type 0) / 1..99 (Type 2); every other Type buys exactly one.
          4. gold AND Victy, both with the client's discount (shop.discount / shop.cost, u64:
             the client's own multiply is 32-bit).
          5. Type 3 learns (level / class / already-known gates, no bag entry), Type 4 changes
             class, Types 0/1/2 must fit the bag model.
        Success = debit, grant, persist, then ONE 0x18. Every refusal is the 0x18 resync plus
        a 0x15 line - never a 0x18 that names an item (it would grant it)."""
        item = int(rec.get('item_id', 0))
        qty = int(rec.get('qty', 0))
        npc_id = int(rec.get('npc_id', 0))
        char = self._session_char(session)
        wallet = self._wallet_of(session)

        def refuse(why, text='That item is not for sale.'):
            log.info(f'[BUY] item={item} x{qty} npc={npc_id} refused: {why}')
            self._send_shop_refusal(sock, session, text)

        if char is None or wallet is None or not session.get('in_world'):
            return refuse('no character in world')
        npc = EC.npcs().get(npc_id)
        if npc is not None and npc.ui == EC.UI_GUILD_NPC_2009:
            return refuse(f'guild NPC {npc_id} {npc.name} (2009 guild menu 0x4BB): no guild system',
                          'Guilds are not available.')
        stock = EC.shop_list(npc_id)
        if stock is None:
            return refuse(f'npc {npc_id} ({EC.npc_name(npc_id)}) is not a merchant (hni UI != 13)')
        if not self._grantable_item(item, 'buy'):
            return refuse('not an EN client item')
        base = SH.offered(stock, item, SK.learned(char))
        if base is None:
            why = None
            if SH.family_row(stock, item) is not None:
                # A level of a stocked family that the row does not show (already known, or
                # not the next one): answer with the learn gate's own reason when it has one.
                why = SK.learn_refusal(char, SK.skill_def(item), R.level_of(char))
            return refuse(f'not on {EC.npc_name(npc_id)}\'s list {stock} (skill rows show the '
                          f'next level of the family only){f": {why}" if why else ""}',
                          self._learn_refusal_text(why) if why else 'That item is not for sale.')
        info = en_item(item)
        if info is None or info.is_cash:
            return refuse('cash item (hii Cash, itemdef+0x1F0 != 0)', "That item can't be bought here.")
        limit = SH.qty_limit(info.type)
        if not 1 <= qty <= limit:
            return refuse(f'qty {qty} outside 1..{limit} for Type {info.type}',
                          'That amount cannot be bought.')
        d = SH.discount(self.client_build, info.type, self.store.manner(session.get('username')),
                        char.get('equipped'))
        gold_cost, victy_cost = SH.cost(info.buy, qty, d), SH.cost(info.pmoney, qty, d)
        why = self.trade.gold_refusal(session, gold_cost)
        if why is not None:
            # trade-escrow-guards: gold locked into a trade (C2S 0x23) is not spendable.
            return refuse(why, trademod.GOLD_IN_TRADE_TEXT)
        if info.type == EC.TYPE_SKILL:
            # A skill master's "skill book" is a learn, not a bag item (cs-skill-learn, B7).
            self._buy_skill(sock, session, item, wallet, gold_cost, victy_cost)
            return
        if info.type == EC.TYPE_CLASS_CHANGE:
            # A job item is the class change itself, applied on receipt (lc-class-change). No
            # EN merchant lists one, so this only runs for a content table that adds one.
            self._buy_job_item(sock, session, item, wallet, gold_cost, victy_cost)
            return
        bag = self._bag(session)
        if bag.tab_of(item) is None:
            return refuse(f'Type {info.type} has no bag tab', "That item can't be bought here.")
        with self._combat_lock(session):
            # The balance test and the debit are ONE step under the lock the combat driver
            # also credits gold under: checking first and then ignoring what pay() answers
            # handed out a free item whenever the balance dropped in between.
            if not wallet.pay(gold_cost, victy_cost):
                short = 'Victy' if wallet.victy < victy_cost else 'gold'
                return refuse(f'cost {gold_cost} gold + {victy_cost} Victy > wallet '
                              f'{wallet.gold}/{wallet.victy}', f'Not enough {short}.')
            if self._inv_add(session, item, qty, 'buy') is None:
                wallet.earn(gold_cost, victy_cost)     # nothing was granted, so nothing is charged
                return refuse('bag tab full', "There isn't empty space in the inventory.")
            gold, victy = self._wallet_commit(session, wallet, 'buy')
        log.info(f'[BUY] {EC.item_name(item)} item={item} x{qty} from npc {npc_id} '
                 f'{EC.npc_name(npc_id)} cost={gold_cost} gold + {victy_cost} Victy (x{d:g}) '
                 f'-> gold={gold} victy={victy}')
        P.send(self, sock, session, '0x18', invmod.currency_fields(wallet, item, qty))

    # Client stack limits for a sold quantity (shop_storage.md F2 step 1: u16 stacks 999,
    # u8 stacks 99, equipment 1).
    SELL_QTY_MAX = {0: 999, 1: 1, 2: 99}

    def _handle_sell_item(self, sock, session, rec, no_enc):
        """C2S 0x0C NpcShopSell {u16 item_id, u16 qty, u8 n, n x u16 socket, u16 extra}
        -> S2C 0x19 {gold, item_id, count, n, opts, extra} (shop_storage-sell-parse, F2).

        - qty is u16 (spec 0x46A679/0x0C, Add u16 @0x46A613): the old u8 read sold 44 of a
          300 stack and 1 of 256 (B1).
        - 0x19 echoes the client's descriptor. The client removes equipment only by an exact
          12-byte option-block match, so rebuilding a zero block fails for socketed gear.
        - n > 5 is refused: 0x19 reads options into a 6-word stack array with no bound
          (>= 63 reaches the /GS cookie; packets.LIST_CAPS).
        - Every refusal is the 0x18 {gold, victy, 0, 0} resync plus a 0x15 warning, never a
          0x19 with a count (it would remove client items). The client already closed the
          sell dialog and changed nothing, so the resync only restores the gold label (B6).
        - Stacks: qty x hii Sell is credited in one step, so selling 300 of a 300 stack pays
          300 x the price (P4 exit criterion 1). Equipment is owned by id AND the exact
          12-byte block (the bag model keeps blocks since item_inventory-model-persist), so a
          socketed item sells and removes exactly that instance."""
        item, qty, n, opts, extra = P.item_descriptor(rec)
        refusal = self._sell_refusal(session, item, qty, n, opts, extra)
        if refusal is not None:
            why, text = refusal
            log.info(f'[SELL] item={item} x{qty} n={n} extra={extra} refused: {why}')
            self._send_shop_refusal(sock, session, text)
            return
        info = en_item(item)
        wallet = self._wallet_of(session)
        refund = (info.sell if info else 0) * qty            # hii `Sell`, the EN refund
        words = invmod.block_from_wire(opts, extra) if info is not None and info.type == 1 else None
        with self._combat_lock(session):       # the combat driver credits gold/items too
            self._inv_remove(session, item, qty, words)
            wallet.earn(refund)
            gold, _victy = self._wallet_commit(session, wallet, 'sell')
        log.info(f'[SELL] {EC.item_name(item)} item={item} x{qty} n={n} extra={extra} '
                 f'refund={refund} -> gold={gold}')
        # The block is echoed VERBATIM: the client removes equipment only by an exact
        # 12-byte block match, and the stored form packs the words toward w0, so a request
        # with a zero gap (n=2 [0, 7]) came back as n=1 [7] and matched nothing - gold paid,
        # item still in the bag (echo_block_fields).
        P.send(self, sock, session, '0x19', dict(
            invmod.item_fields('0x19', item, count=qty,
                               block=invmod.echo_block_fields('0x19', opts, extra)),
            gold=gold))

    def _sell_refusal(self, session, item, qty, n, opts, extra):
        """(log reason, player text) when this sell must be refused, else None."""
        cannot = "That item can't be sold."
        if n > P.OPTION_LIST_MAX:
            return f'{n} option words (max {P.OPTION_LIST_MAX}, 0x19 stack buffer)', cannot
        if not en_item_exists(item):
            return f'item not in the EN client catalog (1..{EC.item_max_id()})', cannot
        itype = en_item_type(item)
        limit = self.SELL_QTY_MAX.get(itype)
        if limit is None:
            return f'type {itype} is not a bag item', cannot
        info = en_item(item)
        if info is not None and info.is_cash:
            return 'cash item (hii Cash, itemdef+0x1F0 != 0)', cannot
        if not 1 <= qty <= limit:
            return f'qty {qty} outside 1..{limit} for type {itype}', cannot
        if itype == 1:
            words = invmod.block_from_wire(opts, extra)
            if not self._inv_has(session, item, 1, words):
                return f'no bag instance of {item} with the block {words}', "You don't have that item."
            return self._escrow_refusal(session, item, 1, words)
        have = self._inventory(session).get(item, 0)
        if have < qty:
            return f'not owned (server bag has {have})', "You don't have that many of that item."
        return self._escrow_refusal(session, item, qty)

    def _escrow_refusal(self, session, item, count=1, words=None):
        """trade-escrow-guards (trade.md 3.6): (log reason, player text) when `count` of `item`
        (equipment: that exact block) would dip into what this session offers in a trade,
        else None. available = owned - offered: the rest of the bag stays usable, while an
        offered item cannot be sold, used, equipped, dropped, banked or handed in (the offer
        would then be a duplicate at the commit - "offer, then sell, then commit", B12). Not
        under store.lock (trade.py lock order: Trades.lock before store.lock)."""
        why = self.trade.escrow_refusal(session, item, count, words)
        return None if why is None else (why, trademod.IN_TRADE_TEXT)

    def _shop_refusal_replies(self, session, text):
        """The shop refusal as registry reply tuples: S2C 0x18 {gold, victy, 0, 0} puts the
        gold/Victy labels back (item_id 0 grants nothing) and a 0x15 warning says why
        (roadmap 1.3 row 0x0B/0x0C). registry.MUST_REPLY answers with this list when no
        handler runs; _send_shop_refusal sends the same two packets from one that did."""
        gold, victy = self._wallet(session)
        return [('0x18', invmod.currency_fields((gold, victy)), None),
                ('0x15', self.notice_fields(text, 'warn'), None)]

    def _send_shop_refusal(self, sock, session, text):
        """The same two packets _shop_refusal_replies lists, sent from a handler: the
        resync first (_send_currency_resync owns those bytes), then the warning."""
        self._send_currency_resync(sock, session)
        P.send(self, sock, session, '0x15', self.notice_fields(text, 'warn'))

    # ---- the bag model (item_inventory-model-persist / trade-economy-persist) ----
    # The three tabs, the equipment grid and the wallet live in the character record, so a
    # portal, a disconnect or a crash keeps them (they used to be a {id: count} dict on the
    # session and were lost on every relog). inventory.py owns the client's rules: stacks of
    # 999 / 99, capacities 1..45, exact-block matching and packed socket words.
    def _bag(self, session):
        """The inventory.Inventory of this session's character, or None outside the world."""
        char = self._session_char(session)
        return None if char is None else invmod.Inventory(char)

    def _inventory(self, session):
        """Flat {item_id: count} view over the three tabs (logs, quest demand checks, tests).
        Read-only: mutate through _inv_add / _inv_remove so the store is marked dirty."""
        bag = self._bag(session)
        return {} if bag is None else bag.totals()

    @staticmethod
    def _grantable_item(item, what):
        """en_item_exists with a log line naming the refused grant (S2-11)."""
        if en_item_exists(item):
            return True
        log.warning(f'[ITEM] {what}: item {item} refused, the EN client has no such item '
                    f'(catalog ids 1..{EC.item_max_id()}; KR-only ids never reach the bag)')
        return False

    def _inv_add(self, session, item, count=1, what='grant', words=None):
        """Add to the bag model. Returns the new total for the id, or None when the add was
        refused: an id the EN client does not have (the model must never hold what the
        client silently dropped), an item the client files in no bag tab (a Type 3 skill
        book is learned, not bagged) or a full tab."""
        if not self._grantable_item(item, what):
            return None
        bag = self._bag(session)
        if bag is None:
            return None
        why = bag.fits(item, count, words)
        if why is not None:
            log.info(f'[ITEM] {what}: item {item} x{count} not added - {why}')
            return None
        total = bag.add(item, count, words)
        self.store.mark_dirty(f'{what} {item}')
        return total

    def _inv_remove(self, session, item, count=1, words=None):
        """Remove up to `count` (exact-block match for equipment when `words` is given).
        Returns what is left of the id, mirroring the old helper's contract."""
        bag = self._bag(session)
        if bag is None:
            return 0
        if bag.remove(item, count, words):
            self.store.mark_dirty(f'remove {item}')
        return bag.count(item)

    def _inv_has(self, session, item, count=1, words=None):
        bag = self._bag(session)
        return bag is not None and bag.has(item, count, words)

    # C2S 0x15 use cooldowns, per item id, on the session (item_inventory.md 3.3
    # `session['use_cd']`). The client stamps its own per-SLOT cooldown (scene+0x6F4) when
    # S2C 0x25 arrives, i.e. always LATER than this one, so a legitimate second use always
    # passes here; what this window stops is the spam the client cannot (it sets no lock on
    # send, live-proven in item_inventory#20: two 0x15 for Herb 3.8 s apart inside a 4000 ms
    # CT). Per id rather than per slot is the conservative reading of Q7: the strictest of
    # the two can only refuse, never desync.
    ITEM_COOLDOWN_KEY = 'use_cd'

    def _use_cooldown_left_ms(self, session, item, ct_ms, now=None):
        """Milliseconds left of `item`'s cooldown for this session (0 = usable now)."""
        if ct_ms <= 0:
            return 0
        last = (session.get(self.ITEM_COOLDOWN_KEY) or {}).get(int(item))
        if last is None:
            return 0
        now = time.monotonic() if now is None else now
        return max(0, int(ct_ms - (now - last) * 1000.0))

    def _stamp_use_cooldown(self, session, item, now=None):
        session.setdefault(self.ITEM_COOLDOWN_KEY, {})[int(item)] = \
            time.monotonic() if now is None else now

    def _handle_use_item(self, sock, session, payload, no_enc):
        """C2S 0x15 UseItem (consumable Type-0 branch) {u16 item_id} -> S2C 0x25
        {u16 item_or_skill_id} (item_inventory-use-consumable, F5; B10).

        S2C 0x25 IS the use: the client removes one unit from the bag stack it is showing,
        stamps the cooldown overlay, plays effect/sound 0x29 and applies the hii HP and MP
        DELTAS itself. Without it nothing at all happened client-side - the stack never
        dropped and the cooldown never started, so a potion was infinite (live
        item_inventory#20). The server therefore mirrors exactly one unit and the same
        clamped HP/MP, and sends nothing else:

        - no S2C 0x28/0x44 absolutes. The client adds the delta on top of whatever it holds,
          so an absolute sent BEFORE 0x25 double-applies (spec 0x25 hazard 5) and one sent
          after is only a resync - which would also "heal" a client that dropped the 0x25
          (its own bag/cooldown gate) and then disagree with the bag. The model keeps the
          new absolute for the next 0x03/0x07 (and for the relog of P2 exit criterion 2).
        - no S2C 0x23/0x19 count fix: 0x25 already removed the unit.

        Refusals, and what the client is left showing:
        - not owned: the bag it is showing is ahead of the model, so 0x23 takes the phantom
          unit back and a [Warning] line says why (F5 step 2).
        - inside the server cooldown: nothing. The client would ignore a 0x25 inside its own
          cooldown anyway (spec 0x25 gate 2 - the whole handler aborts, no removal, no heal),
          and a decrement the client never made is a desync until the next portal. This is
          what makes spamming during the cooldown do nothing (P2 exit criterion 5).
        - hii Type 3 (a skill cast, spec 0x44C239/0x15): _handle_cast_skill (cs-skill-cast).
        - hii `Con` != 0 (EN type-0 items 148 and 153): S2C 0x42 adds the buff icon and
          refreshes the stat panel after the 0x25. Proven live (item_inventory#10): item 148
          Con 10000 -> a 10 s countdown in the entity buff record. Observers get 0x41 once
          there are observers (item_inventory-observer-broadcast). cs-buffs records it so
          the server's 0x3C/0x43 ends it on time (C4: the client never does), and a second
          potion while the first buff runs REFRESHES it: identical item buffs stack as two
          records client-side (item_inventory#11), so the old record is removed first.

        Arena/play-room mode ("Not allowed in arena.") is a client-side gate the server
        cannot see yet; it belongs to the pvp group's room model.
        """
        rec, _ = registry.decode(0x15, payload, self.client_build)
        if registry.is_skill_cast(self, rec):
            self._handle_cast_skill(sock, session, rec)
            return
        if rec is not None and 'item_id' in rec:
            item = int(rec['item_id'])
        elif len(payload) >= 2:
            item = struct.unpack_from('<H', payload, 0)[0]
        else:
            log.warning(f'[USE] short payload {len(payload)}B')
            return

        info = en_item(item)
        if info is None:
            log.info(f'[USE] item={item} is not an EN client item - ignoring')
            return
        if info.type != EC.TYPE_CONSUMABLE:
            # Not a consumable; nothing to apply (equip = 0x0F, etc = 2, skill = the branch
            # above). The client never sends these, so there is nothing to answer.
            log.info(f'[USE] item={item} type={info.type} not consumable - ignoring')
            return
        char = self._session_char(session)
        if char is None:
            return
        bag = invmod.Inventory(char)
        # hp/mp read-modify-write under the combat lock (cs-combat-lock): the combat driver
        # and the tickers change the same session.
        with self._combat_lock(session):
            if session.get('dead'):
                # cs-player-death: a corpse drinks nothing (the client ignores input while
                # dead, live combat_skill#19); a 0x25 here would heal a player the server holds
                # dead. Consumables need no reply, so the request is simply dropped.
                log.info(f'[USE] item={item} while dead - ignored')
                return
            if not bag.has(item, 1):
                self._send_phantom_remove(sock, session, item, why='use of an item the bag lacks')
                self._notice(sock, session, "You don't have that item.", 'warn')
                return
            escrow = self._escrow_refusal(session, item, 1)
            if escrow is not None:
                # trade-escrow-guards: no 0x25 (the client's bag view no longer holds an
                # offered unit, the 0x4B self path took it) and no phantom 0x23 either - the
                # unit is still the player's and comes back with a cancel's 0x49.
                log.info(f'[USE] item={item} refused: {escrow[0]}')
                self._notice(sock, session, escrow[1], 'warn')
                return
            left = self._use_cooldown_left_ms(session, item, info.ct)
            if left > 0:
                log.info(f'[USE] item={item} ("{EC.item_name(item)}") refused: {left} ms of the '
                         f'{info.ct} ms cooldown left (the client would ignore the 0x25)')
                return
            # max = what the client computes for itself (cs-hp-mp-model, hpmp.derive), never
            # the stored current value: `session.get('max_hp', char['hp'])` capped a heal at
            # whatever HP the record last held, so a potion above it was lost (P2 finding).
            d = hpmp.refresh(session, char)
            max_hp, max_mp = d.max_hp, d.max_mp
            cur_hp, cur_mp = R.vitals(session, char)
            # The client's own clamp (spec 0x25: "MP += rec+0x15C (clamped 0..max)").
            new_hp = max(0, min(cur_hp + info.hp, max_hp))
            new_mp = max(0, min(cur_mp + info.mp, max_mp))
            session['hp'], session['mp'] = new_hp, new_mp
            bag.remove(item, 1)
            self._stamp_use_cooldown(session, item)
            self.store.mark_dirty(f'use {item}')
            P.send(self, sock, session, '0x25', {'item_or_skill_id': item & 0xFFFF})
            if info.con:
                self._apply_item_buff(sock, session, item)
            # cs-heal-visuals (F9 step 6): observers see the recovery once per use (S2C 0x40
            # plays the heal effects on the user; remote HP is not written by it).
            if info.hp > 0 or info.mp > 0:
                self._send_heal_notice(session, hp=new_hp if info.hp > 0 else None,
                                       mp=new_mp if info.mp > 0 else None)
        log.info(f'[USE] {EC.item_name(item)} item={item} -> 0x25{" + 0x42" if info.con else ""}: '
                 f'stack {bag.count(item) + 1}->{bag.count(item)}, hp {cur_hp}->{new_hp}/{max_hp}, '
                 f'mp {cur_mp}->{new_mp}/{max_mp}, cooldown {info.ct} ms')

    # ========================================================================
    # Skills: learn, cast, utility replies, buffs (cs-skill-learn, cs-skill-cast,
    # cs-utility-skills, cs-buffs; combat_skill.md F1, F3, F4, F5, F10).
    # skills.py owns the records and the learned-list rules, buffs.py the slot records and
    # their deadlines; everything here is validation, the packets and the persistence.
    # ========================================================================
    # Server cooldown tolerance for latency (F3 step 3.7): the client stamps its own CT when
    # the reply LANDS (FUN_00426cc0 in the 0x25 tail), so its next press can reach the
    # server a little before the server's own stamp + CT.
    SKILL_CT_TOLERANCE_MS = 150
    # session key: {family base id: monotonic() of the last accepted cast}. Per family, like
    # the client's +0x7B4 stamps (one per learned slot = one per family). It survives map
    # loads on purpose: the client's stamps die with the entity a map load recreates, and a
    # portal must not reset a 60 s cooldown.
    SKILL_CD_KEY = 'skill_cd'
    # Kinds answered with S2C 0x3B {id, caster}: the slot-inserting ones. A 0x3B to the
    # local uid runs the whole 0x25 self logic as well (cost, cooldown, cast animation,
    # scene+0x258 cleared), so it is the ONLY reply - a 0x25 too would charge the cost twice
    # (spec 0x3B hazard 3, live combat_skill#08). Every other castable kind gets S2C 0x25.
    SKILL_3B_KINDS = frozenset(('self_buff', 'aura', 'summon', 'trap'))
    # The equip Kind whose Job flags FUN_0041ace0 copies into entity+0x112 (weapon class).
    WEAPON_KIND = 11
    # cs-buffs expiry resolution (F8 table: 50 ms; client durations are multiples of 30 ms).
    BUFF_TICK_SECS = 0.05

    def _refuse_skill_cast(self, sock, session, rec, why):
        """F4: S2C 0x5F (0 B) clears scene+0x258 with no effect - no cost, no cooldown, no
        animation - so casting and equip/unequip work again. The bytes are
        registry.MUST_REPLY[0x15], the same refusal the policy sends if a cast handler
        raises. Never before the scene exists: _handle_cast_skill returns first then."""
        skill = int(rec.get('skill_id', rec.get('item_id', 0)))
        where = f' at ({rec["pos_x"]},{rec["pos_y"]})' if 'pos_x' in rec else ''
        sent = registry.send_refusal(self, sock, session, 0x15, rec)
        # 2009: no pending-skill lock and no S2C 0x5F (registry._skill_cast_refusal), so a
        # refused cast is simply not answered.
        log.info(f'[SKILL] cast {skill}{where} refused with {"/".join(sent) or "no reply"}: {why}')

    def _handle_cast_skill(self, sock, session, rec):
        """C2S 0x15 UseSkill {u16 skill_id [, u16 pos_x, u16 pos_y for 0x0A31..0x0A3B]}
        (spec 0x44C239/0x15; cs-skill-cast, combat_skill.md F3).

        On send the client set scene+0x258 = skill_id, which blocks every further cast AND
        every equip/unequip ("You can't equip or unequip while attacking.") until ONE reply
        clears it (B1): S2C 0x25 (accept), S2C 0x3B with the caster's own uid (accept a
        slot-inserting skill) or S2C 0x5F (reject). All three go out from here; the
        MUST_REPLY 0x5F is only the backstop for a handler that raises. The cast-animation
        lock scene+0x255 clears itself within 3 s (C27), so nothing else is owed.

        Validation (F3 step 3, _cast_refusal), any failure = 0x5F and nothing else: in world
        (else NO reply at all: 0x5F derefs the scene without a null check), alive, not a
        mode-1 room map, an EN Type-3 record with Skill_Lv > 0, the EXACT id learned,
        `Job[class] or Job[0]`, the traveler-weapon gate, level >= Lv, MP >= |MP|, HP > |HP|
        for 0x124..0x12E / 0x8DC..0x8E6 (and no HP cost that would kill), and the server
        cooldown per family with SKILL_CT_TOLERANCE_MS. The client checks all but the
        learned / level / cooldown gates before it sends, so a refusal means a desync or a
        forged packet - or a recast right after a portal, whose map load reset the client's
        own stamps but not the server's.

        Accept: the stamp, then exactly the cost the client is about to apply to itself
        from its own record (MP += rec+0x15C; HP += rec+0x158 only when FUN_004258d0), the
        reply by kind, and the ABSOLUTE 0x28 / 0x44 for whatever changed, AFTER the reply
        (F3 step 6, spec 0x25 hazard 5: an absolute sent before the reply would have the
        delta applied on top of it). A type-3 0x25/0x3B has no client-side refusal path, so
        both sides end on the same value and the absolute only pins it.

            self_buff / aura / summon / trap  -> 0x3B {id, caster[, ground x, y]} + buff record
            attack / self_heal / target_heal / party_heal / debuff / utility -> 0x25 {id}
        """
        skill = int(rec.get('skill_id', rec.get('item_id', 0))) & 0xFFFF
        if not session.get('in_world'):
            # S2C 0x5F / 0x25 / 0x3B all deref the scene (and the 0x25 tail [scene+0x970])
            # with no null check (spec 0x5F, 0x25 hazard 4).
            log.warning(f'[SKILL] cast {skill} while not in world - no 0x5F (scene may not exist)')
            return
        char = self._session_char(session)
        now = time.monotonic()
        with self._combat_lock(session):
            sd, why = self._cast_refusal(session, char, skill, now)
            if why is not None:
                self._refuse_skill_cast(sock, session, rec, why)
                return
            hpmp.refresh(session, char)
            old_hp, old_mp = R.vitals(session, char)
            session.setdefault(self.SKILL_CD_KEY, {})[sd.family] = now
            session['mp'] = hpmp.clamp_mp(session, old_mp - sd.mp_cost)
            if sd.caster_hp_delta:
                session['hp'] = hpmp.clamp_hp(session, old_hp + sd.caster_hp_delta)
            # A cast is an action (+0x904 != 8): the idle HP-regen window restarts (F11).
            hpmp.regen_interrupt(session, now, self.config.REGEN_SECS)
            uid = P.session_uid(session) or 0
            buff = None
            if sd.kind in self.SKILL_3B_KINDS:
                reply = '0x3B'
                fields = {'item_or_skill_id': sd.id, 'target_uid': uid}
                x = y = 0
                if buffmod.is_trap(sd.id):
                    # F3g: the ground point comes from the cast (the client's own
                    # +0x15A4/+0x15A8) and 0x3B echoes it.
                    x, y = self._trap_point(session, rec)
                    fields.update(ground_x=x, ground_y=y)
                if sd.slot_on_3b:
                    buff, displaced = buffmod.apply(session, sd.id, now, x=x, y=y)
                    if displaced is not None:
                        # A stacking group (the fairy summons): remove the running record
                        # first, so a recast refreshes it instead of adding a second slot.
                        self._send_buff_removal(sock, session, displaced['id'], uid)
                P.send(self, sock, session, '0x3B', fields)
                if buff is not None and SK.party_buff(sd.id):
                    # FUN_004259c0 copies an aura into +0x107C and FUN_00427d40(.., 0) raises
                    # max HP by its mHP (Breath of Vitality); the current value is unchanged.
                    hpmp.refresh(session, char)
            else:
                reply = '0x25'
                P.send(self, sock, session, '0x25', {'item_or_skill_id': sd.id})
            new_hp, new_mp = session['hp'], session['mp']
            if new_hp != old_hp:
                self._send_hp(sock, session, new_hp)
            if new_mp != old_mp:
                self._send_mp(sock, session, new_mp)
            if reply == '0x3B':
                # F10: other clients on the map see the buff / trap / summon slot on the
                # caster (a no-op on a client that has no entity with that uid yet: remote
                # players spawn with P5 world-registry).
                self._send_to_observers(session, '0x3B', fields)
        log.info(f'[SKILL] {session.get("char_name")!r} cast {sd.id} {sd.name} Lv{sd.level} ({sd.kind}) '
                 f'-> {reply}: MP {old_mp}->{new_mp} HP {old_hp}->{new_hp}, cooldown {sd.ct} ms'
                 + (f', buff {buffmod.remaining_ms(buff, now)} ms ({buffmod.removal_opcode(sd.id)} '
                    f'at the end)' if buff is not None else ''))
        self._skill_effect(sock, session, sd, rec)

    def _cast_refusal(self, session, char, skill, now):
        """(SkillDef or None, why or None) for one C2S 0x15 skill cast (F3 step 3). The
        caller holds the combat lock."""
        if char is None:
            return None, 'no character in this session'
        if session.get('dead'):
            return None, 'the caster is dead (cs-player-death)'
        cur_map = session.get('current_map')
        if cur_map in hpmp.ROOM_MAPS:
            return None, f'map {cur_map} is a PvP room (room casts belong to the pvp group)'
        sd = SK.skill_def(skill)
        if sd is None:
            return None, 'not an EN skill record'
        if not sd.castable:
            # Passives (Dash, Double Jump, ...) and family bases: the client's own gate
            # (Skill_Lv > 0, signed) never sends them.
            return sd, f'{sd.kind} record (Skill_Lv {sd.skill_lv}) is never cast'
        ids = SK.learned(char)
        if sd.id not in ids:
            return sd, f'not learned (learned: {ids or "none"})'
        job1 = int(char.get('class') or 0)
        if not sd.allows_job(job1):
            return sd, f'class {job1} may not use it (Job flags {list(sd.job)})'
        if not sd.job[0] and not self._weapon_class(session, char):
            return sd, ('a class skill needs a class weapon (entity+0x112 = 0: "You can\'t use '
                        'the skill with elementary traveler weapon.")')
        level = R.level_of(char)
        if level < sd.lv:
            return sd, f'needs level {sd.lv} (character is {level})'
        hp, mp = R.vitals(session, char)
        if mp < sd.mp_cost:
            return sd, f'MP {mp} < cost {sd.mp_cost} ("MP low.")'
        if sd.hp_gated and hp <= abs(sd.hp):
            return sd, f'HP {hp} <= {abs(sd.hp)} ("Not enough HP.")'
        if sd.hp_cost and hp <= sd.hp_cost:
            return sd, f'the HP cost {sd.hp_cost} would take the last of HP {hp}'
        last = (session.get(self.SKILL_CD_KEY) or {}).get(sd.family)
        if last is not None and sd.ct > 0:
            left = sd.ct - (now - last) * 1000.0
            if left > self.SKILL_CT_TOLERANCE_MS:
                return sd, f'cooldown: {int(left)} ms of {sd.ct} ms left'
        return sd, None

    def _weapon_class(self, session, char):
        """entity+0x112 as FUN_0041ace0 sets it: the highest class index i > 0 whose Job flag
        is set on the equipped Kind-11 weapon; 0 with no weapon or a traveler-only one. A
        skill without the traveler flag Job[0] needs it non-zero (FUN_0044c090)."""
        for row in R.equip_grid(session, char):
            item = en_item(row['equip_item_id']) if row['equip_item_id'] else None
            if item is None or item.kind != self.WEAPON_KIND:
                continue
            job = list(item.job or [])
            return max((i for i in range(1, len(job)) if job[i]), default=0)
        return 0

    @staticmethod
    def _trap_point(session, rec):
        """(x, y) of a Booby Trap: the u16 pair the cast carried, else the caster's last
        known position (session['pos'], world-persistence)."""
        if 'pos_x' in rec and 'pos_y' in rec:
            return int(rec['pos_x']) & 0xFFFF, int(rec['pos_y']) & 0xFFFF
        pos = session.get('pos') or (0, 0)
        return int(pos[0]) & 0xFFFF, int(pos[1]) & 0xFFFF

    # What an accepted cast does NOT do yet, by kind: the owning items follow in the P3 order
    # and plug in here (the cost, reply, cooldown and buff record are done). Attack, heal and
    # debuff effects are P3 stage 4 (the handlers below _skill_effect).
    SKILL_EFFECT_OWNERS = {
        'trap': 'armed; the C2S 0x6C trigger + its 0x0D victim resolve it (cs-traps, F7)',
        'summon': '60 s slot only; pet behaviour is untraced (combat_skill.md Q9)',
    }
    # kind -> the P3 stage 4 effect handler (cs-skill-damage, cs-heal-visuals, cs-debuffs).
    SKILL_EFFECT_HANDLERS = {
        'attack': '_skill_attack',
        'self_heal': '_skill_self_heal',
        'target_heal': '_skill_target_heal',
        'party_heal': '_skill_party_heal',
        'debuff': '_skill_debuff',
        'aura': '_skill_aura',                  # cs-party-skills (P6 stage 4)
    }

    def _skill_effect(self, sock, session, sd, rec):
        """cs-utility-skills, and the hand-off seam for the kinds whose effect belongs to
        later items. The utility skills are complete with their S2C 0x25: 0xC2 Open Stall
        opens the client's stall window 0x259 (C2S 0x5E/0x5F/0x60 are shop_storage's stall
        handlers), 0x876..0x88B Mineral Refining / Concoction open the crafting UI (C2S 0x67
        is item_inventory's) and 0x88C Reinforce opens the Reinforce window next to a smithy
        or shows "Too far for Reinforcement." (C27; C2S 0x68). Dash and Double Jump are
        passives: never cast, they work client-side from the learned list the 0x07 record
        carries (cs-skill-learn), which is why losing them on relog was the bug."""
        if sd.kind == 'utility':
            what = ('stall window 0x259 (shop_storage stall handlers)' if sd.id == 0xC2 else
                    'Reinforce window / smithy check (C2S 0x68, item_inventory)' if sd.id == 0x88C else
                    'crafting window (C2S 0x67, item_inventory)')
            log.info(f'[SKILL] {sd.name} ({sd.id}): the client opens its {what}')
            return
        handler = self.SKILL_EFFECT_HANDLERS.get(sd.kind)
        if handler is not None:
            getattr(self, handler)(sock, session, sd)
            return
        owner = self.SKILL_EFFECT_OWNERS.get(sd.kind)
        if owner:
            log.info(f'[SKILL] {sd.name} ({sd.id}, {sd.kind}): {owner}')

    # ---- buffs (cs-buffs) ----
    def _skill_observers(self, session):
        """Other in-world sessions on the caster's map (F10: never back to the source):
        world.peers, the registry's map audience."""
        return self.world.peers(session)

    def _push(self, target, key, fields, tag='WORLD', assume=None, flush=None):
        """One S2C to ANOTHER session (party-mp-registry `_send_to`, social_friend `_push`,
        chat_mail_gm-online-registry). Built for the target (its uid for receiver-dependent
        forms, its encoding) and queued on its outbox, so it never blocks the caller (see
        _send_encrypted). A closed target is logged, never raised: the sender is another
        player's handler or the tick thread. Returns True when the packet was queued.
        flush=False also queues a packet to the calling connection's OWN session instead of
        writing it (the messenger sends under its lock, P6 stage 2)."""
        sock = target.get('sock')
        if sock is None:
            return False
        try:
            P.send(self, sock, target, key, fields, assume, **({} if flush is None else {'flush': flush}))
            return True
        except OSError as e:
            log.debug(f'[{tag}] {key} to {target.get("char_name") or target.get("username")!r} '
                      f'not sent: {e}')
            return False

    def _send_to_observers(self, session, key, fields):
        """Send one S2C about `session`'s own entity to its observers (combat_skill F10,
        cs-observer-sync P6 stage 4): buff slot 0x3B / removal 0x43 / 0x3C, heal visual 0x40,
        skill learned 0x57, death 0x29, 0x58. Never back to the source.

        The audience is presence.to_holders - the map peers whose client HOLDS the entity -
        not every map peer: each of these packets changes (or shows) that entity, and every
        one of them is also in the spawn record a later peer gets (buff rows, skill list, cur
        HP, the corpse), so touch() makes a record that was built before this change and is
        still in flight rebuild with it instead of the peer missing the change for good (a
        0x3B that reached a peer before its 0x05 was a lost slot; a 0x43 before it, a slot
        the copy kept for ever - C4). Returns the number sent."""
        return presence.to_holders(self, session, key, fields, 'SKILL')

    def refuses(self, target, kind, actor=None):
        """chat_mail_gm-privacy-flags consumer API (D13): does `target` refuse `kind`
        ('whisper' / 'exchange' / 'party' / 'talk' / 'friend') from `actor`? target is a
        session, or a character name - the online session's live flags, else the stored
        record's (privacy.py lists the reply each consumer sends)."""
        if isinstance(target, str):
            found = self.world.by_char_name(target)
            if found is None:
                stored = self.store.character_by_name(target)
                found = stored[2] if stored else None
            target = found
        return privacy.refuses(target, kind, actor)

    def _send_buff_removal(self, sock, session, item_id, uid=None, observers=True):
        """End one of this session's own slots: S2C 0x43 {id, uid} when the slot changes
        stats (removal + FUN_0041ace0 recompute + HUD refresh), S2C 0x3C otherwise (removal
        only; a trap's slot also queues event 0xE2 at its x,y). Live combat_skill#09/#10:
        0x3C on a stat slot leaves the bonus behind, 0x43 takes it away. Returns the key.

        The observers need the removal too (spec correction C4: no client ends a buff by
        itself): skill slots and item buffs alike go through presence.to_holders like their
        0x3B / 0x41 (_send_to_observers, cs-observer-sync) - touch() makes a spawn record
        built before this removal (its buff rows still carrying the slot) rebuild without it,
        so no copy of the player keeps the buff."""
        uid = (P.session_uid(session) or 0) if uid is None else uid
        key = buffmod.removal_opcode(item_id)
        fields = {'buff_item_id': int(item_id) & 0xFFFF, 'target_uid': int(uid) & 0xFFFFFFFF}
        P.send(self, sock, session, key, fields)
        if observers:
            self._send_to_observers(session, key, fields)
        return key

    def _apply_item_buff(self, sock, session, item):
        """S2C 0x42 for a consumable with a hii Con (EN items 148 and 153), recorded as a
        cs-buffs slot so the server ends it on time. Identical item buffs STACK client-side
        (item_inventory#11: two records, two icons), so a running one is removed first and
        the new use starts a full Con - a refresh, never two records. The caller holds the
        combat lock."""
        buff, displaced = buffmod.apply(session, item, time.monotonic())
        if displaced is not None:
            # The observers hold the old record too (their 0x41 below, or the buff rows of
            # the record they spawned this player from), and identical item buffs stack there
            # as well: the removal goes to them first, like the owner's.
            self._send_buff_removal(sock, session, displaced['id'])
            log.info(f'[BUFF] item buff {item} refreshed (old record removed first: identical '
                     f'item buffs stack client-side)')
        self._send_item_buff(sock, session, item)
        # item_inventory F5 step 6 (observer-broadcast, P5 stage 4): the clients that hold this
        # player get S2C 0x41 with the same body - the buff record on their copy (and its stat
        # recompute) without the owner's stat-panel refresh of 0x42. Expiry reaches them with
        # the same 0x3C/0x43 as the owner (_expire_buffs -> _send_buff_removal ->
        # presence.to_holders), or identical item buffs would stack on their copy until the
        # 21-slot buff array is full.
        presence.to_holders(self, session, '0x41',
                            invmod.item_buff_fields(P.session_uid(session) or 0, item, 0), 'BUFF')

    def _tick_buffs(self, now=None):
        """cs-buffs expiry (F5; spec correction C4: the client never ends a buff itself) and
        Berserk's self-damage, every BUFF_TICK_SECS on the tick scheduler (world lock held).

        For every in-world session, each record whose deadline passed gets its removal
        (_send_buff_removal: 0x43 for a stat slot, 0x3C otherwise), and an aura that carried
        mHP clamps the current HP to the lower maximum, with an 0x28 if it moved (F5 step
        2: the 0x43 recompute gate only checks the Skill_P/A columns, but FUN_00425af0
        re-runs FUN_00427d40(.., 0), the clamp). A session in a map load is skipped: the load
        drops expired records itself and the new entity never gets them.

        cs-party-skills (P6 stage 4): the due Healing Aura ticks of each holder heal it and
        its same-map party (_aura_heal, after its lock is released: one member's combat lock
        at a time, never nested), and every party member's aura cover follows the auras its
        same-map members run (_tick_party_auras). Returns the number of packets sent (tests
        drive it with an explicit clock)."""
        now = time.monotonic() if now is None else now
        sent = 0
        for session in list(self.sessions.values()):
            if not session.get('buffs') or not session.get('in_world') or session.get('sock') is None:
                continue
            heals = []
            try:
                with self._combat_lock(session):
                    heals = buffmod.due_aura_heals(session, now)
                    sent += self._expire_buffs(session, now)
            except OSError as e:
                # The socket died under the tick; its own connection thread cleans up.
                log.debug(f'[BUFF] {session.get("char_name")!r}: send failed ({e})')
            for buff, amount in heals:
                sent += self._aura_heal(session, buff, amount)
        sent += self._tick_party_auras(now)
        return sent

    # ---- party auras (cs-party-skills, combat_skill.md F3f; P6 stage 4) ----
    # An aura / group buff (0x8E7..0x912, 0xAE3..0xAF8) is ONE slot on the caster (S2C 0x3B
    # {id, caster} to the caster and to every client holding him, cs-observer-sync); each of
    # those clients fans it out to the party members in its scene (FUN_00424e20 returns
    # non-zero -> FUN_004259c0 into +0x107C). The server mirrors the fan-out as each member's
    # session['aura_cover'] (buffs.cover_of): its modifiers count in the damage formula
    # (buffs.modifiers) and its mHP in the member's max HP (buffs.sync_party_slots ->
    # hpmp.slot_mhp: Breath of Vitality raises the whole party's maximum). Coverage = same
    # party, same map, alive - recomputed every BUFF_TICK_SECS, so a cast, an expiry, a join,
    # a leave, a portal or a death all land within 50 ms without a hook per event; the cast
    # (_skill_aura), a join and a leave (_auras_after_join / _auras_after_leave) also apply
    # it at once. Whether a member's client re-runs the fan-out for an aura that was already
    # running when it joined is open (combat_skill.md Q5): the model covers it anyway.
    def _aura_snapshot(self, session, now):
        """buffs.party_auras of one member, read under its own combat lock (never nested)."""
        with self._combat_lock(session):
            return buffmod.party_auras(session, now)

    def _sync_aura_cover(self, session, now, snapshots=None):
        """Recompute one member's aura cover from its same-map, living party members (none
        outside a party, dead or out of the world) and, when it changed, its slot table and
        maxima: a lost mHP aura clamps the current HP (S2C 0x28 when it moved), a gained one
        raises only the maximum (FUN_00427d40(.., 0), as the caster's own slot does). Returns
        the packets sent; 0 when nothing changed."""
        snapshots = {} if snapshots is None else snapshots
        if (not worldmod.reachable(session) and session.get('entered_once') and session.get('sock') is not None
                and not session.get('closed') and not session.get('kicked')):
            # A map load in flight: its 0x03 / 0x07 carry the HP computed with the cover it
            # had; decide once it is on the new map (the next tick), never mid-load.
            return 0
        others = []
        if worldmod.reachable(session) and not session.get('dead'):
            for m in self.party.members(session, same_map=True, include_self=False, alive=True):
                if id(m) not in snapshots:
                    snapshots[id(m)] = self._aura_snapshot(m, now)
                others.append(snapshots[id(m)])
        sent = 0
        with self._combat_lock(session):
            own = buffmod.party_auras(session, now)
            cover = buffmod.cover_of(own, others) if others else []
            old = session.get('aura_cover') or []
            if [(c['id'], c['src']) for c in old] == [(c['id'], c['src']) for c in cover]:
                return 0
            if cover:
                session['aura_cover'] = cover
            else:
                session.pop('aura_cover', None)
            buffmod.sync_party_slots(session)
            char = self._session_char(session)
            hp_before = session.get('hp')
            if char is not None:
                hpmp.refresh(session, char)
                if session.get('hp') != hp_before and worldmod.reachable(session):
                    sent += bool(self._push(session, '0x28', {'hp': session['hp']}, 'AURA'))
        log.info(f'[AURA] {session.get("char_name")!r} aura cover '
                 f'{[c["id"] for c in old]} -> {[c["id"] for c in cover]} (from uid(s) '
                 f'{sorted({c["src"] for c in cover})}): HP {session.get("hp")}/{session.get("max_hp")}')
        return sent

    def _tick_party_auras(self, now=None):
        """Every party member's cover, and anyone who still carries one after leaving a party
        (_sync_aura_cover). Returns the packets sent (0x28 clamps)."""
        now = time.monotonic() if now is None else now
        with self.party.lock:
            targets = {id(m): m for p in self.party.parties.values() for m in p.members}
        for s in list(self.sessions.values()):
            if s.get('aura_cover'):
                targets.setdefault(id(s), s)
        snapshots, sent = {}, 0
        for s in targets.values():
            try:
                sent += self._sync_aura_cover(s, now, snapshots)
            except OSError as e:
                log.debug(f'[AURA] {s.get("char_name")!r}: send failed ({e})')
        return sent

    def _skill_aura(self, sock, session, sd):
        """An accepted aura / group buff (kind 'aura'; its 0x3B went to the caster and to the
        clients holding him, whose party fan-out does the rest client-side): the caster's
        same-map party members are covered at once (_sync_aura_cover) instead of at the next
        tick. Healing Aura then heals the caster and them every 5010 ms (_aura_heal)."""
        now = time.monotonic()
        members = self.party.members(session, same_map=True, include_self=False, alive=True)
        snapshots = {id(session): self._aura_snapshot(session, now)}
        for m in members:
            self._sync_aura_cover(m, now, snapshots)
        log.info(f'[AURA] {session.get("char_name")!r} {sd.name} ({sd.id}): covers '
                 f'{[m.get("char_name") for m in members] or "no party member on the map"}'
                 + (f', Healing Aura +{buffmod.aura_heal(sd.id)} HP every '
                    f'{buffmod.AURA_HEAL_PERIOD_SECS:g} s' if buffmod.is_healing_aura(sd.id) else ''))

    def _aura_heal(self, holder, buff, amount):
        """One Healing Aura tick (FUN_00417e10, host-only so the field client never runs it,
        combat_skill.md F3f step 3): +amount HP (clamped) to the holder and each LIVING
        party member on its map - the absolute S2C 0x28 to each whose HP moved (no heal
        visual: that is for received heals, never per tick); the party frames follow with
        the vitals tick (0x54). One member's combat lock at a time. Returns the packets sent."""
        if amount <= 0:
            return 0
        sent, healed = 0, []
        for m in self.party.members(holder, same_map=True, include_self=True, alive=True):
            with self._combat_lock(m):
                if m.get('dead') or not worldmod.reachable(m):
                    continue
                old = int(m.get('hp') or 0)
                new = hpmp.clamp_hp(m, old + amount)
                if new == old:
                    continue
                m['hp'] = new
                sent += bool(self._push(m, '0x28', {'hp': new}, 'AURA'))
                healed.append(f'{m.get("char_name")} {old}->{new}')
        log.info(f'[AURA] {EC.item_name(buff["id"])} ({buff["id"]}) of {holder.get("char_name")!r}: '
                 f'+{amount} HP -> {", ".join(healed) or "nobody hurt"}')
        return sent

    def _auras_after_leave(self, leaver, party, reason, others=()):
        """party-skill-hooks leave listener (F3f step 4): a member left - Break Party, or a
        disconnect / leaving the world. While the leaver is still in the world on the map of
        a remaining member, each side's running auras stop covering the other: S2C 0x43
        {id, caster} for each of the leaver's auras to that member and for each of the
        member's auras to the leaver (FUN_00425af0 re-evaluates the auras, the HUD refreshes).
        A disconnected leaver's copy is already gone from their screens (its 0x06), so a
        removal about it would find no entity. Then both sides' covers are recomputed at once."""
        now = time.monotonic()
        sent = 0
        if worldmod.reachable(leaver):
            mine = self._aura_snapshot(leaver, now)
            here = self.world.map_of(leaver)
            for m in others:
                if not worldmod.reachable(m) or self.world.map_of(m) != here:
                    continue
                for a in mine:
                    sent += bool(self._push(m, buffmod.removal_opcode(a['id']),
                                            {'buff_item_id': a['id'], 'target_uid': a['src']}, 'AURA'))
                for a in self._aura_snapshot(m, now):
                    sent += bool(self._push(leaver, buffmod.removal_opcode(a['id']),
                                            {'buff_item_id': a['id'], 'target_uid': a['src']}, 'AURA'))
        snapshots = {}
        for s in [leaver, *others]:
            self._sync_aura_cover(s, now, snapshots)
        if sent:
            log.info(f'[AURA] {leaver.get("char_name")!r} left the party ({reason}): {sent} aura removal(s)')

    def _auras_after_join(self, joiner, party):
        """party-skill-hooks join listener: the joiner and the members cover each other at
        once (the next tick would too)."""
        now = time.monotonic()
        snapshots = {}
        for s in list(party.members):
            self._sync_aura_cover(s, now, snapshots)

    def _expire_buffs(self, session, now):
        """One session's due Berserk ticks and expired records (caller holds the lock)."""
        sock = session['sock']
        sent = 0
        for buff, dmg in buffmod.due_berserk(session, now):
            hp = int(session.get('hp') or 0)
            new = max(1, hp - dmg)
            if new != hp:
                session['hp'] = new
                self._send_hp(sock, session, new)
                sent += 1
                log.info(f'[BUFF] Berserk {buff["id"]}: self-damage HP {hp}->{new}')
        gone = buffmod.pop_expired(session, now)
        if not gone:
            return sent
        uid = P.session_uid(session) or 0
        for buff in gone:
            # Observers included, item buffs too (their 0x41 put the slot on every copy).
            key = self._send_buff_removal(sock, session, buff['id'], uid)
            sent += 1
            log.info(f'[BUFF] {session.get("char_name")!r}: {EC.item_name(buff["id"])} ({buff["id"]}) '
                     f'expired -> {key}')
        if any(SK.party_buff(b['id']) for b in gone):
            char = self._session_char(session)
            if char is not None:
                before = session.get('hp')
                hpmp.refresh(session, char)
                if session.get('hp') != before:
                    self._send_hp(sock, session, session['hp'])
                    sent += 1
        return sent

    # ---- learning (cs-skill-learn) ----
    def _learn_skill(self, session, skill_id, *, check=True, notify=True, what='learn'):
        """Record a learned skill in the character (skills.learn: the client's own
        FUN_00426b80 family rule) and persist it, so the 0x07 skill list of every later map
        load and relog carries it.

        notify=True  sends S2C 0x18 {gold, victy, skill_id, 1}: the client learns it itself
                     ("You've learned skill(%s).", skill window and quick slots refreshed).
                     Only in world: FUN_00440920 case 3 needs [scene+0x970].
        notify=False mirrors a grant the client has ALREADY applied on its own (quest
                     reward S2C 0x27, live quest_cards_misc#11).
        check        the level / class gates (shop, dev !learn); a mirror never checks,
                     because the client does not either.
        Observers get S2C 0x57 {uid, skill} (F10). Returns the skills.Learned result."""
        char = self._session_char(session)
        if char is None:
            return SK.Learned(False, skill_id, None, 'no character in this session')
        with self.store.lock:
            res = SK.learn(char, skill_id, level=R.level_of(char), check=check)
        if not res.ok:
            log.info(f'[SKILL] {what}: {skill_id} not learned by {char.get("name")!r}: {res.why}')
            return res
        self.store.mark_dirty(f'{what} skill {res.id}')
        log.info(f'[SKILL] {what}: {char.get("name")!r} learned {res.id} {EC.item_name(res.id)}'
                 + (f' (replaces {res.replaced})' if res.replaced else '')
                 + f'; skills {SK.learned(char)}')
        sock = session.get('sock')
        if sock is not None and session.get('in_world'):
            if notify:
                P.send(self, sock, session, '0x18',
                       invmod.currency_fields(self._wallet(session), res.id, 1))
            self._send_to_observers(session, '0x57', {'uid': P.session_uid(session) or 0,
                                                      'skill_item_id': res.id})
        return res

    @staticmethod
    def _learn_refusal_text(why):
        """The player-facing line for a refused skill purchase (the client's own wording
        where it has one: FUN_00467680 "You already have learned this skill.")."""
        if why.startswith('already'):
            return 'You already have learned this skill.'
        if why.startswith('needs level'):
            return 'Your level is too low to learn this skill.'
        if why.startswith('class'):
            return 'Your class cannot learn this skill.'
        if why.startswith('all'):
            return 'You cannot learn any more skills.'
        return 'You cannot learn that skill.'

    def _buy_skill(self, sock, session, item, wallet, gold_cost, victy_cost):
        """C2S 0x0B for a Type-3 record: a skill master's "skill book" (combat_skill.md F1,
        roadmap B7/B8). _handle_buy_item has already checked that the NPC's row shows this
        exact id; the learn gates of FUN_00467680 (Lv, class, "You already have learned this
        skill.") are re-checked against the persisted model, hii Buy and PMoney are charged
        once (a skill has no quantity and no discount) and S2C 0x18 {gold, victy, skill_id, 1}
        makes the client learn it. It files no bag entry anywhere, so neither does the model
        (P4 exit criterion 1: the skill window gains it, the bag does not)."""
        char = self._session_char(session)
        sd = SK.skill_def(item)
        why = SK.learn_refusal(char, sd, R.level_of(char))
        if why is not None:
            log.info(f'[BUY] skill {item} refused: {why}')
            self._send_shop_refusal(sock, session, self._learn_refusal_text(why))
            return
        with self._combat_lock(session):
            if not wallet.pay(gold_cost, victy_cost):
                log.info(f'[BUY] skill {item} cost={gold_cost} gold + {victy_cost} Victy > '
                         f'wallet {wallet.gold}/{wallet.victy}; refusing')
                short = 'Victy' if wallet.victy < victy_cost else 'gold'
                self._send_shop_refusal(sock, session, f'Not enough {short}.')
                return
            self._wallet_commit(session, wallet, 'buy skill')
            # The gates passed above; the learn mirrors what the 0x18 does client-side.
            res = self._learn_skill(session, item, check=False, notify=True, what='buy')
            if not res.ok:                     # raced another learn of the same family
                wallet.earn(gold_cost, victy_cost)
                self._wallet_commit(session, wallet, 'buy skill refund')
                self._send_shop_refusal(sock, session, self._learn_refusal_text(res.why))
                return
        log.info(f'[BUY] skill {sd.name} {item} cost={gold_cost} gold + {victy_cost} Victy '
                 f'-> gold={wallet.gold} victy={wallet.victy}')

    # ---- class change (lc-class-change; login_character.md F8, 3.5.5) ----
    # The class lives in the record as `class` (+0x110) and `job2` (+0x111, the tier). The
    # 0x02 character list, the own 0x07 and the remote 0x04/0x05 records all read it from
    # there (records.py), so persisting it is what makes the select screen and the HUD of
    # every later map load and relog agree with the popup the client showed.
    def _apply_job_item(self, session, item_id, *, notify=True, what='job item'):
        """Apply a Type-4 job item to this session's character with FUN_004249E0's own rule
        (classchange.apply) and persist the new class/job2. Every class-change source goes
        through here: `!job`, `!give`, a shop buy and the quest reward mirror.

        notify=True  sends the owner S2C 0x18 {gold, victy, item, 1} (absolute wallet): the
                     client applies it, recomputes the maxima on a base class, redraws the
                     HUD and pops "<class> is your new class." - no bag entry (live
                     login_character#17). Only in world: FUN_00440920 case 4 needs the local
                     player, so the client would drop it and the record would be ahead.
        notify=False mirrors a change the client has ALREADY applied (quest reward S2C 0x27:
                     FUN_00440920 runs the same case 4 on it).
        Observers get S2C 0x58 {uid, class, tier} (assume table: they hold the entity); it
        sets the two bytes and recomputes only at tier 0 (spec 0x58). A refused item changes
        nothing on either side (the client refuses it silently too). Returns the
        classchange.Change."""
        char = self._session_char(session)
        if char is None:
            return CC.Change(False, int(item_id), 0, 0, (0, 0), 'no character in this session')
        sock = session.get('sock')
        live = sock is not None and bool(session.get('in_world'))
        if notify and not live:
            return CC.Change(False, int(item_id), *CC.of(char), CC.of(char),
                             'not in world (the client applies a job item only to its local player)')
        with self.store.lock:
            res = CC.apply(*CC.of(char), item_id)
            if res.ok:
                char['class'], char['job2'] = res.cls, res.tier
        if not res.ok:
            log.info(f'[CLASS] {what}: {EC.item_name(item_id)} ({item_id}) not applied to '
                     f'{char.get("name")!r}: {res.why}')
            return res
        self.store.mark_dirty(f'{what} class {res.cls}/{res.tier}')
        # FUN_00440BC0 recomputes the maxima of a base-class change and heals nothing (live
        # #17: max 418 -> 654, current kept); a tier change indexes no HP/MP table. The
        # refresh is a clamp either way, so it is safe for both.
        with self._combat_lock(session):
            hpmp.refresh(session, char)
        log.info(f'[CLASS] {what}: {char.get("name")!r} {CC.class_name(*res.old) or res.old} -> '
                 f'{CC.class_name(res.cls, res.tier) or (res.cls, res.tier)} (class {res.cls} tier '
                 f'{res.tier}) via {EC.item_name(item_id)} ({item_id})'
                 + ('' if notify else ' - mirrored, the client applied it itself'))
        if live:
            if notify:
                P.send(self, sock, session, '0x18',
                       invmod.currency_fields(self._wallet(session), res.item, 1))
            self._send_to_observers(session, '0x58', {'uid': P.session_uid(session) or 0,
                                                      'class': res.cls, 'class_tier': res.tier})
        return res

    def _set_class_silently(self, session, char, cls, tier, what='class fix-up'):
        """The GM fix-up of login_character F8 step 3: set (class, tier) in the record and
        send S2C 0x58 {own uid, class, tier} to the owner and its observers - no popup and no
        FUN_004249E0 check. Live login_character#16: the two bytes change and a tier-0 value
        recomputes the maxima, but neither the status window nor the HUD label redraw until
        something else redraws them, so a caller that ends here (`!job novice`) follows with
        _send_stat_redraw. Only for dev commands: a player never gets a class without an item."""
        with self.store.lock:
            char['class'], char['job2'] = int(cls) & 0xFF, int(tier) & 0xFF
        self.store.mark_dirty(f'{what} class {cls}/{tier}')
        with self._combat_lock(session):
            hpmp.refresh(session, char)
        sock = session.get('sock')
        if sock is None or not session.get('in_world'):
            return
        fields = {'uid': P.session_uid(session) or 0, 'class': int(cls) & 0xFF,
                  'class_tier': int(tier) & 0xFF}
        P.send(self, sock, session, '0x58', fields)
        self._send_to_observers(session, '0x58', fields)
        if (int(cls), int(tier)) == CC.NOVICE:
            self._send_stat_redraw(sock, session, char)
        log.info(f'[CLASS] {what}: {char.get("name")!r} set to class {cls} tier {tier} '
                 f'({CC.class_name(cls, tier) or "?"}) by S2C 0x58 to self')

    def _send_stat_redraw(self, sock, session, char):
        """S2C 0x14 {uid, 0, STR}: the absolute STR the client already holds. It changes
        nothing, but a 0x14 for the local player redraws the status window and the HUD class
        label, which 0x58 does not (live login_character#16)."""
        value = int(char.get(self.STAT_KEYS[0]) or 0) & 0xFFFF
        P.send(self, sock, session, '0x14', {'uid': P.session_uid(session) or 0,
                                             'stat_index': 0, 'value': value})

    def _buy_job_item(self, sock, session, item, wallet, gold_cost, victy_cost):
        """C2S 0x0B for a Type-4 record: a job item is applied on receipt, never bagged, so the
        buy is the class change itself. FUN_004249E0's rule is checked BEFORE the charge: the
        old path charged the price, sent the 0x18 and recorded nothing, so the class the
        client applied was gone after the next map load - and an item the client refused
        silently was paid for anyway. No EN merchant stocks a job item (EN hands them out as
        quest rewards: quests 8, 11-15, 166-221), so the shop-list check in _handle_buy_item
        refuses every retail request before this runs."""
        char = self._session_char(session)
        why = CC.refusal(*CC.of(char), item) if char is not None else 'no character'
        if why is not None:
            log.info(f'[BUY] job item {item} refused: {why}')
            self._send_shop_refusal(sock, session, 'You cannot change to that class.')
            return
        with self._combat_lock(session):
            if not wallet.pay(gold_cost, victy_cost):
                log.info(f'[BUY] job item {item} cost={gold_cost} gold + {victy_cost} Victy > '
                         f'wallet {wallet.gold}/{wallet.victy}; refusing')
                self._send_shop_refusal(sock, session, 'Not enough gold.')
                return
            self._wallet_commit(session, wallet, 'buy job item')
            res = self._apply_job_item(session, item, notify=True, what='buy')
            if not res.ok:                     # raced another class change
                wallet.earn(gold_cost, victy_cost)
                self._wallet_commit(session, wallet, 'buy job item refund')
                self._send_shop_refusal(sock, session, 'You cannot change to that class.')
                return
        log.info(f'[BUY] job item {EC.item_name(item)} {item} cost={gold_cost} -> gold={wallet.gold}')

    # ========================================================================
    # P3 stage 4: skill damage, heal visuals, targeted debuffs, traps, player death and
    # revive (cs-skill-damage, cs-heal-visuals, cs-debuffs, cs-traps, cs-player-death;
    # combat_skill.md F3a/F3b/F3c/F3h/F7/F8). combat.py holds the hitbox / damage / penalty
    # / revive-point rules and debuffs.py the monster slots; everything here runs under the
    # session's combat lock and sends the packets.
    #
    # Monsters are shared per map since world-shared-monsters (P5 stage 3): every client on
    # the map holds the same uid. The slot packets 0x41 and its removal still go to the caster
    # only (their observer broadcast is cs-observer-sync, P6); the 0x2A hit release goes to
    # the attacker only and the others get the hit relay (_release_hit_lock).
    # ========================================================================
    # C2S 0x0D interact event (state_lo bits 16-19 = +0x950) of a Booby Trap catch: its tail
    # names the victim (target_uid) and the trap (item_id) - FUN_00416380 @0x417CE0, live
    # combat_skill#22 (45 B tail).
    TRAP_INTERACT_EVENT = 0xC
    # How long a C2S 0x6C waits for that 0x0D before the fallback victim search (live #22:
    # the 0x0D followed the 0x6C 31 ms later).
    TRAP_PAIR_SECS = 0.5
    # A C2S 0x0D action 0xD this soon after a revive is the old corpse's report, not a death.
    REVIVE_GRACE_SECS = 3.0
    # Minimum time between two SWING hits (events 4/5, 7..10) of one monster on one player
    # (config MOB_CONTACT_DAMAGE), a per-(victim, monster) slot (_swing_slot) like body
    # contact's (events 1/6, config MOB_CONTACT_MIN_SECS, _contact_slot): sharing one slot
    # let contact take nearly every one (livetest bug 2: 441 contact hits against 5 swings
    # while a chasing mob walked back and forth through the player), and one swing slot per
    # victim dropped the second of two monsters swinging within 0.5 s - whose digit and
    # flinch the victim's client had already drawn, with no HP taken (review of bug 2).
    MOB_SWING_MIN_SECS = 0.5
    # Vampiric Attack (0xB25..0xB2F) is in FUN_004258d0's "HP is not the caster's" list, so it
    # classifies as a target heal; policy (combat_skill.md Q4): it is a drain - it hits the
    # monsters in front and its HP column heals the caster.
    VAMPIRIC_RANGES = SK._ranges((0xB25, 0xB2F))

    def _skill_origin(self, session):
        """((x, y), facing) a skill hitbox is laid from: the position and facing the combat
        driver (+0x11F8/+0x1288/+0x8BD) and C2S 0x0D (interact tails, state bits 0-1) last
        gave, else the last arrival point, facing right."""
        pos = session.get('pos') or (float(self.config.START_X), float(self.config.START_Y))
        facing = session.get('facing') or combat.FACING_RIGHT
        return (float(pos[0]), float(pos[1])), facing

    def _caster_attack(self, session, char):
        """(attack, the running buffs' Skill_P_A) for the placeholder combat.skill_damage
        (DAMAGE_FORMULA 'placeholder'): Increase Attack Power and Berserk make skills hit
        harder while they run (buffs.modifiers)."""
        return combat.attack_power(char), buffmod.modifiers(session).p_a

    # ---- the damage formula (config DAMAGE_FORMULA, damage.py) ----
    def _client_damage(self):
        """True: hits use the client's own formula (damage.py, the default); False: the old
        placeholder rules of combat.py (DAMAGE_FORMULA 'placeholder', for rollback)."""
        return self.config.get('DAMAGE_FORMULA', 'client') != 'placeholder'

    def _player_stats(self, session, char=None):
        """The session's player as his client derives him (FUN_0041b830: class, level, stats,
        the regular equipment slots, the running buffs, the Evasion passive)."""
        char = self._session_char(session) if char is None else char
        return dmgmod.player_stats(char or {}, session)

    @staticmethod
    def _hit_text(event, hit):
        return f'[event {event}: B {hit.b} + E {hit.e}' + (f', {hit.note}' if hit.note else '') + ']'

    # The grade roll's dice (config DAMAGE_GRADE_ROLL; random.Random API). An instance
    # attribute in tests pins the rolls (a seeded random.Random).
    DAMAGE_RNG = random

    def _grade_roller(self):
        """damage.grade_roll on DAMAGE_RNG while config DAMAGE_GRADE_ROLL is on, else None.
        livetest bug 10: the 2009 client patch (combo HUD v2) rolls the digit it DRAWS
        (CRITICAL! x1.5 5%, BAD x0.75 13%, GOOD x1.25 13%, x0.9..1.1 jitter) while the server
        took the unrolled value, so kill counts never varied as the digits did. The server now
        rolls the same table on a player's hit on a monster - client-reported swings and
        strong attacks, attack skills, traps, the detonation release - never on DoT ticks,
        reflections or a monster's hit on a player. The digit and the server's roll are
        INDEPENDENT draws with the same distribution and mean (damage.GRADE_MEAN), not the
        same number per hit. The default (None, auto: config.grade_roll_on) rolls only for
        the 2009 build: a 2008 exe draws the exact unrolled value, so rolling there would
        make the HP taken disagree with the digit (bug 10 in reverse)."""
        if not cfgmod.grade_roll_on(self.config):
            return None
        rng = self.DAMAGE_RNG
        return lambda value: dmgmod.grade_roll(value, rng)

    def _graded(self, value, what=''):
        """The placeholder formula's value through the same roll (DAMAGE_FORMULA
        'placeholder' has no damage() call to put it in)."""
        roll = self._grade_roller()
        if roll is None:
            return value
        grade, rolled = roll(max(1, int(value)))
        if grade != dmgmod.GRADE_NONE:
            log.debug(f'[DMG] placeholder {what} {grade}: {value} -> {rolled}')
        return rolled

    def _formula_hit(self, session, mob, event, skill=None, *, char=None, now=None):
        """One hit of the session's player on `mob` by the client's formula (damage.damage),
        capped at the mob's current HP and rolled (_grade_roller, config DAMAGE_GRADE_ROLL).
        An event-9 fire skill hit opens the player's 720 ms fire window
        (session['fire_window_t']); a later fire hit inside it gets B = 0 and E / 3, as the
        client's +0xE14 window does. Caller holds the session's combat lock."""
        now = time.monotonic() if now is None else now
        att = self._player_stats(session, char)
        t0 = session.get('fire_window_t')
        window = t0 is not None and now - t0 < dmgmod.FIRE_WINDOW_SECS
        vic = dmgmod.monster_stats(mob)
        hit = dmgmod.damage(att, vic, event, skill, cap=mob.hp, splash=window, roll=self._grade_roller())
        if hit.fire and not window:
            session['fire_window_t'] = now
        log.debug(f'[DMG] {session.get("char_name")!r} ({dmgmod.describe(att)}) -> {mob.name} '
                  f'({dmgmod.describe(vic)}) {self._hit_text(event, hit)} = {hit.total}')
        return hit

    def _skill_hit_damage(self, session, sd, mob, event=dmgmod.EV_STRONG, char=None):
        """(damage, log text) of attack skill `sd` - the caster's fresh cast - landing on `mob`.

        'client': the victim event 9 (4 guarded) with the skill term of the cell the client
        resolves for the cast (damage.cast_skill: the lowest learned rank of the family, its
        Attribute / Attri_Atk / Skill_P_A, the Triple Kick / Merciless Strike divisor, Nova,
        Vampiric Attack); a cast with no cell is a plain strong hit.
        'placeholder': combat.skill_damage over the caster's attack."""
        char = self._session_char(session) if char is None else char
        if not self._client_damage():
            atk, bonus = self._caster_attack(session, char)
            return (self._graded(combat.skill_damage(sd, atk, mob.defense, bonus), sd.name),
                    f'{sd.name} ({sd.id})')
        hit = self._formula_hit(session, mob, event, dmgmod.cast_skill(char, sd.id), char=char)
        return hit.total, f'{sd.name} ({sd.id}) {self._hit_text(event, hit)}'

    def _trap_damage(self, session, sd, mob):
        """(damage, log text) of a Booby Trap catch on `mob`: the event-9 hit whose skill term
        is the family 0xA31 (the client's attacker +0x960 override), fire included."""
        char = self._session_char(session)
        if not self._client_damage():
            atk, bonus = self._caster_attack(session, char)
            return self._graded(combat.skill_damage(sd, atk, mob.defense, bonus), 'trap'), ''
        hit = self._formula_hit(session, mob, dmgmod.EV_STRONG, dmgmod.trap_skill(char), char=char)
        return hit.total, self._hit_text(dmgmod.EV_STRONG, hit)

    def _detonation_damage(self, session, sd, mob, char=None):
        """The delayed hit of a Time Bomb / Cartilage Smash on `mob`, fixed at cast time.
        Client: the event-9 hit of the (p5 6, class 4, job 2) / (p5 9, class 2, job 2) cell is
        clamped to >= 1 and stored at victim +0xE20 with no digit (0x419D44, before the HP cap);
        FUN_00426b60 releases it at the end as an event-9 arg6 hit, which adds the weapon
        element again. The grade roll (DAMAGE_GRADE_ROLL) is on that release, where the client
        patch rolls the stored hit's one digit - never on the stored value."""
        char = self._session_char(session) if char is None else char
        if not self._client_damage():
            atk, bonus = self._caster_attack(session, char)
            return self._graded(combat.skill_damage(sd, atk, mob.defense, bonus), 'detonation')
        att = self._player_stats(session, char)
        vic = dict(dmgmod.monster_stats(mob), hp=None)            # stored before the HP cap
        stored = dmgmod.damage(att, vic, dmgmod.EV_STRONG, dmgmod.cast_skill(char, sd.id)).total
        return dmgmod.damage(att, vic, dmgmod.EV_STRONG, direct=stored, roll=self._grade_roller()).total

    def _damage_monster(self, sock, session, mob, dmg, what, *, attacker=None, take_control=True, mons=None):
        """Take `dmg` off a live monster; at 0 HP it dies through the one monster lifecycle
        (_kill_monster: 0x29 with the hold tick to every client on the map, exp 0x21, quest
        credit, drop 0x18 to the killer, the despawn/respawn timers). Returns True on a kill.
        The caller holds the combat lock of `session` (the hitting player, credited with a
        kill; None for a DoT whose caster left the map) and the map's monster lock, so the
        alive check and the kill share one critical section (exactly once).

        A survivor feeds its hate list and may turn on `attacker` (default: the session's own
        player; _mob_aggro). take_control=False when the caller releases a client-caught hit
        with the 0x2A itself (_resolve_hit ack, _resolve_trap), which takes the mob over.
        `mons`: the mob's map (default: the session's)."""
        if not mob.alive:
            return False
        dmg = max(0, int(dmg))
        mob.hp = max(0, mob.hp - dmg)
        log.info(f'[ATK] {what} hit {mob.name} uid={mob.uid:#x} dmg={dmg} -> hp={mob.hp}/{mob.max_hp}')
        if attacker is None and session is not None:
            attacker = P.session_uid(session)
        if mob.hp > 0:
            self._mob_aggro(sock, session, mob, attacker, dmg, take_control=take_control, mons=mons)
            return False
        debuffmod.clear(mob)
        self._kill_monster(sock, session, mob, no_enc=(session or {}).get('no_enc', True), mons=mons)
        return True

    def _release_hit_lock(self, sock, session, mob, event=HIT_REPORT_EVENT):
        """A monster SURVIVED a hit that `session`'s client caught itself (a swing @0x416AA3,
        a trap @0x417C9A). `event` is the interact event of the catch: HIT_REPORT_EVENT 7 the
        basic swing (_resolve_hit), 9 / 4 a strong attack or skill hit / a guarded one
        (_resolve_skill_report), TRAP_INTERACT_EVENT 0xC a trap catch (_resolve_trap). Two
        packets follow:

        - to the attacker only: S2C 0x2A {mover = mob, 8 zero state bytes, target = his own
          uid} (16 B). His client's catch set its +0x8E2 hit-lock, which holds the mob in the
          hurt pose and refuses every later hit until a 0x29 or a 0x2A aimed at the receiver
          clears it (0x455136; found live on the live-combat branch, whose _send_hit_release
          packs the same 16 bytes by hand). The same 16 bytes are the monster AI's command
          with lo = 0 (_mob_command): the 0x2A also sets +0x971 = 1 (0x459C89 [0x454EB3]), so
          from here the server drives the mob (MOB_AGGRO) until a 0x9E, 0x29 or 0x1A clears
          it again. No other client has that hit-lock, and its first chase word (to everyone)
          takes their copies over;
        - to every OTHER client holding the mob: the hit relay (_relay_hit). The attacker
          never gets it back: his own client drew the hit already - and he never gets a
          33-byte 0x2A before this release (only target == receiver clears the hit-lock,
          0x459F04: a position form under it flushes the queue and freezes the mob in hurt).
        Desync fix M1 / M3 (HIT_STUN_PER_SWING_RE_2026-09-28), for a swing or dash attack
        (event 7) and a skill or strong-attack hit (event 9): _hit_stun reads what the hit did
        to the attacker's own copy - the hurt L - E (what is left of his action at the hit,
        from his own 0x0D words: hitstun.py), the stun and the slide - and
        - no chase word goes to anyone before the hit + max(MOB_HIT_RECOVER_SECS, stun +
          MOB_HIT_RECOVER_MARGIN_SECS) (Monster.ai_recover_until, _mob_send_decision; a
          combo extends it, nothing shortens it): by then every copy has finished its hurt
          and stands, so the chase restarts on all of them in the same tick from the same
          point. An ice-element hit (stun hurt + 1000) instead sends it at the hit +
          MOB_HIT_ICE_CHASE_SECS, inside the stun, before the copies stall (_arm_hit_gate);
        - the relay carries the attacker's facing (_hit_knock_facing) and that hurt: the
          33-byte swing form (MOB_HIT_RELAY_KNOCKBACK) or the 34-byte case-9 form with his
          cast variant (MOB_HIT_RELAY_SKILL_VARIANT), and the server's point slides as the
          copies do.
        A guarded hit (4) and a trap catch (0xC) keep the relay from before M1 - the flinch in
        place (facing2 0, hi 0), no slide of the server's point - and set no gate (a gate an
        earlier hit set runs on)."""
        mons = self._mob_home(session)
        if mons is None:
            return
        now = time.monotonic()
        hit = self._hit_stun(session, mob, event, now) if event in self.HIT_STUN_EVENTS else None
        with mons.lock:
            if hit is not None:
                self._arm_hit_gate(mons, mob, now, hit)
            self._mob_command(mons, mob, mobai.STOP, now, form='2A', only=[session])
            knock = hit is not None and self._hit_knock_on(event)
            facing, why = self._hit_knock_facing(session, mob, now, hit) if knock else (None, '')
            self._relay_hit(mons, mob, session, facing=facing, why=why, now=now, event=event, hit=hit)

    # The interact events whose catch has a per-kind hurt, a relay knockback and a chase gate
    # (hitstun.py): 7 a swing / dash attack, 9 a skill or strong attack. 4 (guarded) and 0xC
    # (trap) flinch in place.
    HIT_STUN_EVENTS = frozenset((HIT_REPORT_EVENT, HS.SKILL_EVENT))

    def _hit_knock_on(self, event):
        """Is the knockback relay on for a catch with interact `event`? 7: config
        MOB_HIT_RELAY_KNOCKBACK (M1), 9: MOB_HIT_RELAY_SKILL_VARIANT (M3); never for 4 / 0xC,
        and never with MOB_AGGRO off (the 0x1B node form)."""
        if not self._mob_ai_enabled():
            return False
        if event == HIT_REPORT_EVENT:
            return bool(self.config.get('MOB_HIT_RELAY_KNOCKBACK', True))
        if event == HS.SKILL_EVENT:
            return bool(self.config.get('MOB_HIT_RELAY_SKILL_VARIANT', True))
        return False

    def _hit_stun(self, session, mob, event, now=None):
        """hitstun.Hit of `session`'s client-caught hit on `mob` with interact `event` (7 /
        9): the tail's target_action_event (session['last_hit'], when it names this mob and
        is fresh), his class / tier / weapon type, his attack-skill cast of the last
        SKILL_REPORT_SECS and its age (session['last_cast']: a cast younger than its L still
        runs, and swing words then are the attack key held through it) and his action tracker
        (session['hitstun'], _on_move_state). Config MOB_HIT_HURT_PER_SWING off:
        MOB_HIT_RELAY_HURT_MS; a hit it cannot classify: a high guess (hitstun.classify)."""
        now = time.monotonic() if now is None else now
        char = self._session_char(session)
        cls, tier = CC.of(char)
        tae = None
        last = session.get('last_hit')
        if (last is not None and len(last) > 4 and (int(last[0]) & 0xFFFF) == (mob.uid & 0xFFFF)
                and now - float(last[3]) <= self.LAST_HIT_FRESH_SECS):
            tae = last[4]
        cast = session.get('last_cast')
        age = now - float(cast['t']) if cast is not None else None
        fresh = age is not None and age <= self.SKILL_REPORT_SECS
        return HS.classify(session.get('hitstun'), event, tae, cls=cls, tier=tier,
                           weapon=self._weapon_class(session, char) if char is not None else 0,
                           cast_skill=int(cast['skill']) if fresh else None,
                           cast_age_ms=max(0.0, age * 1000.0) if fresh else None,
                           fallback_ms=int(self.config.get('MOB_HIT_RELAY_HURT_MS', 490)),
                           per_swing=bool(self.config.get('MOB_HIT_HURT_PER_SWING', True)),
                           attribute_of=self._item_attribute)

    @staticmethod
    def _item_attribute(item_id):
        """The hii Attribute (record +0x190) of `item_id`, 0 when it has none."""
        item = en_item(item_id)
        return int(getattr(item, 'attribute', 0) or 0) if item is not None else 0

    def _arm_hit_gate(self, mons, mob, now, hit):
        """The chase gate of a client-caught swing / skill hit (hitstun.Hit `hit`; caller holds
        mons.lock). Every copy is in state 3 until the hit + hit.stun (Monster.ai_stun_until).
        - Ice element (+0x95D = 2, stun = hurt + 1000, config MOB_HIT_ICE_CHASE_SECS > 0):
          the gate is the hit + MOB_HIT_ICE_CHASE_SECS - set, not maxed: this hit restarted
          state 3 on every copy, so no earlier hit's or cast's gate counts any more - and a
          one-shot timer runs the mob's AI step then (_chase_at), not the next 'monster-ai'
          tick (up to MOB_AI_TICK_SECS later). A server-driven copy's state machine runs only
          while its last 0x2A node's 960 ms hold does (type-4 tick gate 0x4142B8..0x4142D1:
          (+0xEE4 && +0x971) || +0x96D): with no word queued, both copies stalled their
          +0xE9C at 960-990 and ran the rest of the stun only after the chase word (live 2b:
          3.3-3.5 s frozen with the 2.14 s gate, retail 2.0 s). A queued word does not cut the
          stun short: state 3 exits on +0xE9C against +0x9E4 (+1000 ice) alone (0x41528A..
          0x4152C2) and a command's action nibble 0 never reaches pass 2 (the +0x9E4 copy at
          0x413BAF is inside the 6..10 cases). So both copies end at 1000 + their hurt.
          The word goes out even when the decision did not change (STOP included) and is
          kept alive until the stun ends (_ice_word_due).
        - Otherwise: _arm_recover_gate (the hit + max(MOB_HIT_RECOVER_SECS, stun + margin)).
          A stun past the hold stalls the same way (a strong attack / non-ice skill, 1020..
          1220 ms): both copies hold +0xE9C at 960-990 until the chase word at the gate, then
          each runs the rest of its own stun (stun - 0.96 s). So the gate evens out two
          copies' hurts only when one of them is under the hold (it stands idle, the other
          stalls); when both are past it, it evens out nothing and freezes both for (stun -
          0.84 s) plus up to one AI tick more than their stun. The server's estimate follows
          that: the first word after the hit, past the hold, starts the walk at that word +
          the rest (Monster.ai_stun_t / ai_stun_secs, _mob_word_after_stun).
        MOB_HIT_RECOVER_SECS 0: no gate at all. Returns the gate (0.0 with none)."""
        mob.ai_stun_until = now + float(hit.stun) / 1000.0
        mob.ai_stun_t, mob.ai_stun_secs = now, float(hit.stun) / 1000.0
        mob.ai_ice_until = 0.0
        ice = self._ice_chase_secs()
        if hit.element == HS.ELEMENT_ICE and ice > 0 and float(self.config.get('MOB_HIT_RECOVER_SECS', 0.0) or 0.0) > 0:
            mob.ai_ice_until = mob.ai_stun_until
            until = mob.ai_recover_until = mob.ai_ice_chase_t = now + ice
            ticks = getattr(self, 'ticks', None)
            if ticks is not None:
                ticks.call_at(until, self._chase_at, mons, mob, until, name='mob-ice-chase')
            return until
        return self._arm_recover_gate(mob, now, hit.stun)

    def _ice_chase_secs(self):
        """Config MOB_HIT_ICE_CHASE_SECS (0: the ice rule is off)."""
        return float(self.config.get('MOB_HIT_ICE_CHASE_SECS', 0.0) or 0.0)

    def _chase_at(self, mons, mob, when):
        """Tick callback at an ice hit's gate (_arm_hit_gate): the mob's AI step at once, so
        its chase word reaches every copy before their +0xE9C stalls - the decision even when
        it did not change (_ice_word_due). Nothing when the mob died, was handed back or a
        later hit moved the gate."""
        if not self._mob_ai_enabled():
            return
        now = max(time.monotonic(), float(when))
        with mons.lock:
            if mob.alive and mob.aggro_uid and now >= mob.ai_recover_until:
                self._mob_ai_step_in(mons, mob, now)

    @staticmethod
    def _ice_word_due(mob, now, keepalive):
        """True while an ice hit's stun runs past its chase time (Monster.ai_ice_until /
        ai_ice_chase_t) and the copies need a word although the decision did not change: the
        first at the chase time, then one every keep-alive - STOP included, which a decision
        is otherwise never re-sent as (a mob a control effect holds at the chase time would
        get no word, and both copies would stall at the end of the release's 960 ms hold)."""
        if now >= getattr(mob, 'ai_ice_until', 0.0):
            return False
        return mob.ai_sent_t < getattr(mob, 'ai_ice_chase_t', 0.0) or now - mob.ai_sent_t >= keepalive

    @staticmethod
    def _mob_word_after_stun(mob, now):
        """A word to every copy of `mob` at `now` (_mob_command): the first one after its last
        client-caught hit (Monster.ai_stun_t) says when the copies' stun really ends. Past the
        0x2A node's hold (mobai.HOLD_2A_SECS from the hit's release / relay) a stun longer than
        the hold has stalled both copies (the type-4 tick gate 0x4142B8): they run its rest,
        stun - the hold, from this word on, so the dead reckoning (Monster.ai_stun_until)
        starts no earlier. Caller holds mons.lock."""
        stun = float(getattr(mob, 'ai_stun_secs', 0.0) or 0.0)
        if stun <= 0 or now <= mob.ai_stun_t:
            return                                  # none, or the hit's own release / relay
        mob.ai_stun_secs = 0.0
        hold = mobai.HOLD_2A_SECS
        if stun > hold and now - mob.ai_stun_t > hold:
            mob.ai_stun_until = max(mob.ai_stun_until, now + stun - hold)

    def _arm_recover_gate(self, mob, now, stun_ms):
        """Desync fix M1: no chase word for `mob` before now + max(MOB_HIT_RECOVER_SECS,
        stun_ms / 1000 + MOB_HIT_RECOVER_MARGIN_SECS) - never earlier than a gate already
        running. MOB_HIT_RECOVER_SECS 0: no gate. Caller holds the map's monster lock.
        Returns that time (0.0 with no gate)."""
        floor = float(self.config.get('MOB_HIT_RECOVER_SECS', 0.0) or 0.0)
        if floor <= 0:
            return 0.0
        margin = float(self.config.get('MOB_HIT_RECOVER_MARGIN_SECS', 0.12))
        until = now + max(floor, float(stun_ms) / 1000.0 + margin)
        if until > mob.ai_recover_until:
            mob.ai_recover_until = until
        return until

    # Desync fix M1: with no +0x949 model (no 0x07 sent to him yet), no cast and no held key,
    # a hit tail with |target_dx| below this knocks right; a tail older than
    # LAST_HIT_FRESH_SECS is not this hit's.
    KNOCK_FACING_MIN_DX = 8.0
    # hitstun.report_facing (+0x949: 2 left, 6 right) -> combat.FACING_*.
    KNOCK_FACING_OF_949 = {2: combat.FACING_LEFT, 6: combat.FACING_RIGHT}
    LAST_HIT_FRESH_SECS = 1.0
    # The hurt slide (state 3 moves while +0xE9C <= 0x95 from 0x3C, 0x415412..0x415426): two
    # 30 ms ticks at the monster's walk speed (Monkey Soldier 2 x 3.75 = 7.5 px, measured) for
    # a swing; four from 0 for a skill hit, 19 x +0x13E8 with the wind element (hitstun.Hit).
    MOB_KNOCK_TICKS = 2
    MOB_TICK_SECS = 0.03

    def _hit_knock_facing(self, session, mob, now=None, hit=None):
        """(facing, why) of the attacker for the knockback of his client-caught hit on `mob`
        (desync fix M1). The attacker's copy of the mob slides in HIS facing (FUN_00412c60
        case 7 @0x412F57 / case 9 0x4134DA..0x4134E0: mob +0x95B = attacker +0x949), not
        toward the side the mob is on - a mob overlapping him or behind him included.
        First his +0x949 as his own words turned it (hitstun.report_facing: the direction key
        counts only on the ticks the turn check runs, 0x414498..0x41450F - states 8 / 0xC /
        the air; a key pressed in a hurt, a dash, a swing or a cast turns nothing - and the
        report's own key not yet; live 2b: 85 / 85 hits against A's +0x95B, where the held
        key missed 5 and the tail's side 9, each leaving a 15 px gap).
        Until his words turn him, the model is the facing his last S2C 0x07 created him with
        (read into +0x949 at 0x453B80: the idle block's 2, left; _map_transfer seeds it).
        With no model at all (no 0x07 sent to this session): a cast's hit (`hit` event 9
        classified as the skill, with his attack-skill cast of the last SKILL_REPORT_SECS, of
        the hit's variant): his facing at that cast (session['last_cast']['facing'],
        _skill_attack) - he cannot turn in state 1, and a box with a back (Crescent Slash,
        Heaven Strike: 300 px behind) hits mobs behind him; else his last held direction
        (session['facing'], combat.facing_from_state); else the side of the mob in his
        report's interact tail (session['last_hit'] from _on_move_state; target_dx = mob -
        attacker) when |dx| >= KNOCK_FACING_MIN_DX; else right. Two case-9 exceptions (by
        the attacker's class / tier and the cast variant): Rogue tier-1 variant 5
        (Assassination) reverses it; Mage tier-1 variant 7 (Nova) pushes the mob away from
        him (the side of the mob, any |dx|). `why` names the source and the values, for the
        log."""
        now = time.monotonic() if now is None else now
        tail_dx = None
        last = session.get('last_hit')
        if (last is not None and (int(last[0]) & 0xFFFF) == (mob.uid & 0xFFFF)
                and now - float(last[3]) <= self.LAST_HIT_FRESH_SECS):
            tail_dx = float(last[2])
        held = session.get('facing')
        names = {combat.FACING_LEFT: 'left', combat.FACING_RIGHT: 'right'}
        track = session.get('hitstun')
        turned = self.KNOCK_FACING_OF_949.get(HS.report_facing(track))
        cast = session.get('last_cast') if hit is not None and hit.event == HS.SKILL_EVENT and hit.kind == 'skill' else None
        cast_facing = (cast.get('facing') if cast is not None and now - float(cast['t']) <= self.SKILL_REPORT_SECS
                       and HS.variant_of(cast['skill']) == hit.variant else None)
        if turned is not None:
            origin = 'his 0x07 spawn record' if HS.report_facing_from(track) == 'spawn' else 'his words'
            facing, src = turned, f'his +0x949 ({names[turned]}, {origin})'
        elif cast_facing in names:
            facing, src = cast_facing, f'the cast ({names[cast_facing]} at cast time)'
        elif held in names:
            facing, src = held, 'held key'
        elif tail_dx is not None and abs(tail_dx) >= self.KNOCK_FACING_MIN_DX:
            facing, src = (combat.FACING_RIGHT if tail_dx > 0 else combat.FACING_LEFT), 'tail dx'
        else:
            facing, src = combat.FACING_RIGHT, 'default'
        if hit is not None and hit.event == HS.SKILL_EVENT and hit.variant:
            job = CC.of(self._session_char(session)) + (hit.variant,)
            if job == (4, 1, 5):
                facing = combat.FACING_LEFT if facing == combat.FACING_RIGHT else combat.FACING_RIGHT
                src += ', reversed (Rogue tier 1 variant 5)'
            elif job == (5, 1, 7) and tail_dx:
                facing = combat.FACING_RIGHT if tail_dx > 0 else combat.FACING_LEFT
                src = 'the side of the mob (Mage tier 1 variant 7)'
        tail = 'none' if tail_dx is None else f'{tail_dx:+.1f}'
        return facing, f'facing from {src}: tail dx {tail}, held {names.get(held, "none")}'

    @classmethod
    def hit_relay_words(cls, facing=None, hurt_ms=0, action=None):
        """(lo, hi) of the hit relay (desync fix M1 / M3): lo = the action nibble << 12 (the
        tail's target_action_event: 7 / 8 a swing, 9 / 10 a skill hit - 9 / 10 make the 0x2A
        handler read the u8 action_flag, 0x459DF1..0x459E10, hit_relay_fields; default
        MOB_HIT_RELAY_EVENT 7) | f << 20 with f = 1 for an attacker facing left, 2 right, 0
        none (the old in-place flinch: node+0x50 8); the reaction nibble (bits 16-19) stays
        0. hi = the hurt ms, 12 bits (node+0x44 -> +0x9E8 -> +0x9E4). Right, 360: (0x00207000,
        0x168); Ice Spear left, 1020: (0x00109000, 0x3FC) with action 9."""
        f = {combat.FACING_LEFT: 1, combat.FACING_RIGHT: 2}.get(facing, 0)
        action = cls.MOB_HIT_RELAY_EVENT if action is None else int(action) & 0xF
        return (action << 12) | (f << 20), int(hurt_ms) & 0xFFF

    @classmethod
    def hit_relay_fields(cls, mob_uid, attacker_uid, x, y, facing=None, hurt_ms=0, action=None, flag=0):
        """The S2C 0x2A of the hit relay to a client that is NOT the attacker (hit_relay_words):
        - action 7 / 8, 33 bytes: {u32 mob, u32 lo, u32 hi, u32 target = attacker, f64 x,
          f64 y, u8 airborne 0}. Mob 0x000F0009, right, hurt 360, attacker uid 1, (656.25,
          1901.375): 09 00 0F 00 00 70 20 00 68 01 00 00 01 00 00 00 00 00 00 00 00 82 84 40
          00 00 00 00 80 B5 9D 40 00;
        - action 9 / 10 (desync fix M3), 34 bytes: the u8 action_flag `flag` (node+0x52 ->
          +0x98C: the attacker's cast variant +0x98B) after the target, before the position
          block. Ice Spear: mob 0x000F0000, left, hurt 1020, uid 1, flag 1, (861, 1920):
          00 00 0F 00 00 90 10 00 FC 03 00 00 01 00 00 00 01 00 00 00 00 00 E8 8A 40 00 00
          00 00 00 00 9E 40 00."""
        lo, hi = cls.hit_relay_words(facing, hurt_ms, action)
        fields = {'mover_uid': int(mob_uid) & 0xFFFFFFFF, 'move_bits': struct.pack('<II', lo, hi),
                  'target_uid': int(attacker_uid) & 0xFFFFFFFF, 'pos_x': float(x), 'pos_y': float(y),
                  'airborne': 0}
        if (lo >> 12) & 0xF in HS.TAES[HS.SKILL_EVENT]:
            fields['action_flag'] = int(flag or 0) & 0xFF
        return fields

    def _mob_knock_estimate(self, mob, facing, now, ticks=None):
        """Desync fix M1 / M3 + P3: the server's point of a mob after a client-caught hit
        (_relay_hit) slides as every client's copy does - `ticks` ticks (hitstun.Hit.ticks: 2
        for a swing, 4 for a skill hit, 19 x 1.0 / 2.5 for the wind element, 0 for an airborne
        victim; default MOB_KNOCK_TICKS) at its walk speed in the attacker's facing - and is a
        fix at `now`. Returns the dx."""
        ticks = self.MOB_KNOCK_TICKS if ticks is None else float(ticks)
        dx = ((1 if facing == combat.FACING_RIGHT else -1) * ticks * self.MOB_TICK_SECS
              * self._mob_walk_px_s(mob))
        mob.x += dx
        mob.fix_t = now
        return dx

    def _hitter_point(self, session, mob, now=None):
        """Where `session`'s client had `mob` at his hit: the victim point of his report's
        interact tail (session['last_hit'] from _on_move_state: his pos + target_dx / dy),
        when it names this mob and is at most LAST_HIT_FRESH_SECS old; else None. His own
        copy of the mob stands there and slides from there - the sync point of _relay_hit,
        whether or not the mob chases him (_mob_fix_allowed rules every other fix)."""
        now = time.monotonic() if now is None else now
        last = session.get('last_hit')
        if (last is None or len(last) < 4 or last[1] is None
                or (int(last[0]) & 0xFFFF) != (mob.uid & 0xFFFF)
                or now - float(last[3]) > self.LAST_HIT_FRESH_SECS):
            return None
        return float(last[1][0]), float(last[1][1])

    def _relay_hit(self, mons, mob, attacker, *, facing=None, why='', now=None, event=HIT_REPORT_EVENT,
                   hit=None):
        """The attacker's client-caught hit on the OTHER clients that hold the mob (world-
        shared-monsters + party-dep-combat-multiclient: "everyone else sees the hit feedback,
        the attacker never gets it back"). Their clients detected nothing, so the server
        shows it with a node on the MONSTER whose action nibble (lo bits 12-15 -> +0x9DC via
        the consumer 0x4129F0 [0x412100]) is the hit event 7 and whose target (-> +0xE10) is
        the attacker: the relay LIVE_TEST_LOG 2026-09-23 "combat SOLVED" names ("Multiplayer
        relay = S2C 0x1B action 7 + target = attacker to OTHER clients only"; spec 0x1B/0x2A:
        action nibble != 0 -> u32 target_uid). The carrier depends on who runs the watchers'
        copy of the mob:

        - MOB_AGGRO on: the server does - every chase word goes to every client - and a
          queued 0x1B would wait behind the running 960 ms chase node until the next
          keep-alive 0x2A flushed it unplayed. So the node is the full S2C 0x2A {mob, lo =
          7 << 12, hi 0, target = the attacker, f64 x, f64 y, u8 airborne 0} (33 B: target !=
          receiver, so the handler reads the position block): applied at once (it flushes,
          C3), it plays the hit, takes the copy over like the attacker's (+0x971 = 1: red
          under the aggro-rules patch, contact allowed) and puts it at the sync point - the
          retail post-hit viewer packet (MONSTER_AGGRO_RE retail flow step 2,
          RETAIL_COMBAT_FEEL 2.3 "Other players watching"). The sync point is the hitter's
          own report tail (_hitter_point: his copy of the mob stands there and is knocked
          from there), whoever the mob chases, and the server's point takes it too; only
          without a fresh tail naming the mob is it the server's point. Every surviving hit
          thus re-syncs every copy to the hitter's: the watchers' by this packet, the
          server's here, his own never moved. Live session 3: with the server's point
          instead, the 7 of 54 relays of a hitter the mob was not chasing (_mob_fix_allowed
          had refused his tail) put the watchers 11..69 px off his copy.
        - MOB_AGGRO off: nothing drives the watchers' copy and a 0x2A would freeze it for
          good (+0x971 = 1 with no follow-up, Gate B), so the node is the queued S2C 0x1B
          {mob, hold MOB_HIT_RELAY_HOLD_MS, lo = 7 << 12, 0, target = the attacker}, which never
          touches +0x971.
        Desync fix M1 (config MOB_HIT_RELAY_KNOCKBACK, MOB_AGGRO on): the 0x2A also carries
        the attacker's facing in lo bits 20-21 and the hurt time in hi (hit_relay_fields:
        right, 360 = lo 0x00207000, hi 0x168). With target != receiver the
        handler keeps both (node+0x50 0x459D83..0x459DA5, node+0x44 0x459D7A; only the
        self-form forces facing2 8 and hi 0), writes the position at once (0x459E16..) and
        sets +0x963; the type-4 consumer copies them to +0x95C / +0x9E8 and the hit
        resolution (0x413BAF) to +0x95B / +0x9E4, so the watchers' copies slide 2 ticks and
        stay in state 3 for hurt - 60 ms, as the attacker's own copy does from its detection.
        The reaction nibble stays 0, the packet 33 bytes. The server's
        point of the mob then slides the same way (_mob_knock_estimate) - also with no
        watcher: the attacker's copy slid. False: facing2 0 and hi 0, the old flinch in place.
        The hurt is `hit`'s (hitstun.Hit from _hit_stun: basic swing 490, dash attack 760,
        ...; MOB_HIT_RELAY_HURT_MS for a hit it cannot classify) and the action nibble its
        tae: 7, or 8 for an airborne victim (state 0x17 on every copy, as on the attacker's).
        Desync fix M3 (config MOB_HIT_RELAY_SKILL_VARIANT, `event` 9): the case-9 form - action
        9 (10 airborne) and the u8 action_flag = the attacker's cast variant (0 for a strong
        attack; 0 too for a Priest tier-2 Vampiric Attack, whose drain FUN_004194f0 would
        apply to the watchers' copies of the attacker: hitstun.relay_flag) before the
        position block, 34 bytes (hit_relay_fields). The watchers' consumer
        copies the flag to +0x98C and their pass 2 runs the attacker's: the 4-tick slide
        (+0xE9C from 0), the stun of hi, the element FUN_004194f0 reads from THEIR copy of the
        attacker's learned list (ice +1000 ms, wind 19 ticks) and FUN_00426840's local debuff.
        The server's point slides `hit.ticks`. Off: the old flinch in place.
        A guarded hit (4) and a trap catch (0xC) are always the old flinch in place.
        Caller holds mons.lock. Returns the number sent."""
        now = time.monotonic() if now is None else now
        attacker_uid = P.session_uid(attacker) or 0
        aggro = self._mob_ai_enabled()
        if hit is None and event in self.HIT_STUN_EVENTS:
            hit = self._hit_stun(attacker, mob, event, now)
        knock = hit is not None and self._hit_knock_on(event)
        if knock and facing is None:
            facing, why = self._hit_knock_facing(attacker, mob, now, hit)
        server_x, server_y = float(mob.x), float(mob.y)
        tail = self._hitter_point(attacker, mob, now)
        if tail is not None:                                # the sync point: his copy's
            mob.x, mob.y = tail
            mob.fix_t = now
        hit_x, hit_y = float(mob.x), float(mob.y)          # the copies are put here, then slide
        moved = (f' (his tail; the server had ({server_x:.0f},{server_y:.0f}))'
                 if abs(hit_x - server_x) >= 0.5 or abs(hit_y - server_y) >= 0.5 else '')
        dx = self._mob_knock_estimate(mob, facing, now, hit.ticks) if knock else 0.0
        watchers = self._mob_viewers(mons, mob, exclude=attacker)
        sent = 0
        action = hit.tae if knock else self.MOB_HIT_RELAY_EVENT
        if watchers and aggro:
            fields = self.hit_relay_fields(mob.uid, attacker_uid, hit_x, hit_y,
                                           facing if knock else None, hit.hurt if knock else 0,
                                           action=action, flag=hit.flag if knock else 0)
            sent = self._mob_broadcast(mons, mob, '0x2A', fields, receivers=watchers)
            mob.ai_takers.update(self._viewer_key(s) for s in watchers)
            mob.ai_owned = True
            lo, hi = struct.unpack('<II', fields['move_bits'])
            flag = f' flag {fields["action_flag"]}' if 'action_flag' in fields else ''
            form = f'0x2A lo {lo:#010x} hi {hi}{flag}, synced to ({hit_x:.0f},{hit_y:.0f}){moved}'
        elif watchers:
            sent = self._mob_broadcast(mons, mob, '0x1B', {
                'uid': mob.uid, 'hold_ms': self.MOB_HIT_RELAY_HOLD_MS,
                'state_blob': struct.pack('<II', self.MOB_HIT_RELAY_EVENT << 12, 0),
                'target_uid': attacker_uid}, receivers=watchers)
            form = '0x1B node'
        else:
            form = 'no other client holds it'
        wait = mob.ai_recover_until - now
        held = f'; chase held {wait:.2f} s' if wait > 0 else ''
        if held and hit is not None and hit.element == HS.ELEMENT_ICE and self._ice_chase_secs() > 0:
            held += (f' (ice: the word goes inside the stun, before the copies\' +0xE9C stalls '
                     f'at the 0x2A hold\'s 960 ms; they stand at {hit.stun} ms)')
        if hit is not None:
            elem = HS.ELEMENT_NAMES.get(hit.element, str(hit.element))
            stun = (f'{hit.why}, stun {hit.stun}' + (f' ({elem})' if hit.element else '')
                    + (f', slide {hit.ticks:g} ticks' if knock else ''))
        if knock:
            side = 'right' if facing == combat.FACING_RIGHT else 'left'
            knock_text = (f'; knock {side}, hurt {hit.hurt} ({why}); {stun}; estimate {dx:+.1f} px -> '
                          f'({mob.x:.0f},{mob.y:.0f}){held}')
        elif not aggro:
            knock_text = ''
        elif hit is not None:
            switch = 'MOB_HIT_RELAY_KNOCKBACK' if event == HIT_REPORT_EVENT else 'MOB_HIT_RELAY_SKILL_VARIANT'
            knock_text = f'; interact event {event:#x}: flinch in place, no knock ({switch} off); {stun}{held}'
        else:
            knock_text = f'; interact event {event:#x}: flinch in place, no knock, no chase gate'
        if watchers or knock:
            log.info(f'[ATK] hit on {mob.name} uid={mob.uid:#x} by uid {attacker_uid:#x} relayed to '
                     f'{sent} other client(s) ({form}, action {action}{knock_text})')
        return sent

    # ---- attack skills (cs-skill-damage, F3a) ----
    def _skill_attack(self, sock, session, sd):
        """An accepted attack skill (its S2C 0x25 and cost already went out) damages every
        live monster inside SKILL_HITBOX[family] in front of the caster; one at 0 HP dies
        through _kill_monster (P3 exit criterion 3: "damages a Pupu in front and kills it
        with exp"). A survivor gets no packet: EN monsters have no HP bar, and a server-side
        hit set no client hit-lock. No monster in the box is not an error - the cast happened.
        Returns the monsters hit."""
        char = self._session_char(session)
        with self._combat(session):
            origin, facing = self._skill_origin(session)
            box = combat.hitbox(sd.family)
            hit = combat.targets((session.get('monsters') or {}).values(), origin, facing, box)
            side = 'right' if facing == combat.FACING_RIGHT else 'left'
            # The client catches its own victims too and reports each one (C2S 0x0D event
            # 9); _resolve_skill_report matches those reports against this cast.
            # 'facing': his +0x949 for the whole cast (FUN_00414210 turns him only in states
            # 8 / 0xC / 9 / ...: never in state 1) - the knockback of its hits (_hit_knock_facing)
            session['last_cast'] = {'skill': sd.id, 't': time.monotonic(), 'hit': {m.uid for m in hit},
                                    'facing': facing}
            if not hit:
                log.info(f'[SKILL] {sd.name} ({sd.id}) cs-skill-damage: no monster in the '
                         f'{box.front}/{box.back}/{box.half_height} px box facing {side} from '
                         f'({origin[0]:g},{origin[1]:g})')
                return []
            gate = self._arm_cast_gate(session, char, sd, hit)
            for mob in hit:
                dmg, what = self._skill_hit_damage(session, sd, mob, char=char)
                self._damage_monster(sock, session, mob, dmg, what)
        log.info(f'[SKILL] {sd.name} ({sd.id}) cs-skill-damage: {len(hit)} monster(s) in the box '
                 f'facing {side} (Skill_P_A {sd.skill_p_a}, Attri_Atk {sd.attri_atk}, '
                 f'DAMAGE_FORMULA {self.config.get("DAMAGE_FORMULA", "client")}){gate}')
        return hit

    def _arm_cast_gate(self, session, char, sd, mobs):
        """Desync fix M1, config MOB_HIT_CAST_GATE: the chase gate of every monster the
        server's box of attack skill `sd` lands on, armed BEFORE its damage (whose aggro would
        otherwise send the chase word at cast time, live s02 15.749): the caster's client
        catches its own hit and reports it while he is still in state 1, so within the skill's
        length L (hitstun.cast_len: weapon type, cast variant, tier) - the gate is the cast +
        max(MOB_HIT_RECOVER_SECS, L + MOB_HIT_RECOVER_MARGIN_SECS), and the report re-arms it
        from the hurt it started (_release_hit_lock). No report: it just runs out. Caller
        holds the combat lock (the map's monster lock). Returns the log text ('' = none)."""
        if not mobs or not self.config.get('MOB_HIT_CAST_GATE', True):
            return ''
        wt = self._weapon_class(session, char) if char is not None else 0
        length = HS.cast_len(sd.id, wt, CC.of(char)[1])
        now = time.monotonic()
        until = 0.0
        for mob in mobs:
            until = max(until, self._arm_recover_gate(mob, now, length))
        if until <= 0:
            return ''
        return (f'; chase held {until - now:.2f} s from the cast (L {length}: weapon {wt}, '
                f'variant {HS.variant_of(sd.id)})')

    # ---- heals (cs-heal-visuals, F3b/F3c/F9/F11) ----
    def _send_heal_notice(self, session, uid=None, hp=None, mp=None):
        """S2C 0x40 EntityRecoverHpMp {uid, has_hp, has_mp, [hp], [mp]} to the OBSERVERS of
        `session`, once per heal event and never per regen tick. It is the heal visual - HP
        effect 0x37, MP effect 0x58, 0x54 always: the client's own Self Heal sprites (live
        combat_skill#15: a sparkle, no flinch) - not the hit reaction the old notes called
        it (B12), and a receiver writes no remote HP from it. The healed player himself gets
        S2C 0x25 (self heal, potion) or 0x3D (a received heal), never 0x40 as well, which
        would play the effect twice. Returns the number sent (0 until remote players share a
        map, P5)."""
        uid = P.session_uid(session) if uid is None else uid
        fields = {'uid': int(uid or 0) & 0xFFFFFFFF, 'has_hp': int(hp is not None),
                  'has_mp': int(mp is not None)}
        if hp is not None:
            fields['hp'] = max(0, int(hp)) & 0xFFFF
        if mp is not None:
            fields['mp'] = max(0, int(mp)) & 0xFFFF
        return self._send_to_observers(session, '0x40', fields)

    def _skill_self_heal(self, sock, session, sd):
        """F3b Self Heal: the HP went to the caster with the cast itself (S2C 0x25 adds the
        record's HP client-side and plays 0x54/0x37; the server mirrored it and pinned it with
        0x28). What is left is the observers' S2C 0x40."""
        with self._combat_lock(session):
            hp = int(session.get('hp') or 0)
            sent = self._send_heal_notice(session, hp=hp)
        log.info(f'[HEAL] {sd.name} ({sd.id}): self heal to {hp} HP, 0x40 to {sent} observer(s)')

    def _skill_target_heal(self, sock, session, sd):
        """F3c Heal / Remote Heal / Breath of Life (FUN_004258d0 = 0: the caster's S2C 0x25
        charged the cost and added no HP). No target is on the wire (combat_skill.md Q4), so
        party-skill-hooks picks F3c's interim one: the lowest-HP% living party member on the
        caster's map within party.HEAL_RANGE_PX, else the caster (no party, nobody weaker or
        near). The target: HP += the record's HP (clamped), S2C 0x3D {has_hp 1, hp} - the
        received-heal packet that sets the absolute and plays heal effect 0x37 with its sound
        once (live #14) - and S2C 0x40 to ITS observers (the caster among them). Party frames
        follow with the vitals tick (0x54). A Vampiric Attack drains: it always heals the
        caster. The target's combat lock only (never nested with the caster's: the cast
        released it before _skill_effect)."""
        vampiric = SK._in(sd.id, self.VAMPIRIC_RANGES)
        if vampiric:
            self._skill_attack(sock, session, sd)
        target = session if vampiric else self.party.heal_target(session)
        char = self._session_char(target)
        with self._combat_lock(target):
            if target.get('dead') or not target.get('in_world') or char is None:
                return
            if not target.get('max_hp'):
                hpmp.refresh(target, char)
            old = int(target.get('hp') or 0)
            new = hpmp.clamp_hp(target, old + max(0, sd.hp))
            target['hp'] = new
            fields = {'has_hp': 1, 'has_mp': 0, 'hp': new}
            if target is session:
                P.send(self, sock, session, '0x3D', fields)
            else:
                self._push(target, '0x3D', fields, 'HEAL')
            sent = self._send_heal_notice(target, hp=new)
        who = 'caster' if target is session else f'party member {target.get("char_name")!r}'
        log.info(f'[HEAL] {sd.name} ({sd.id}): target = {who}, HP {old}->{new} '
                 f'-> 0x3D, 0x40 to {sent} observer(s)')

    def _skill_party_heal(self, sock, session, sd):
        """F3e Group Heal 0xAD8..0xAE1 (party-skill-hooks). The caster's S2C 0x25 already
        healed the caster (its record HP applies to the caster: the cast handler added it and
        pinned it with 0x28) and, client-side, every party entity in the caster's scene. Each
        OTHER living member on the caster's map: HP += the record's HP (clamped, the client's
        own rule), S2C 0x92 {id} - its client plays effect/sound 0x10E and adds the same HP
        to its own party slots and to itself (spec 0x92; live combat_skill#24: exactly once)
        - and the absolute 0x28 pin. Members on other maps are not reached (the caster's
        client only heals entities in its scene). The frames follow with the next vitals
        tick (0x54). One member's combat lock at a time, never nested with the caster's."""
        healed = []
        for m in self.party.members(session, same_map=True, include_self=False, alive=True):
            char = self._session_char(m)
            with self._combat_lock(m):
                if m.get('dead') or not m.get('in_world') or char is None:
                    continue
                if not m.get('max_hp'):
                    hpmp.refresh(m, char)
                old = int(m.get('hp') or 0)
                new = hpmp.clamp_hp(m, old + sd.hp)
                m['hp'] = new
                if self._push(m, '0x92', {'skill_id': sd.id & 0xFFFF}, 'SKILL'):
                    self._push(m, '0x28', {'hp': new}, 'SKILL')
            healed.append(f'{m.get("char_name")} {old}->{new}')
        log.info(f'[HEAL] {sd.name} ({sd.id}): party heal +{sd.hp} -> '
                 + (f'0x92 + 0x28 to {", ".join(healed)}' if healed else 'no other member on the map'))

    # ---- targeted debuffs (cs-debuffs, F3h) ----
    def _skill_debuff(self, sock, session, sd):
        """An accepted targeted skill (S2C 0x25 sent: its 0x3B would insert nothing) lands on
        the nearest live monster in its hitbox in front of the caster: S2C 0x41
        {target_uid = mob, source_uid = caster, item_id} inserts the slot on the monster with
        the caster at +0x14 and shows its icon (live combat_skill#12), and debuffs.py records
        what the client will never run by itself in the field: DoT ticks (990 ms), the
        detonation at Con (Time Bomb / Cartilage Smash) and the control window (Stun, Spider
        Web, ...). Damage is fixed at cast time from the caster's attack. No monster in the
        box: the cast still happened, nothing lands. Returns the monster or None."""
        char = self._session_char(session)
        now = time.monotonic()
        with self._combat(session):
            origin, facing = self._skill_origin(session)
            hit = combat.targets((session.get('monsters') or {}).values(), origin, facing,
                                 combat.hitbox(sd.family))
            if not hit:
                log.info(f'[DEBUFF] {sd.name} ({sd.id}): no monster in front of '
                         f'({origin[0]:g},{origin[1]:g}) - nothing lands')
                return None
            mob = hit[0]
            uid = P.session_uid(session) or 0
            # DoT ticks are not FUN_004194f0's (the host-only FUN_00417e10 runs them): the same
            # rule under either DAMAGE_FORMULA
            rec, replaced = debuffmod.apply(
                mob, sd, now, src=uid,
                tick_damage=combat.dot_damage(sd, R.level_of(char)) if sd.dot else 0,
                hit_damage=self._detonation_damage(session, sd, mob, char) if sd.detonates else 0)
            if rec is None:
                log.info(f'[DEBUFF] {sd.name} ({sd.id}) has no duration (Con 0): nothing lands')
                return None
            fields = {'target_uid': mob.uid, 'source_uid': uid, 'item_id': sd.id}
            P.send(self, sock, session, '0x41', fields)
            # cs-observer-sync (F10 "debuff 0x41"): the other clients holding the monster show
            # the slot too, with the caster at +0x14 (0x41 inserts whenever Con != 0), and
            # get its end from _run_debuffs like the caster.
            mons = self._mob_home(session)
            seen = self._mob_broadcast(mons, mob, '0x41', fields, exclude=session) if mons is not None else 0
        log.info(f'[DEBUFF] {sd.name} ({sd.id}) -> 0x41 on {mob.name} uid={mob.uid:#x} (+{seen} observer(s)): '
                 f'{rec["kind"]} '
                 f'for {sd.duration_ms} ms'
                 + (f', {rec["tick_damage"]} per 990 ms' if rec['kind'] == 'dot' else '')
                 + (f', {rec["hit_damage"]} at the end' if rec['kind'] == 'detonate' else '')
                 + (' (refreshed)' if replaced else ''))
        return mob

    def _tick_debuffs(self, now=None):
        """cs-debuffs ticker (BUFF_TICK_SECS on the tick scheduler, world lock held): DoT
        ticks, detonations and the end of every slot on the monsters of every map.
        FUN_00417e10 runs all of it only as a room host, so without this a poisoned Pupu
        never takes a point and a Time Bomb's icon stays for ever (live #12). Returns the
        number of events handled (tests drive it with an explicit clock).

        world-shared-monsters: the slots live on the map's monsters, so they run on whoever
        is there. Each caster's slots run under HIS combat lock first and then the map's
        monster lock (the lock order: a kill's exp and drop are his), and only while he is
        on that map is he credited and sent the slot's removal (0x3C / 0x43; the 0x41 went
        to him alone, cs-observer-sync broadcasts both later). A caster who left the map or
        logged off leaves his DoT running with nobody credited."""
        now = time.monotonic() if now is None else now
        done = 0
        for mons in self.world.monster_maps():
            with mons.lock:
                srcs = sorted({int(rec.get('src') or 0) for mob in mons.values()
                               for rec in (mob.debuffs or {}).values()})
            for src in srcs:
                caster = self.world.session(src) if src else None
                try:
                    with contextlib.ExitStack() as held:
                        if caster is not None:
                            held.enter_context(self._combat_lock(caster))
                        held.enter_context(mons.lock)
                        if caster is not None and caster.get('monsters') is not mons:
                            caster = None
                        done += self._run_debuffs(mons, now, src, caster)
                except OSError as e:
                    log.debug(f'[DEBUFF] map {mons.map_code} uid {src:#x}: send failed ({e})')
        return done

    def _run_debuffs(self, mons, now, src, caster):
        """The due debuff events of caster uid `src` on the monsters of `mons` (caller holds
        the caster's combat lock and mons.lock). `caster` is his session while he is on the
        map, else None."""
        sock = caster.get('sock') if caster is not None else None
        done = 0
        for mob in list(mons.values()):
            if not mob.debuffs:
                continue
            if not mob.alive:
                debuffmod.clear(mob)
                continue
            for rec in debuffmod.due_ticks(mob, now, src=src):
                done += 1
                if self._damage_monster(sock, caster, mob, rec['tick_damage'],
                                        f'DoT {EC.item_name(rec["id"])} ({rec["id"]})',
                                        attacker=rec.get('src') or None, mons=mons):
                    break
            if not mob.alive:
                continue
            for rec in debuffmod.pop_expired(mob, now, src=src):
                # The removal first: for a detonation its 0x3C handler plays FUN_004256c0's
                # knockdown / explosion (live #12); then the damage, and 0x29 if it kills.
                key = buffmod.removal_opcode(rec['id'])
                # cs-observer-sync: every client that holds the monster got its 0x41 and has
                # the slot, so every one of them gets the removal (C4: none ends it itself),
                # the caster among them while he is on the map.
                self._mob_broadcast(mons, mob, key, {'buff_item_id': rec['id'], 'target_uid': mob.uid})
                done += 1
                log.info(f'[DEBUFF] {EC.item_name(rec["id"])} ({rec["id"]}) on {mob.name} uid={mob.uid:#x} '
                         f'ended -> {key}' + (' (detonation)' if rec['kind'] == 'detonate' else ''))
                if rec['kind'] == 'detonate' and self._damage_monster(
                        sock, caster, mob, rec['hit_damage'], f'{EC.item_name(rec["id"])} ({rec["id"]})',
                        attacker=rec.get('src') or None, mons=mons):
                    break
        return done

    # ---- Booby Trap trigger (cs-traps, F3g/F7) ----
    def _handle_trap_triggered(self, sock, session, rec, no_enc=False):
        """C2S 0x6C TrapTriggered {u16 trap_skill_id} (spec 0x417DB0/0x6C, C28). The owner's
        client caught a victim in its own trap (FUN_00416380: the first eligible entity whose
        box overlaps GetColRect(0xE1, x, y)), zeroed the slot's time and sent this. The victim
        uid is not in it: it follows at once in the owner's next C2S 0x0D, whose interact-0xC
        tail carries target_uid and item_id = the trap (live #22: 31 ms later, 45 B tail).

        So the trigger is only noted here (a live trap slot with that id must be on record,
        else it is stale, a duplicate or forged and ignored), and _on_move_state resolves it
        with that 0x0D; with no such 0x0D within TRAP_PAIR_SECS the nearest monster at the
        trap point is the victim (F7 step 4). The client waits for nothing, so nothing is
        sent here."""
        trap_id = int(rec.get('trap_skill_id', 0)) & 0xFFFF
        now = time.monotonic()
        with self._combat_lock(session):
            buff = buffmod.find(session, trap_id) if buffmod.is_trap(trap_id) else None
            if buff is None or session.get('dead') or not session.get('in_world'):
                log.info(f'[0x6C] trap triggered - consumed: no live trap {trap_id} on record '
                         f'(stale, duplicate or forged) - ignored')
                return
            pending = {'id': trap_id, 'x': buff['x'], 'y': buff['y'], 't': now}
            session['pending_trap'] = pending
        self.ticks.call_later(self.TRAP_PAIR_SECS, self._trap_pair_timeout, session, pending,
                              name=f'trap-pair-{trap_id}')
        log.info(f'[0x6C] trap triggered: {EC.item_name(trap_id)} ({trap_id}) at ({buff["x"]},{buff["y"]}); '
                 f'the victim comes in the next C2S 0x0D')

    def _trap_pair_timeout(self, session, pending):
        """Tick callback TRAP_PAIR_SECS after a C2S 0x6C whose 0x0D never named the victim
        (or did not decode): resolve it on the nearest monster at the trap point."""
        with self._combat(session):
            if session.get('pending_trap') is not pending:
                return                           # resolved by its 0x0D, or a map load came
            session.pop('pending_trap', None)
            sock = session.get('sock')
            if sock is None or not session.get('in_world') or session.get('dead'):
                return
            self._resolve_trap(sock, session, pending, None, None,
                               f'no C2S 0x0D victim within {self.TRAP_PAIR_SECS:g} s')

    def _resolve_trap(self, sock, session, trap, victim_uid, victim_pos, how, *, client_catch=True):
        """Consume `trap` ({id, x, y}) and damage its victim (F7 steps 3-5). The victim is the
        reported uid when it is a live monster standing within TRAP_VICTIM_RADIUS of the trap
        (its reported position when there is one, else the server's), else the nearest live
        monster inside the +-40 x +-30 px fallback box. The trap goes either way - the client
        zeroed its slot's time when it fired - with S2C 0x3C {trap id, owner} (slot removal +
        event 0xE2 at x,y: P3 exit criterion 8 "the trap icon clears"). A survivor of a
        client-detected catch gets the 0x2A hit-lock release. client_catch=False (the dev
        !trap) takes the given victim without the distance check or the release. Caller holds
        the combat lock. Returns the monster hit, or None."""
        trap_id = trap['id']
        point = (trap['x'], trap['y'])
        mons = session.get('monsters') or {}
        mob, why = None, ''
        if victim_uid is not None:
            cand = mons.get(int(victim_uid))
            where = victim_pos or ((cand.x, cand.y) if cand is not None else point)
            if cand is None or not cand.alive:
                why = f'reported victim {int(victim_uid):#x} is not a live monster; '
            elif client_catch and combat.distance(where, point) > combat.TRAP_VICTIM_RADIUS:
                why = (f'reported victim {cand.uid:#x} at ({where[0]:g},{where[1]:g}) is '
                       f'{combat.distance(where, point):.0f} px from the trap; ')
            else:
                mob = cand
                # position authority (_mob_fix_allowed): the trap owner's catch point is a fix
                # when the mob chases him or nobody
                if victim_pos is not None and self._mob_fix_allowed(mob, P.session_uid(session)):
                    mob.x, mob.y = float(victim_pos[0]), float(victim_pos[1])
                    mob.fix_t = time.monotonic()
        if mob is None:
            mob = combat.nearest(mons.values(), point, combat.TRAP_FALLBACK_HALF_W,
                                 combat.TRAP_FALLBACK_HALF_H)
        if buffmod.remove(session, trap_id) is not None:
            self._send_buff_removal(sock, session, trap_id)
        sd = SK.skill_def(trap_id)
        if mob is None or sd is None:
            log.info(f'[TRAP] {EC.item_name(trap_id)} ({trap_id}) at {point} fired ({how}): {why}'
                     f'no monster at the trap - consumed without damage (0x3C)')
            return None
        dmg, detail = self._trap_damage(session, sd, mob)
        log.info(f'[TRAP] {sd.name} ({trap_id}) at {point} fired ({how}): {why}victim {mob.name} '
                 f'uid={mob.uid:#x}, {dmg} damage {detail}, trap removed (0x3C)')
        # A client catch set the victim's hit-lock: the 0x2A release below takes it over. The
        # dev !trap caught nothing client-side, so the monster AI takes it over itself.
        if not self._damage_monster(sock, session, mob, dmg, f'{sd.name} ({trap_id})',
                                    take_control=not client_catch) and client_catch:
            self._release_hit_lock(sock, session, mob, self.TRAP_INTERACT_EVENT)
        return mob

    # ---- C2S 0x0D: facing, positions, trap victim, death watch, contact ----
    def _on_move_state(self, sock, session, rec):
        """What P3 stage 4 reads from a decoded C2S 0x0D (never answered as such: every packet
        sent from here is an event it caused, registry.detached()):

        - facing from state_lo bits 0-1 (+0x8B3, the key held; kept when released) for the
          skill hitbox;
        - an interact tail (bits 16-19 != 0) carries the player's position and his target:
          the target's point is pos + target_dx/dy, which becomes the server's estimate of
          that monster's position (wandering mobs, world-position-estimate);
        - interact 0xC with item_id = the pending trap: the trap's victim (cs-traps);
        - action 0xD (bits 12-15, +0x94C): the client's own death. After our S2C 0x3E it is
          the confirmation (live combat_skill#20); with the server's player still alive it is
          a client-side death (FUN_0041a230 opens no dialog), so the death runs here too and
          S2C 0x3E gives the player the dialog, his only way out (F8 step 6);
        - any other action with event_source_uid = one of the monsters of his map: that
          monster hit the player (FUN_00416380 writes 1..10 into the victim's +0x94C and the
          attacker into +0xD80) - contact damage when config MOB_CONTACT_DAMAGE is on.
        A packet naming another map (sent before a map load finished) is ignored."""
        if rec is None or not session.get('in_world'):
            return
        if int(rec.get('map_code', -1)) != int(session.get('current_map') or -1):
            return
        lo = int(rec.get('state_lo', 0))
        action, interact = (lo >> 12) & 0xF, (lo >> 16) & 0xF
        now = time.monotonic()
        self._track_action(session, rec)
        with registry.detached(), self._combat(session):
            side = combat.facing_from_state(lo)
            if side is not None:
                session['facing'] = side
                session['facing_t'] = now          # the driver's 1-in-N sample must not undo it
            victim_pos = None
            if interact and 'target_uid' in rec:
                victim_pos = (float(rec['pos_x']) + float(rec['target_dx']),
                              float(rec['pos_y']) + float(rec['target_dy']))
                mob = (session.get('monsters') or {}).get(int(rec['target_uid']))
                # world-shared-monsters: only the client the mob chases (or any, while it
                # chases nobody) says where it is (_mob_fix_allowed, G1 for monsters)
                if mob is not None and mob.alive and self._mob_fix_allowed(mob, P.session_uid(session)):
                    mob.x, mob.y = victim_pos
                    mob.fix_t = now
                # desync fix M1: the victim's side (target_dx = victim - attacker) gives the
                # knockback facing of the hit relay that may follow (_hit_knock_facing), the
                # tail's target_action_event (7 / 8, 9 / 10) its action (_hit_stun)
                session['last_hit'] = (int(rec['target_uid']), victim_pos, float(rec['target_dx']), now,
                                       int(rec.get('target_action_event', 0) or 0))
            if interact == self.TRAP_INTERACT_EVENT:
                pending = session.get('pending_trap')
                if pending is not None and int(rec.get('item_id', 0)) == pending['id']:
                    session.pop('pending_trap', None)
                    self._resolve_trap(sock, session, pending, int(rec.get('target_uid', 0)),
                                       victim_pos, 'C2S 0x0D interact 0xC')
            if action == self.ACTION_DIE:
                self._watch_client_death(session, now)
            elif action and self.config.get('MOB_CONTACT_DAMAGE'):
                self._monster_contact(session, rec, action, now)

    def _track_action(self, session, rec):
        """Desync fix M1 per-swing hurt: follow the player's current action from his C2S 0x0D
        words (hitstun.observe: the swing / dash attack / strong attack / cast that started
        it and the logic ms since; attack words inside a cast or strong attack start nothing,
        his client is in state 1; his own hurt - action nibble 6..10, hi its ms - ends his
        action, and an input held through it starts its action at the hurt's end, stage 0),
        so a hit report can say how much of it was left at the hit (_hit_stun), and his
        facing +0x949 at the hit (hitstun.report_facing, _hit_knock_facing). One tracker per
        session, new with every S2C 0x07 he is sent (_map_transfer: facing that record's
        direction byte) and with a map change."""
        track = session.get('hitstun')
        code = session.get('current_map')
        if not isinstance(track, dict) or track.get('map') != code:
            track = session['hitstun'] = dict(HS.new_tracker(), map=code)

        def weapon():
            char = self._session_char(session)
            try:
                return self._weapon_class(session, char) if char is not None else 0
            except (AttributeError, KeyError, TypeError, ValueError):
                return 0                        # an odd equip record: E0 / L of no weapon

        def tier():
            return CC.of(self._session_char(session))[1]
        HS.observe(track, rec.get('state_lo', 0), rec.get('logic_elapsed_ms', 0), weapon=weapon, tier=tier,
                   hi=rec.get('state_hi', 0))

    def _watch_client_death(self, session, now):
        """C2S 0x0D action 0xD (F8 step 6). Caller holds the combat lock."""
        if session.get('dead'):
            log.debug('[DEATH] client confirms the death (C2S 0x0D action 0xD)')
            return
        if now - float(session.get('revived_t') or 0.0) < self.REVIVE_GRACE_SECS:
            return
        log.warning(f'[DEATH] {session.get("char_name")!r}: client-side death (C2S 0x0D action 0xD) '
                    f'with {session.get("hp")} HP on the server - opening the death dialog')
        self._player_death(session, 'client-side death (C2S 0x0D action 0xD)', client_side=True)

    # C2S 0x0D action event (bits 12-15 = the victim's +0x9DC) of a monster hit -> the monster
    # stat it hits with. Only the victim's own client detects it (a type-4 attacker is checked
    # against victim uid == local uid only, 0x416EFF..0x416F20) and the field client never
    # subtracts HP itself (0x41A6C2). Its damage estimate (FUN_004194f0, switch 0x419619,
    # table 0x41A954) first remaps 5 -> 4, 8 -> 7, 10 -> 9 (0x419549), then uses Body_Atk for
    # 1/6 (contact 0x416DD6; 1 while guarding: + the shield's guard Def), Weak_Atk for 7 and
    # Strong_Atk for 4/9 (4 guarded: + guard Def); 8 and 10 are attack A / B landing on an
    # airborne victim (0x4172ED/0x4172F5). Only 2 and 3 - a weak swing into a guard - return
    # with no damage and no digit. (5 was wrongly taken for a no-damage guard before the
    # damage-formula port: a Monkey Soldier's event 5 on TestHero shows 17 in the exe.)
    MOB_HIT_STATS = {1: 'body_atk', 6: 'body_atk', 7: 'weak_atk', 8: 'weak_atk', 4: 'strong_atk',
                     5: 'strong_atk', 9: 'strong_atk', 10: 'strong_atk', 2: None, 3: None}
    MOB_HIT_KINDS = {'body_atk': 'contact', 'weak_atk': 'attack', 'strong_atk': 'strong attack'}
    # The swing events (attack A 7/8, attack B 9/10, and attack B into the victim's guard 4/5 -
    # FUN_00416ab0 writes 4/5 in the strong-swing catch, 0x417B12/0x417B1E; they hurt since the
    # damage-formula port, 5 being 4). With MOB_AGGRO on, a swing only hurts when the server
    # commanded that monster an attack of the swing's OWN kind within MOB_ATTACK_EVENT_SECS -
    # attack A for 7/8, attack B for 4/5/9/10 (P13 boss-b3, bosses.swing_age). The client CAN
    # swing an un-hit mob by itself: its wander (FUN_004185b0) rolls attack A / B / dash
    # into +0x947 (0x418C2A..0x418C70, r % 5 against the AI flags) and copies it into the
    # motion input for the whole 3-6 s wander period (0x41926A..0x419274) while +0x971 == 0,
    # so a Monkey Soldier (AI[0] set) swings about one decision in five and the victim's own
    # client draws the event-7 hit (livetest bug 1). The client patch at 0x419272 (74 06 ->
    # EB 06, patch_2009.py AGGRO_RULES) stops the wander from taking the rolled motion; this
    # filter stays for unpatched clients. 1 / 6 are body contact.
    # The client checks contact BEFORE the swing (FUN_00416ab0 l.103 vs l.427, every 30 ms
    # substep): a touch sets the victim's +0x9DC = 6, which the attack-A check skips, and the
    # ~0.5 s hurt state + 270 ms i-frames that follow cover the swing's hit frame. So body
    # contact is how a player pinned by a swinging mob gets hurt (P7 live L2: 407 event-6
    # reports, no event 7, in 226 s of "attack A"); event 7 comes when it steps in from a walk.
    MOB_SWING_EVENTS = frozenset((4, 5, 7, 8, 9, 10))
    MOB_CONTACT_EVENTS = frozenset((1, 6))

    def _monster_contact(self, session, rec, action, now):
        """A monster's hit reported by the victim's own client (config MOB_CONTACT_DAMAGE): the
        stat of the event (MOB_HIT_STATS) against the player's defense - the client's formula
        (_monster_hit_player; DAMAGE_FORMULA 'placeholder': stat - the equipment Def sum,
        combat.body_damage) - through damage_player: S2C 0x28, or the death at 0. Two rate
        limits (livetest bug 2): body contact (events 1/6) at most once per
        MOB_CONTACT_MIN_SECS per (victim, monster) (_contact_slot) - also from a monster in a
        commanded attack, whose touch is the only hit a pinned player's client reports (P7
        live L2) - and swings (4/5, 7..10) at most once per MOB_SWING_MIN_SECS per (victim,
        monster) (_swing_slot). None from a monster under a control effect, and (config
        MOB_CONTACT_AGGRO_ONLY, with MOB_AGGRO) none from a monster that is not after this
        player - un-provoked wandering mobs are harmless to walk through, as in retail. A hit
        on the player never changes aggro. Caller holds the combat lock and the map's
        monster lock (_combat).

        world-shared-monsters: the mob is the one every client on the map holds, and the
        chase words make it red and touchable on ALL of them (the aggro-rules patch gates
        contact on +0x971, which every 0x2A sets), so each client may report a touch. The
        server still hurts only a player the mob is after - its target or on its hate list
        (he hit it) - so a mob A provoked bumps into B harmlessly (P5 exit criterion 5)."""
        src = int(rec.get('event_source_uid', 0))
        mob = (session.get('monsters') or {}).get(src)
        if mob is None or not mob.alive or session.get('dead'):
            return
        if action not in self.MOB_HIT_STATS:
            log.debug(f'[DAMAGE] {mob.name} uid={mob.uid:#x}: C2S 0x0D action {action} is no monster hit')
            return
        stat = self.MOB_HIT_STATS[action]
        if stat is None:
            log.info(f'[DAMAGE] {mob.name} uid={mob.uid:#x}: guarded hit (C2S 0x0D action {action}), no damage')
            return
        if debuffmod.controlled(mob, now):
            log.info(f'[DAMAGE] {mob.name} uid={mob.uid:#x} is under a control effect: no contact hit')
            return
        if self.config.get('MOB_CONTACT_AGGRO_ONLY') and self._mob_ai_enabled():
            me = P.session_uid(session)
            if mob.aggro_uid != me and me not in mob.hate:
                log.debug(f'[DAMAGE] {mob.name} uid={mob.uid:#x} is not after this player: '
                          f'touch (C2S 0x0D action {action}) ignored')
                return
        if (action in self.MOB_SWING_EVENTS and self._mob_ai_enabled()
                and bossmod.swing_age(mob, action, now) > self.MOB_ATTACK_EVENT_SECS):
            # P13 boss-b3: the swing's OWN kind must have been commanded - 7/8 after an attack
            # A, 4/5/9/10 after an attack B (bosses.swing_age). A Strong event from a mob the
            # server never told to swing B (Monkey Lord; MOB_ATTACK_B off) is the client's own
            # wander, and a mismatch here is also what a wrong lo 15/16 assumption would show.
            log.info(f'[DAMAGE] {mob.name} uid={mob.uid:#x}: swing event {action} with no attack '
                     f'{bossmod.swing_kind(action)} commanded in the last {self.MOB_ATTACK_EVENT_SECS:g} s '
                     f'- ignored')
            return
        if action in self.MOB_CONTACT_EVENTS:
            # Always the contact slot, whatever the mob was commanded (P7 live L2): the
            # client resolves an overlap as contact before the swing check, so a pinned
            # player only ever reports event 6 (MOB_SWING_EVENTS comment).
            if not self._contact_slot(session, mob.uid, now):
                return
        elif not self._swing_slot(session, mob.uid, now):
            return
        reason = f'{mob.name} uid={mob.uid:#x} {self.MOB_HIT_KINDS[stat]} (C2S 0x0D action {action})'
        if self._client_damage():
            self._monster_hit_player(session, mob, action, reason)
            return
        dmg = combat.body_damage(getattr(mob, stat), combat.defense_power(self._session_char(session)))
        self.damage_player(session, dmg, attacker=mob.uid, reason=reason)

    def _contact_slot(self, session, mob_uid, now):
        """True (and the slot is taken) when a body-contact hit (events 1/6) of monster
        `mob_uid` on this player may land: at most one per MOB_CONTACT_MIN_SECS per (victim,
        monster). The client reports a touch after every 270 ms i-frame window (0x41531A) and a
        chasing mob walks back and forth through the player, so one shared 0.5 s slot let
        contact drain HP about every 0.54 s (livetest bug 2). session['contact_t'] maps
        monster uid -> the last accepted contact. Caller holds the combat lock."""
        return self._rate_slot(session, 'contact_t', mob_uid, now,
                               float(self.config.get('MOB_CONTACT_MIN_SECS', 1.2)))

    def _swing_slot(self, session, mob_uid, now):
        """True (and the slot is taken) when a swing hit (events 4/5, 7..10) of monster
        `mob_uid` on this player may land: at most one per MOB_SWING_MIN_SECS per (victim,
        monster), so two monsters swinging at him together both hurt - his client drew both
        digits and never takes HP off itself. session['swing_t'] maps monster uid -> the last
        accepted swing. Caller holds the combat lock."""
        return self._rate_slot(session, 'swing_t', mob_uid, now, self.MOB_SWING_MIN_SECS)

    @staticmethod
    def _rate_slot(session, key, mob_uid, now, window):
        """The per-(victim, monster) rate limit behind _contact_slot / _swing_slot:
        session[key] maps monster uid -> the last accepted hit (anything else there, e.g. the
        float of an older server, is replaced); entries past `window` are pruned."""
        slots = session.get(key)
        if not isinstance(slots, dict):
            slots = session[key] = {}
        last = slots.get(mob_uid)
        if last is not None and now - last < window:
            return False
        for uid in [u for u, t in slots.items() if now - t >= window]:
            del slots[uid]
        slots[mob_uid] = now
        return True

    def _monster_hit_player(self, session, mob, action, reason):
        """DAMAGE_FORMULA 'client': a monster's hit on the player by the client's own formula
        (damage.damage) - its Body / Weak / Strong_Atk against his DERIVED Def (the armour's Def
        scaled by SPR and class; + the shield's raw guard Def for events 1 and 4/5), level
        scaled, + its element (hni type 4..7) against his DEX-scaled resist, >= 1, capped at his
        HP. His buffs: Holy Protection takes the hit (nothing, no digit), Magic Shield puts it
        on MP while he has MP (S2C 0x44), Wicked Protection sends (0.3 + 0.05 rank) of B back to
        the monster as an arg6 hit. Caller holds the combat lock and the map's monster lock."""
        vic = self._player_stats(session)
        att = dmgmod.monster_stats(mob)
        hp = session.get('hp')
        hit = dmgmod.damage(att, vic, action, cap=int(hp) if hp else None,
                            vic_buffs=dmgmod.buff_ids(session), vic_mp=int(session.get('mp') or 0))
        reason = f'{reason} {self._hit_text(action, hit)}'
        if hit.reflect and mob.alive:
            # the client deals the reflection inside the victim's own damage call, before his
            # HP changes: the monster takes it first (a kill is his)
            back = dmgmod.damage(vic, att, 0, cap=mob.hp, direct=hit.reflect)
            self._damage_monster(session.get('sock'), session, mob, back.total,
                                 f'Wicked Protection reflection {self._hit_text(0, back)}')
        if hit.total <= 0:
            log.info(f'[DAMAGE] {session.get("char_name")!r}: {reason}: no damage')
        elif hit.to_mp:
            self._damage_player_mp(session, hit.total, reason)
        else:
            self.damage_player(session, hit.total, attacker=mob.uid, reason=reason)

    def _damage_player_mp(self, session, amount, reason):
        """A hit Magic Shield took: `amount` comes off MP (absolute S2C 0x44), none off HP.
        Returns the new MP, or None for a dead / out-of-world / socketless session."""
        with self._combat_lock(session):
            sock = session.get('sock')
            if session.get('dead') or not session.get('in_world') or sock is None:
                return None
            old = int(session.get('mp') or 0)
            new = max(0, old - max(0, int(amount)))
            session['mp'] = new
            hpmp.regen_interrupt(session, time.monotonic(), self.config.REGEN_SECS)
            self._send_mp(sock, session, new)
        log.info(f'[DAMAGE] {session.get("char_name")!r}: {reason}: Magic Shield -{amount} MP {old}->{new}')
        return new

    # ---- player death and revive (cs-player-death, F8) ----
    def damage_player(self, session, amount, *, attacker=None, reason='damage'):
        """F8 steps 1-3: THE entry point for server-side damage to a player (dev !damage /
        !die, monster contact; monster AI, DoT and PvP rules later). hp = max(0, hp - amount);
        a survivor gets the absolute S2C 0x28 - never 0x28 hp=0, which leaves a corpse with no
        dialog (F8 step 1) - and 0 HP is _player_death. Ignored (None) for a dead, out-of-world
        or socketless session: S2C 0x3E derefs scene+0x970 with no null check. A hit restarts
        the idle HP-regen window (the hurt reaction is not idle). Returns the new HP."""
        amount = max(0, int(amount))
        now = time.monotonic()
        with self._combat_lock(session):
            sock = session.get('sock')
            char = self._session_char(session)
            if session.get('dead') or not session.get('in_world') or sock is None or char is None:
                return None
            if not session.get('max_hp'):
                hpmp.refresh(session, char)
            old = int(session.get('hp') or 0)
            new = max(0, old - amount)
            session['hp'] = new
            hpmp.regen_interrupt(session, now, self.config.REGEN_SECS)
            if new > 0:
                self._send_hp(sock, session, new)
            else:
                self._player_death(session, reason, attacker=attacker)
        log.info(f'[DAMAGE] {session.get("char_name")!r}: {reason}: -{amount} HP {old}->{new}'
                 f'/{session.get("max_hp")}')
        return new

    def _player_death(self, session, reason, *, attacker=None, client_side=False):
        """F8 steps 2-5 (caller holds the combat lock):

        - dead; HP 0; every buff, trap and pending trap trigger forgotten without a packet
          (the client memsets both slot tables in action case 0xD, FUN_00412360);
        - S2C 0x3E (0 B): death animation, HP 0 and the death dialog, window 0x79 "You've run
          out of health ... resurrect in town" with its one "Revived" button (live
          combat_skill#20). A local 0x29 is NOT a way to do this or to revive: the local player
          is a type-3 entity and FUN_00413920 case 0x16 revives only type 4 (see
          _handle_revive_request);
        - the exp penalty (config DEATH_EXP_PENALTY): FUN_0041a230's own amount
          (combat.death_penalty, levels 10..98, never a level down) as a negative S2C 0x21 -
          the 0x3E path applies none client-side. Not for a client-side death: that client
          already ran FUN_0041a230 on its own copy;
        - observers: S2C 0x29 {uid, 0x7FFFFFFF, x, y} (a corpse that stays down);
        - every monster chasing him goes back to the client wander (S2C 0x9E to every client
          on the map, monster aggro);
        - the record is saved with HP 0 at once, so a logout from the dialog comes back
          revived (_handle_enter_world)."""
        sock = session.get('sock')
        session['dead'] = True
        session['reviving'] = False
        session['hp'] = 0
        session['dead_t'] = time.monotonic()
        # trade-cancel-lifecycle: a corpse does not trade - 0x49 to both, the escrow released
        # (trade.Trades.cancel takes only its own lock, after this combat lock).
        self.trade.cancel(session, f'death ({reason})')
        buffmod.clear(session)
        session.pop('pending_trap', None)
        if sock is not None and session.get('in_world'):
            P.send(self, sock, session, '0x3E', {})
        self._release_aggro(session, P.session_uid(session), 'its target died')
        penalty = 0
        char = self._session_char(session)
        # premium_cash-use-generic: an active 1891 "Waive EXP Penalty" (a period cash record,
        # cashuse.waives_penalty) spares the server's penalty.
        if not client_side and self.config.DEATH_EXP_PENALTY and char is not None \
                and not self.cashuse.waives_penalty(session):
            with self.store.lock:
                exp = char.get('exp', 0)
            penalty = combat.death_penalty(exp)
            if penalty:
                self.grant_exp(session, -penalty)
        pos = session.get('pos') or (0.0, 0.0)
        self._send_to_observers(session, '0x29', {
            'uid': P.session_uid(session) or 0, 'respawn_tick': MOB_RESPAWN_HOLD_TICK,
            'respawn_x': max(0, int(pos[0])) & 0xFFFFFFFF, 'respawn_y': max(0, int(pos[1])) & 0xFFFFFFFF})
        self._save_world_state(session, reason='death')
        log.info(f'[DEATH] {session.get("char_name")!r} died on map {session.get("current_map")} ({reason})'
                 + (f' by uid {attacker:#x}' if attacker else '')
                 + (f', -{penalty} exp' if penalty else '')
                 + ' -> 0x3E death dialog' + (' (client-side death: no exp penalty)' if client_side else ''))

    def _revive_point(self, map_code):
        """(map, x, y) a death on `map_code` revives at (combat.revive_point)."""
        return combat.revive_point(map_code, EC.portals(),
                                   start=(self.config.START_MAP, self.config.START_X, self.config.START_Y),
                                   overrides=self.config.REVIVE_POINTS)

    def _revive_hp(self, max_hp):
        """The partial HP a revive restores (config REVIVE_HP_PCT of the maximum, at least 1)."""
        return max(1, int(max_hp or 0) * int(self.config.REVIVE_HP_PCT) // 100)

    def _handle_revive_request(self, sock, session, payload, no_enc=False):
        """C2S 0x2E DeathReviveRequest (0 B; spec 0x4484CC/0x2E, live combat_skill#21): the
        death dialog's one button. The client closes the dialog itself and sends no choice,
        so the answer is always the revive. Not dead (a repeated click after the revive
        landed) is ignored; the MUST_REPLY refusal re-opens the dialog for a dead player whose
        revive could not be made."""
        ok, info = self.revive_player(session, reason='revive (C2S 0x2E)')
        if not ok:
            log.info(f'[0x2E] death revive request - ignored: {info}')

    def revive_player(self, session, *, reason='revive'):
        """F8 steps 8-9. Revive re-derived from the client (spec correction C16): FUN_00413920
        case 0x16 revives by itself only when entity+0x98 == 4 - a 0x1A template entity -
        once (int)(+0xE5C - scene clock) < 0; a type-3 entity (every 0x07 player, the local
        one included) has only the room branches (map +0x70 == 1 with the room host flag
        scene+0xF24 and 20 s dead; or scene+0xF24 with +0xE76 set), both dead in the field.
        That is why the live respawn_tick = 0 test (#19) left the player dead: the gate is the
        entity type, not the tick compare. So a revive is always an explicit map load:

            hp = REVIVE_HP_PCT of max, MP as it was; MapTransfer (0x08 -> 0x03 -> 0x07 ->
            0x28/0x44 -> 0x1A) to the revive point, which also closes window 0x79 and
            recreates the local player alive and controllable. A revive point on the same map
            is a full reload too: 0x08 frees every entity whatever the map code.

        Returns (True, (map, x, y) he landed on - the floor point) or (False, why). A failed
        transfer leaves the player dead (HP 0) so he can ask again."""
        sock = session.get('sock')
        char = self._session_char(session)
        with self._combat_lock(session):
            if not session.get('dead'):
                return False, 'not dead (a repeated click after the revive)'
            if session.get('reviving'):
                return False, 'a revive is already in flight'
            if char is None or sock is None:
                return False, 'no character or socket'
            session['reviving'] = True
            d = hpmp.refresh(session, char)
            dest = self._revive_point(session.get('current_map'))
            died_on = session.get('current_map')
            session['hp'] = self._revive_hp(d.max_hp)
        ok = False
        try:
            ok = self._map_transfer(sock, session, dest[0], dest[1], dest[2], reason='revive',
                                    no_enc=session.get('no_enc', True))
        finally:
            with self._combat_lock(session):
                session['reviving'] = False
                if ok:
                    session['dead'] = False
                    session['revived_t'] = time.monotonic()
                else:
                    session['hp'] = 0
        if not ok:
            return False, f'the MapTransfer to {dest} was refused'
        dest = (dest[0], *self._arrival_point(*dest))          # the floor point he landed on
        log.info(f'[DEATH] {session.get("char_name")!r} revived ({reason}): map {died_on} -> map {dest[0]} '
                 f'at ({dest[1]:g}, {dest[2]:g}) with {session.get("hp")}/{session.get("max_hp")} HP')
        return True, dest

    # ------------------------------------------------- world-persistence ---
    def _save_world_state(self, session, reason='world state'):
        """Write this session's live map, position and HP/MP into its character record
        (world-persistence). Called on disconnect and from the world-state tick; a map
        transfer saves its own destination before the state packets go out.

        session['pos'] is the best position the server has: the point enter world spawned
        the player on, then every portal arrival, then the walk dead-reckoned from every C2S
        0x0D and each point an interact tail named (world-position-estimate, presence.py) -
        a relog lands on that estimate, which sits on an EN floor line."""
        char = self._session_char(session)
        if char is None:
            return False
        hp, mp = R.vitals(session, char)
        pos = session.get('pos')
        x, y = (float(pos[0]), float(pos[1])) if pos else (None, None)
        # cs-buffs: the running slots with the time they have left, so a relog re-sends each
        # one with that remaining time (P3 exit criterion 3). None = no buff model yet.
        with self._combat_lock(session):
            kept = (buffmod.persist(session, time.monotonic())
                    if isinstance(session.get('buffs'), list) else None)
        return self.store.save_world_state(char, map_code=session.get('current_map'),
                                           x=x, y=y, hp=hp, mp=mp, buffs=kept, reason=reason)

    # Desync fix P2: how often the settle check runs (presence.settle_node). With the default
    # RELAY_SETTLE_AFTER_MS 450 a settle node goes out 450..600 ms after the mover's last 0x0D.
    PRESENCE_SETTLE_TICK_SECS = 0.15
    # A mover whose settle raises is logged at most once per this many seconds (the tick runs
    # every 0.15 s); the next line counts the failures in between.
    PRESENCE_SETTLE_FAIL_LOG_SECS = 30.0

    def _tick_presence_settle(self, now=None):
        """'presence-settle' ticker (desync fix P2, config RELAY_SETTLE_NODE; tick scheduler,
        world lock held - the lock order is world_lock -> move lock -> presence_lock ->
        send_lock): presence.settle_node for every in-world mover, which sends the clients holding
        him his final idle node again with a long hold, once per stop. Each mover is on his
        own: one that raises (a PacketError building a holder's 0x1B, ...) is logged
        (_settle_failed, rate-limited) and the pass goes on with the next. Returns the packets
        sent (tests drive it with an explicit clock)."""
        if not self.config.get('RELAY_SETTLE_NODE', True):
            return 0
        now = time.monotonic() if now is None else now
        sent = 0
        for session in self.world.in_world_sessions():
            try:
                sent += presence.settle_node(self, session, now)
            except Exception as e:                    # noqa: BLE001 - one mover must not end the pass
                self._settle_failed(session, e, now)
        return sent

    def _settle_failed(self, session, error, now):
        """One log line for a mover whose settle raised - at most one per
        PRESENCE_SETTLE_FAIL_LOG_SECS per mover, so a mover that fails on every 0.15 s pass
        cannot flood the log; the failures in between are counted into the next line.
        Returns True when a line was written."""
        uid = session.get('uid')
        entry = self._settle_fail_log.get(uid)
        if entry is not None and now - entry[0] < self.PRESENCE_SETTLE_FAIL_LOG_SECS:
            entry[1] += 1
            return False
        more = (f' ({entry[1]} more in the {now - entry[0]:.0f} s since the last line)'
                if entry is not None and entry[1] else '')
        self._settle_fail_log[uid] = [now, 0]
        log.warning(f'[MOVE] settle node for {session.get("char_name") or session.get("username")!r} '
                    f'uid {uid} failed: {type(error).__name__}: {error}; the pass goes on{more}')
        return True

    def _tick_world_state(self):
        """Mirror every in-world session into the store (world-persistence: "and every
        60 s"). Runs on the tick scheduler under world_lock, so it takes no session lock:
        it only reads ints the session thread writes atomically, and the debounced save
        then puts them on disk."""
        saved = 0
        for session in list(self.sessions.values()):
            if session.get('in_world') and self._save_world_state(session, reason='world tick'):
                saved += 1
        if saved:
            log.debug(f'[STORE] world-state tick: {saved} character(s) updated')
        return saved

    def _session_account(self, session):
        """The store's account record for this session (uid, gender, manner: the fields
        every player record and the 0x02 list read, login_character.md 3.1)."""
        return self.store.account(session.get('username')) or {}

    def _session_char(self, session):
        """Resolve the store's character dict for this session (mirrors enter-world)."""
        with self.store.lock:
            chars = self.store.characters(session.get('username'))
            name = session.get('char_name')
            if name:
                for c in chars:
                    if c.get('name') == name:
                        return c
            return chars[0] if chars else None

    # ------------------------------------------------------------------------
    # F6 MapTransfer: ONE map load for every caller (world-maptransfer, and its
    # aliases premium_cash-map-replay-helper and pvp-warp-refactor).
    # ------------------------------------------------------------------------
    # The lead opcode is the packet that tears the client's world down before the rebuild.
    # All four carry the same 6-byte {u16 map_code, u32 game_time_ms} body: 0x08 is the
    # field change-map, 0x5E / 0xA3 / 0xA4 are the PvP returns (each draws its own popup
    # first). None = enter world, which must NOT get one: an 0x08 there put the client in a
    # re-map-load loop (2026-06-09 experiment, kept in the enter-world handler).
    LEAD_OPCODES = ('0x08', '0x5E', '0xA3', '0xA4')
    # 2009 (spec_2009 0x08, client-2009-world): the three PvP-return opcodes are gone (no
    # handler, s2c_format_diff gone_in_2009) and 0x08 carries a leading u8 reason instead:
    # 0 = a normal map change (no popup), 1 = "Connection to PVP server was lost" (2008 0x5E),
    # 2 = "You were kicked out." (new), 3 = room deleted, bad name (0xA3), 4 = manner penalty
    # (0xA4). Callers keep naming the 2008 lead; _lead_packet maps it.
    LEAD_REASON_2009 = {'0x08': 0, '0x5E': 1, '0xA3': 3, '0xA4': 4}

    def _lead_packet(self, lead, map_code, clock):
        """(S2C key, payload) of a map-load lead in this server's build."""
        fields = {'map_code': map_code, 'game_time_ms': clock}
        if self.client_build == cfgmod.BUILD_2009:
            fields['reason'] = self.LEAD_REASON_2009[lead]
            lead = '0x08'
        return lead, P.build(lead, fields, client_build=self.client_build)

    # Portal guards (world-portal-guards, world_movement_npc.md F3 step 2). The client's own
    # gate stamps 2500 ms, so a second 0x7E inside this window is a repeat, not a new press.
    PORTAL_COOLDOWN_SECS = 2.0
    # The map clock the client keeps at scene+0xF1C: set by S2C 0x03 game_clock_ms and 0x08
    # game_time_ms, +30 per logic frame, and carried back in every C2S 0x0D as
    # logic_elapsed_ms. Seeded to the value the live capture shows (item_inventory.md 1.6).
    START_CLOCK_MS = 1000

    def _game_clock(self, session):
        """This session's game clock in ms. One value feeds both the 0x08 lead and the 0x03
        that follows it, so the two packets cannot disagree about the clock they both write."""
        return int(session.get('clock') or self.START_CLOCK_MS) & 0xFFFFFFFF

    def _advance_clock(self, session, elapsed_ms):
        """Follow the client's clock with the logic time each C2S 0x0D reports. Ground-item
        ownership and buff windows are measured against it, so it must not jump backwards on
        a map load - which is why the next 0x03/0x08 sends this value, not a constant."""
        elapsed_ms = max(0, min(60000, int(elapsed_ms or 0)))
        session['clock'] = (self._game_clock(session) + elapsed_ms) & 0xFFFFFFFF
        # The moment both sides agreed on that value (_client_clock extrapolates from it).
        session['clock_t'] = time.monotonic()
        return session['clock']

    # How far _client_clock extrapolates past the last agreed value: a client that sent no
    # C2S 0x0D for longer than this is not ticking (minimised, stalled) as far as we know.
    CLIENT_CLOCK_EXTRAPOLATE_MS = 600000

    def _client_clock(self, session, now=None):
        """Estimate of the receiving client's scene clock NOW (2008 scene+0xF1C, 2009
        +0xF34): the last value both sides agreed on - the one our 0x03/0x08 wrote, plus
        every C2S 0x0D logic_elapsed_ms since - plus the real time since then, because the
        client keeps adding 30 per logic frame while it sends nothing (a 0x0D goes out on
        input changes only). Ground-item drop_time only (ground.client_drop_time): the 15 s
        loot protection is measured against it. _game_clock stays the un-extrapolated value
        the next 0x03/0x08 carries, so the 0x0D sum is never counted twice."""
        base = self._game_clock(session)
        anchor = session.get('clock_t')
        if anchor is None:
            return base
        now = time.monotonic() if now is None else now
        ahead = max(0, min(self.CLIENT_CLOCK_EXTRAPOLATE_MS, int((now - anchor) * 1000)))
        return (base + ahead) & 0xFFFFFFFF

    def _handle_change_map(self, sock, session, payload, no_enc):
        """C2S 0x7E portal {u32 portal_line_index} -> MapTransfer, or nothing at all.

        The index is the client's collision-line index, not a destination: the map file's
        `value_num` is read by the client and never sent (world doc 1.5), so the server
        resolves (map, index) through the portal table.

        Guards (world-portal-guards, F3 step 2). Every refusal sends NOTHING and leaves the
        player standing where they are - the client needs no answer, and any answer that
        re-loads the current map re-creates the local player on a live scene (duplicate
        entity, invisible character, flashing HP - observed):
          1. not in world: a map load is already in flight;
          2. inside PORTAL_COOLDOWN_SECS of the last accepted transfer: the repeat press
             (P2 exit criterion 8: two presses in under a second do one transfer, not two);
          3. index 0 on a map with no genuine index-0 portal: when no collision line
             matches, FUN_0042cf20 tests an uninitialised stack slot and can send 0 (client
             bug, world doc 1.5 step 6). The EN table (world-portal-table-en) knows the 27
             maps whose FIRST collision line is a portal floor (103_0 -> 102, ...); only
             there is a 0 a real portal (and only there can the client bug still warp:
             closing that needs the position check of world-portal-proximity, P11);
          4. (map, index) not in the table. The table is the EN one, generated from the
             client's own .hmi/.hsi (en_maps.py, portals_en.json), so every portal the
             client can send has a row; a miss is logged with the map file's destinations;
          5. destination == the current map.
        """
        rec, err = registry.decode(0x7E, payload, self.client_build)
        if rec is None:
            log.warning(f'[PORTAL] 0x7E ({len(payload)}B) not decoded: {err}')
            return
        portal_code = int(rec.get('portal_line_index', 0))
        cur_map = session.get('current_map', self.config.START_MAP)
        refusal = self._portal_refusal(session, cur_map, portal_code)
        if refusal is not None:
            log.info(f'[PORTAL] map {cur_map} index {portal_code} refused: {refusal}')
            return
        next_map, xpos, ypos = EC.portal(cur_map, portal_code)
        if next_map == EC.STALL_MAP and cur_map != EC.STALL_MAP:
            self._remember_market_return(session, cur_map, portal_code)
        elif cur_map == EC.STALL_MAP and next_map != EC.STALL_MAP:
            back = self._market_return(session)
            if back is not None:
                next_map, xpos, ypos = back
        ax, ay = self._arrival_point(next_map, xpos, ypos)          # the floor point he lands on
        log.info(f'[PORTAL] map {cur_map} ({EC.map_name(cur_map) or map_filename(cur_map)}) index '
                 f'{portal_code} -> map {next_map} ({EC.map_name(next_map) or map_filename(next_map)}) '
                 f'at ({ax:g}, {ay:g})')
        self._map_transfer(sock, session, next_map, xpos, ypos, reason='portal', no_enc=no_enc)

    # world-flea-return: arrivals stand 100 px above the portal's floor line, like every
    # generated portal arrival (e.g. 9701's exit line y 2268 -> arrival y 2168); the map load
    # settles them back onto the line (_arrival_point, livetest bug 5).
    MARKET_RETURN_LIFT = 100.0

    def _remember_market_return(self, session, cur_map, portal_code=None):
        """Entering the flea market (9701): remember where to send the player back. Every
        town's market portal leads into the one shared market, whose only exit tile names
        map 101 (live 2026-09-25: in from Ozi Village, out on The Beginning of the
        Adventure). The return point is the town portal he used (its collision line, 100 px
        above the floor), else where he stood; kept in the character record so a relog
        inside the market still leads home."""
        line = EC.portal_line(cur_map, portal_code) if portal_code is not None else None
        if line is not None:
            x, y = (line[0] + line[2]) / 2.0, line[1] - self.MARKET_RETURN_LIFT
        else:
            x, y = map(float, session.get('pos') or self._warp_point(cur_map)[:2])
        char = self._session_char(session)
        if char is None:
            return
        with self.store.lock:
            char['market_return'] = [int(cur_map), float(x), float(y)]
        self.store.mark_dirty('market return point')
        log.info(f'[PORTAL] flea market entered from map {cur_map}: the exit leads back to '
                 f'({x:g}, {y:g})')

    def _market_return(self, session):
        """(map, x, y) the market's exit portal leads to for this player, or None (no
        remembered town: the map file's own destination, 101)."""
        char = self._session_char(session)
        back = (char or {}).get('market_return')
        try:
            town, x, y = int(back[0]), float(back[1]), float(back[2])
        except (TypeError, ValueError, IndexError):
            return None
        if town == EC.STALL_MAP or not (town in map_codes or self._client_has_map(town)):
            return None
        return town, x, y

    def _portal_refusal(self, session, cur_map, portal_code):
        """Why this 0x7E must be ignored, or None when it may be honoured. Split out of the
        handler so the guards can be tested without a socket."""
        if not session.get('in_world'):
            return 'not in world (a map load is in flight)'
        if session.get('dead'):
            # cs-player-death: a corpse leaves only through the death dialog's revive.
            return 'the player is dead (revive through the death dialog, C2S 0x2E)'
        since = time.monotonic() - float(session.get('last_transfer_t') or 0.0)
        if since < self.PORTAL_COOLDOWN_SECS:
            return (f'{since:.2f}s since the last transfer, cooldown is '
                    f'{self.PORTAL_COOLDOWN_SECS:.1f}s (repeated portal key)')
        target = EC.portal(cur_map, portal_code)
        if portal_code == 0 and target is None:
            return ('portal index 0 (the client sends it from an uninitialised slot when no '
                    f'collision line matches; map {cur_map} has no index-0 portal line)')
        if target is None:
            # The EN map file itself says where this map can lead: logging those candidates
            # with the miss is what makes a gap in the table (or a KR fallback table) fixable.
            dests = EC.map_destinations(cur_map)
            return (f'no portal table entry {cur_map}_{portal_code} in {EC.portals_source()} '
                    f'(EN map value_num candidates: {dests or "none / map file not readable"})')
        if target[0] == cur_map:
            return f'destination {target[0]} is the current map'
        return None

    # ---- village transfer (world-village-transfer; world_movement_npc.md 1.6 / F5) ----
    def _handle_village_transfer(self, sock, session, rec, no_enc=False):
        """C2S 0x5D {u8 village_index, u16 fee} (Garan Maria, window 600 -> confirm 0x237)
        -> S2C 0x81 {1, u64 gold} + MapTransfer to the town, or S2C 0x81 {0}.

        The client computed and displayed the fee, checked `gold >= fee` and sends no second
        request (no wait dialog). 0x81 {1} only sets the absolute gold and plays the coin
        sound - it does NOT move the player (live world_movement_npc#31) - so the server
        moves him with the F6 MapTransfer right after it (0x08 -> 0x03 -> 0x07 -> vitals ->
        0x1A, spec correction C1: never a bare 0x08). Every refusal is exactly 0x81 {0}
        ("Village transfer failed. Please try again in a few minutes.", live #32) and nothing
        moves or is charged:
          - not in world (a map load is in flight) or dead;
          - village_index outside 1..10;
          - the server's own fee (village.fee: FUN_00422050 + the manner discount, from the
            session's map and the account manner the client holds at scene+0xEE0) is 0:
            already in that village's group, or not on a village-group map at all;
          - inside VILLAGE_COOLDOWN_SECS of this session's last village transfer;
          - the stored gold cannot pay the fee. The client's label was ahead of the store
            then, so an S2C 0x3F resync follows the 0x81 {0} (the refusal reads 1 byte and
            leaves the label as it was).
        A client fee that differs from the server's is logged and the SERVER's fee is
        charged (F5 step 2): gold drops by exactly what the store says, once, under the
        combat lock the driver credits gold under, and 0x81 carries the new absolute."""
        index = int(rec.get('village_index', 0))
        sent_fee = int(rec.get('fee', 0))
        cur_map = int(session.get('current_map') or self.config.START_MAP)
        fee = village.fee(cur_map, index, self.store.manner(session.get('username')))
        why = self._village_refusal(session, index, fee)
        dest = None
        if why is None:
            dest = self._village_arrival(index)
            if dest is None:
                why = f'no arrival point for village {index} (map {village.town_map(index)})'
        wallet = self._wallet_of(session)
        if why is None and wallet is None:
            why = 'no character in this session'
        short = False
        if why is None:
            if sent_fee != fee:
                log.warning(f'[VILLAGE] {session.get("char_name")!r}: client fee {sent_fee} != server '
                            f'fee {fee} (map {cur_map} -> village {index}); charging {fee}')
            with self._combat_lock(session):
                if wallet.pay(fee):
                    gold, _victy = self._wallet_commit(session, wallet, 'village transfer')
                else:
                    short = True
                    why = f'gold {wallet.gold} < fee {fee}'
        if why is not None:
            log.info(f'[VILLAGE] map {cur_map} -> village {index} ({village.name(index)}) refused: {why}')
            P.send(self, sock, session, '0x81', {'result': 0})
            if short:
                self._send_gold(sock, session)
            return
        town, x, y, how = dest
        session['last_village_t'] = time.monotonic()
        ax, ay = self._arrival_point(town, x, y)                    # the floor point he lands on
        log.info(f'[VILLAGE] {session.get("char_name")!r}: map {cur_map} -> {village.name(index)} '
                 f'(map {town}) fee {fee} -> gold {gold}; arrival ({ax:g}, {ay:g}) {how}')
        P.send(self, sock, session, '0x81', {'result': 1, 'gold': gold})
        try:
            moved = self._map_transfer(sock, session, town, x, y, reason='village', no_enc=no_enc)
        except Exception:
            log.exception(f'[VILLAGE] transfer to map {town} failed')
            moved = False
        if not moved:
            # Nothing moved, so nothing is paid (and nothing counts toward the cooldown): put
            # the fee back and the label with it.
            session.pop('last_village_t', None)
            with self._combat_lock(session):
                wallet.earn(fee)
                self._wallet_commit(session, wallet, 'village transfer refund')
            self._send_gold(sock, session)

    def _village_refusal(self, session, index, fee):
        """Why this 0x5D must get 0x81 {0}, or None (the gold check is the atomic pay)."""
        if not session.get('in_world'):
            return 'not in world (a map load is in flight)'
        if session.get('dead'):
            return 'the player is dead'
        if not village.VILLAGE_MIN <= index <= village.VILLAGE_MAX:
            return f'village index {index} outside {village.VILLAGE_MIN}..{village.VILLAGE_MAX}'
        if fee == 0:
            return (f'fee 0: map {session.get("current_map")} is in that village\'s group or in no '
                    f'village group (the client opens no dialog for it)')
        since = time.monotonic() - float(session.get('last_village_t') or 0.0)
        cooldown = float(self.config.VILLAGE_COOLDOWN_SECS)
        if since < cooldown:
            return f'{since:.1f}s since the last village transfer, cooldown is {cooldown:g}s'
        return None

    def _village_arrival(self, index):
        """(town, x, y, how) for village `index` (village.arrival): config VILLAGE_ARRIVALS,
        else next to Garan Maria in the town's EN map file, else the town's revive point
        (101: the start point; 201/401: the first incoming portal's arrival)."""
        start = (int(self.config.START_MAP), float(self.config.START_X), float(self.config.START_Y))

        def revive_town_point(town):
            rmap, rx, ry = combat.revive_point(town, EC.portals(), start=start)
            return (rx, ry) if rmap == town else None

        return village.arrival(index, overrides=self.config.VILLAGE_ARRIVALS,
                               fallback=revive_town_point)

    @staticmethod
    def _arrival_point(map_code, x, y):
        """Where a map load puts the local player: (x, y) settled onto the floor line below it
        (presence.settle with SLOPE_SLACK_PX, as observers' records are, presence.floor_point).
        Portal arrivals (portals_en*.json), the 101 start point and the revive points sit 100 px
        above their floor, and the 2009 client does not drop an idle local player: he floated
        there while the other clients drew him on the floor (livetest bug 5; the first !loot
        pickup missed because of it). A point with no floor line below is kept as it is."""
        return presence.settle(int(map_code), float(x), float(y), presence.SLOPE_SLACK_PX)

    def _map_transfer(self, sock, session, map_code, x, y, *, lead='0x08', reason='portal',
                      no_enc=False):
        """THE map load (roadmap F6). Every caller that moves a player between maps goes
        through here: the 0x7E portal, the dev `!warp`, enter world (lead=None), and the
        village transfer, revive, warp stones, GM /go and PvP returns as they land.

        Live-verified sequence (spec correction C1, world_movement_npc#18): the lead alone
        destroys every entity including the local player and leaves the client on the
        loading overlay for ever - only a restart recovers it. The 0x03/0x07 pair is
        therefore mandatory, and `in_world` is False for exactly as long as the client has
        no local player:

            [lead 0x08/0x5E/0xA3/0xA4]  {map_code, game_time_ms}   6 B, skipped at enter world
                                        (2009: always 0x08 {u8 reason, ...} 7 B, _lead_packet)
            0x03                        the whole character state, from the store
            0x07                        the own player record at the arrival point
            0x28 / 0x44                 CURRENT hp/mp (max here was a free heal per portal)
            0x1A ...                    the destination map's monsters

        The arrival point is settled onto the floor first (_arrival_point, livetest bug 5):
        the 0x07, session['pos'] and the saved map/x/y all carry the floor point, for every
        caller - portal, warp, revive, village, GM /go and enter world.

        Resolve and build BEFORE committing (the F6 opening line): a character that cannot
        be resolved, or a packet that will not encode, must leave the session on the map it
        is already on instead of half-moving it while the client still shows the old map.
        Nothing sleeps: the six 50 ms sleeps this replaces ran on the session's receive
        thread, the thread that has to stay free to read the client's next packet.
        """
        if lead is not None and lead not in self.LEAD_OPCODES:
            raise ValueError(f'{lead}: not a map-load lead opcode ({", ".join(self.LEAD_OPCODES)})')
        if session.get('in_cash_shop'):
            # premium_cash-presence (P8 stage 4, mall.py "Presence"): a player in the mall is
            # hidden from the world and comes back ONLY through his own exit (Mall._leave
            # clears in_cash_shop before its replay). Any other map load (a warp stone, GM
            # /go or !warp, a revive) would drop the 0x08 into the preview scene and put him
            # back in the world with the mall still open server-side.
            log.warning(f'[MAP] {reason} transfer of {session.get("char_name")!r} to map {map_code} refused: '
                        f'in the {mallmod.mall_name(self.client_build)} (only its EXIT brings him back)')
            return False
        char = self._session_char(session)
        if char is None:
            log.warning(f'[MAP] {reason} transfer to map {map_code} without a character in '
                        f'the session - nothing sent')
            return False
        map_code = int(map_code)
        x, y = self._arrival_point(map_code, x, y)

        # --- resolve + build (nothing is committed yet) --------------------------------
        # RegisterLocalPlayer recomputes the maxima of the new local player and clamps the
        # current values to them (FUN_00427d40/FUN_00427f40 with 0): mirror that first, so
        # the 0x07 and the 0x28/0x44 carry what the client will actually hold. A clamp is
        # idempotent, so it is safe even if this transfer is refused below.
        with self._combat_lock(session):
            # cs-buffs (F2): the 0x07 below re-sends every running buff with its remaining
            # time, so a portal keeps them. Traps stay on the old map and records whose time
            # is up are dropped without a packet (the new entity never gets them).
            now = time.monotonic()
            for gone in buffmod.prune_for_map_load(session, now):
                log.info(f'[BUFF] map load drops {gone["id"]} '
                         f'({"trap" if buffmod.is_trap(gone["id"]) else "expired"})')
            # cs-traps: a trigger still waiting for its C2S 0x0D victim belongs to the old map.
            session.pop('pending_trap', None)
            kept_buffs = buffmod.persist(session, now)
            hpmp.refresh(session, char)
            cur_hp, cur_mp = R.vitals(session, char)
        clock = self._game_clock(session)
        lead_key, body_lead = (None, None) if lead is None else self._lead_packet(lead, map_code, clock)
        trades_before = trademod.commits(session)
        grants_before = eventmod.grants(session)
        body_03 = self._build_opcode_03(session, char, current_map=map_code, clock=clock)
        body_07 = self._build_player_spawn(session, char, pos=(x, y))

        # --- leave the old map (F5 before_server_map_load: trade cancel, stall close 0x84
        #     before the lead, 0x06 to the old map's peers) -----------------------------
        self.world.hooks.fire(worldmod.BEFORE_SERVER_MAP_LOAD, self, session,
                              map_code=map_code, reason=reason)
        rebuild = session.pop('rebuild_03', False)
        if trademod.commits(session) != trades_before:
            # The partner's confirm committed this session's trade (it had confirmed first)
            # after the 0x03 above was built and before the hook's cancel could stop it: the
            # bag and gold changed under it (P7 review). The commit bumps the counter under
            # Trades.lock, which the hook's cancel also takes, so after the hooks a commit
            # is either counted here or can no longer happen.
            log.info(f'[MAP] {session.get("char_name")!r}: a trade committed while the map load '
                     f'was being built - 0x03 rebuilt')
            rebuild = True
        if eventmod.grants(session) != grants_before:
            # P13 (events.py): the event tick (or another thread's turn-in) granted a login gift
            # or pushed an event quest after the 0x03 above was built and before the hook
            # cleared events_ready - bag and quest log changed under it. The hook takes the
            # combat lock the grant runs under, so after the hooks a grant is either counted
            # here or can no longer happen (events.py "Once per login").
            log.info(f'[MAP] {session.get("char_name")!r}: an event grant landed while the map load '
                     f'was being built - 0x03 rebuilt')
            rebuild = True
        if rebuild:
            # A hook changed the bag model the 0x03 above was built from: the stall close
            # (market.Market.map_load, F14.1) put its escrow back. The client's window put
            # those items back too (its 0x84 {1} went out first), so the 0x03 must list them.
            # Or the trade commit / event grant just above.
            body_03 = self._build_opcode_03(session, char, current_map=map_code, clock=clock)
        # Off the old map's shared monsters first (world-shared-monsters): from here none of
        # their packets (a despawn/respawn timer, a chase word) reaches this client, whose
        # coming 0x03 frees every entity - a stale 0x06/0x1A would land in the new map load
        # and the new map's own 0x1A would duplicate that uid (S1-07). Their timers stay with
        # the old map and its other players.
        self._clear_map_monsters(session)

        # --- commit --------------------------------------------------------------------
        session['in_world'] = False
        # world-registry (F5): off the old map's instance until the new 0x07 is out, so no
        # broadcast reaches a client that has no scene; on_map_change then tells the groups
        # (the old map's peers get their 0x06 there, world-presence; party HUD map change).
        old_map = self.world.depart(session)
        session['current_map'] = map_code
        # A map load frees every client entity, including the local player, and the
        # allocator hands the block straight back out. The combat driver's cached entity
        # pointer therefore stays "valid" (a recycled block often still reads uid 1 at
        # +0x84) while its position reads return 0.0 - live P2 verification saw hits stop
        # landing after any warp AND the 60 s world save writing the OLD map's coordinates
        # (a character saved on map 102 at a map-201 point, then respawned mid-air).
        # Bumping the epoch makes the driver re-walk the scene list on its next tick.
        self._driver_epoch += 1
        # The arrival point is the best position the server has until the client names one
        # of its own in a C2S 0x0D (world-persistence).
        session['pos'] = (x, y)
        if old_map is not None:
            self.world.hooks.fire(worldmod.ON_MAP_CHANGE, self, session, old_map=old_map,
                                  map_code=map_code, reason=reason)
        if reason != 'enter_world':
            # The portal cooldown counts map CHANGES, not the map load that put the player
            # in the world: a relog standing on a portal tile must still be able to use it,
            # and the client's own 2500 ms gate starts when its load finishes anyway.
            session['last_transfer_t'] = time.monotonic()
        # Persist BEFORE the state packets go out, so what the client is told and what a
        # relog will load cannot disagree.
        self.store.save_world_state(char, map_code=map_code, x=x, y=y, hp=cur_hp, mp=cur_mp,
                                    buffs=kept_buffs, reason=f'{reason} transfer')

        # --- send ----------------------------------------------------------------------
        if body_lead is not None:
            self._send_encrypted(sock, session, P.opcode(lead_key, client_build=self.client_build), body_lead,
                                 use_by_array=no_enc)
        self._send_encrypted(sock, session, 0x03, body_03, use_by_array=no_enc)
        # The 0x03 just (re)set the client's scene clock to `clock` (_client_clock anchors here).
        session['clock_t'] = time.monotonic()
        self._send_encrypted(sock, session, 0x07, body_07, use_by_array=no_enc)
        # Desync fix M1: the 0x07 re-created his entity, +0x949 = the record's direction byte
        # (0x453B80: the local record's idle block, 2 = left) - a fresh action tracker facing
        # that way, so a hit before his first turn knocks the way his copy does, not the way
        # of a key he held on the old map (_hit_knock_facing).
        session['hitstun'] = dict(HS.new_tracker(facing=R.IDLE_MOTION['direction_8bd']), map=map_code)
        # F5: in world again only now - the 0x03 set CMessenger+0x74 (injected SubHandler3
        # packets need it) and the 0x07 re-created the local player. The session joins the
        # map's instance at the same moment: from here on it is in its peers' peers().
        session['in_world'] = True
        if not self.world.arrive(session, map_code):
            log.warning(f'[MAP] {session.get("username")!r} no longer owns uid {session.get("uid")}: '
                        f'not registered on map {map_code} (kicked or replaced mid-transfer)')
        first = not session.get('entered_once')
        session['entered_once'] = True
        # cs-regen: the regen timers live on the entity the 0x07 just recreated (idle,
        # both timers at 0), so a map change restarts the 15 s windows exactly as the client
        # would - and never refills anything by itself.
        with self._combat_lock(session):
            hpmp.regen_reset(session, time.monotonic(), self.config.REGEN_SECS)
        self.world.hooks.fire(worldmod.ON_ENTER_WORLD, self, session,
                              map_code=map_code, reason=reason, old_map=old_map, first=first)
        # CURRENT hp/mp: sending the maxima here healed the player on every portal (F6 step
        # 5) and left the client showing more HP than the record kept.
        self._send_hp(sock, session, cur_hp, no_enc=no_enc)
        self._send_mp(sock, session, cur_mp, no_enc=no_enc)
        # premium_cash-owned-list-sync (F1 step 3): the 0x03 above memset the cash bag tab, so
        # the owned cash list comes back here - after the own 0x07 (its tail needs the local
        # player, 0x441D85) and the 0x28 / 0x44, before the monsters.
        self._send_owned_cash(sock, session, reason=reason, no_enc=no_enc)
        # chat_mail_gm-gift-inbox (P8 stage 2, mall.py): undelivered gifts go to the client's
        # append-only 0x6D queue once per connection (their popups open at the next 0x6A); a
        # map load that finds the queue still holding some relights the HUD gift button with
        # a count-0 0x6D (C20). Nothing at all for an account without pending gifts.
        self.mall.send_gift_queue(sock, session, reason=reason, relight=True)
        self._spawn_map_monsters(sock, session, map_code, no_enc=no_enc)
        # The 0x03 above emptied the client's ground list (spec correction C19): the items
        # still lying on this map come back as S2C 0x11 state 0 (item_inventory.md F6 step 5).
        self._send_ground_list(sock, session, map_code)
        log.info(f'[MAP] {reason} transfer -> map {map_code} ({EC.map_name(map_code) or map_filename(map_code)}) at '
                 f'({x}, {y}) lead={lead or "none"} hp={cur_hp} mp={cur_mp} '
                 f'0x03={len(body_03)}B 0x07={len(body_07)}B')
        return True

    def _send_owned_cash(self, sock, session, *, reason='sync', force=False, no_enc=None):
        """S2C 0x6F, the character's whole owned cash list (premium_cash-owned-list-sync, F1;
        cash.owned_list_packets: 2008 one packet, 2009 mode 0 or 1/2../3 pages). Returns the
        number of records sent, or None when nothing was sent.

        Sent when the character owns a record, when this connection's client may still hold
        some (a non-empty 0x6F went out earlier: `cash_on_client`), or when `force`d (a grant).
        A 0x6F with count 0 to a client that holds nothing purges nothing and rebuilds empty
        lists, so it is skipped: every map-load sequence of a character without cash items
        stays byte-for-byte what it was (live-verified 0x03 0x07 [0x15 0x65] 0x28 0x44 0x1A).
        The client's mall lists survive a map load (only its bag tab is memset), so once it
        was sent a record the empty list still goes out to clear them.

        ROADMAP_2009_ADDENDUM C2 (pet H5): the 0x6F frees every record, the one local +0x1628
        (the worn pet's pet_info) points at included, and only an equipped kind-3 record in the
        new list re-binds it. The worn pet record (cash.equipped_pet - the same record the own
        0x07's grid slot 15 comes from, records.pet_slot_2009) is therefore in EVERY 0x6F, first
        (cash.pet_first), and `pet_bound` remembers the serial this client bound. A later list
        without it would leave +0x1628 dangling: that is logged as an error - taking a worn pet
        off is S2C 0xAC first (it clears +0x1628, and the unequip path clears `pet_bound`
        with it: P15 pet-s2)."""
        char = self._session_char(session)
        if char is None or sock is None:
            return None
        # premium_cash-expiry (F15): a period record whose date passed while its owner was
        # offline / in the mall / loading is removed here, before the list goes out (the 0x6F
        # leaves it out anyway), and told with the client's own words as a 0x15 line after it
        # - not by sending it and a 0x93, which would first print its NEGATIVE remaining time
        # (the 0x6F tail FUN_00465110, cashuse.py "Period items").
        expired = self.cashuse.take_expired(char)
        pages, count = self.cash.owned_packets(char, self.client_build)
        if not count and not force and not session.get('cash_on_client'):
            self.cashuse.expired_notice(sock, session, expired)
            return None
        use_by_array = session.get('no_enc', True) if no_enc is None else no_enc
        worn = None
        if self.client_build == cfgmod.BUILD_2009:
            with self.store.lock:
                pet = cashmod.equipped_pet(char)
                worn = pet['serial'] if pet is not None else None
            bound = session.get('pet_bound')
            if bound is not None and bound != worn:
                log.error(f'[CASH] {session.get("char_name")!r}: this 0x6F lacks the worn pet record {bound:#x} the client '
                          f'bound (+0x1628) - it frees it and nothing re-binds it (ROADMAP_2009_ADDENDUM C2 / pet H5); a '
                          f'worn pet must be taken off with S2C 0xAC before its record leaves the list')
        for fields in pages:
            body = P.build('0x6F', fields, client_build=self.client_build)
            self._send_encrypted(sock, session, 0x6F, body, use_by_array=use_by_array)
        session['cash_on_client'] = bool(count)
        session['pet_bound'] = worn
        bag, catalog = self._bag(session), EC.items()
        stray = [] if bag is None else [e['id'] for tab in invmod.TABS for e in bag.slots(tab)
                                        if getattr(catalog.get(e['id']), 'is_cash', False)]
        if stray:
            # The 0x6F handler purges every cash item (def+0x1F0 != 0) from every bag first; a
            # cash item the bag MODEL holds (a dev !give of a costume) is gone client-side now.
            log.warning(f'[CASH] {session.get("char_name")!r}: the 0x6F purged the bag cash item(s) '
                        f'{stray} client-side; cash items belong in the cash inventory')
        log.info(f'[CASH] {reason}: 0x6F owned list -> {session.get("char_name")!r}: {count} record(s)'
                 f'{f" in {len(pages)} pages" if len(pages) > 1 else ""}'
                 + (f'; worn pet {worn:#x} first (C2)' if worn is not None else ''))
        self.cashuse.expired_notice(sock, session, expired)
        return count

    # ---- the Item Mall / Spark Shop (P8 stage 2; mall.py holds the flows) ----
    def _handle_mall_close(self, sock, session, payload, no_enc):
        """C2S 0x42 ItemMallCloseStorageCommit (the EXIT label of window 0x1CF, live followup
        premium_cash#30) -> the box <-> character moves, S2C 0x6B, the map-load replay back to
        where `!mall` was typed (premium_cash-mall-exit, F7)."""
        self.mall.close(sock, session, payload)

    def _handle_mall_buy(self, sock, session, payload, no_enc):
        """C2S 0x43 single buy / cart / slot extension -> one S2C 0x6C per item, or 0x6C {0 |
        0x0E} (premium_cash-buy, F4)."""
        self.mall.buy(sock, session, payload)

    def _handle_mall_delete(self, sock, session, payload, no_enc):
        """C2S 0x45 CashItemBoxDelete -> S2C 0x6E (premium_cash-box-delete, F5)."""
        self.mall.delete(sock, session, payload)

    def _handle_mall_refresh(self, sock, session, payload, no_enc):
        """C2S 0x46 (both send sites: the window restore after the charge button and the
        refresh control, each only while the charge flag is set) -> exactly one S2C 0x70,
        which clears that flag, so a minimise / restore never repeats it
        (premium_cash-balance-refresh, F3; exit criterion 2)."""
        if not session.get('username'):
            log.warning('[MALL] 0x46 before login; ignored')
            return
        self.mall.refresh(sock, session)

    def _handle_mall_gift(self, sock, session, rec, no_enc=False):
        """C2S 0x47 CashShopSendGift -> S2C 0x71, the recipient's box + gift inbox, 0x79 / 0x6D
        to him online (premium_cash-gift, F6; chat_mail_gm-gift-inbox)."""
        self.mall.gift(sock, session, rec)

    # ---- using cash items (P8 stage 3; cashuse.py holds the flows) ----
    def _handle_cash_item_use(self, sock, session, rec, no_enc=False):
        """C2S 0x48 UseCashItemConfirm -> S2C 0x72 to the owner (and the observer form to the
        holders): hair / colour / eyes recompose the look, a period item is activated, an item
        with a registered effect is consumed; the refusal (also for an item with no modelled
        effect, which is kept) is 0x72 {uid, 0, 0} (premium_cash-use-generic, F8)."""
        self.cashuse.use(sock, session, rec)

    def _handle_cash_rename(self, sock, session, rec, no_enc=False):
        """C2S 0x49 NameChangeRequest -> S2C 0x73 {1, serial} + 0x74 to self and the holders,
        then the world.ON_RENAME hook (friends' 0x0B, the mentor's 0x7B, the party frames), or
        0x73 {0} (premium_cash-rename, F9; ROADMAP_2009_ADDENDUM C4)."""
        self.cashuse.rename(sock, session, rec)

    def _handle_pet_rename(self, sock, session, rec, no_enc=False):
        """2009 C2S 0x4D PetRename (window 0x4CB, a waiting box) -> S2C 0x73 {0}, the planned
        refusal through the one 0x73 builder (ROADMAP_2009_ADDENDUM C5; pets.py, P15 pet-s6)."""
        self.pets.rename(sock, session, rec)

    def _handle_cash_add_option(self, sock, session, rec, no_enc=False):
        """2009 C2S 0x4E CashItemAddOption -> S2C 0xC4 {0} (ROADMAP_2009_ADDENDUM C8; mall.py)."""
        self.mall.add_option(sock, session, rec)

    def _handle_cash_sale_offer(self, sock, session, rec, no_enc=False):
        """2009 C2S 0x80 CashItemSaleOffer (a waiting box) -> S2C 0x71 {is_trade 1, 0x17}: cash
        item sales are not offered (ROADMAP_2009_ADDENDUM C8; mall.py)."""
        self.mall.sale_offer(sock, session, rec)

    def _handle_cash_sale_reply(self, sock, session, rec, no_enc=False):
        """2009 C2S 0x81 CashItemSaleReply (no waiting box): the seller's Cancel (1) -> S2C 0x71
        {is_trade 1, 0x16}, which closes window 0x4B8; the buyer's 0 / 2 -> no reply, no offer
        is ever pending (ROADMAP_2009_ADDENDUM C8; mall.py)."""
        self.mall.sale_reply(sock, session, rec)

    def _handle_cash_stat_reset(self, sock, session, rec, no_enc=False):
        """C2S 0x4A StatResetApply -> S2C 0x76 (premium_cash-stat-reset, F10; C13 / C15)."""
        self.cashuse.stat_reset(sock, session, rec)

    def _handle_cash_megaphone(self, sock, session, rec, no_enc=False):
        """C2S 0x4C MegaphoneMessage -> S2C 0x90 to every player (Super) / the channel, and the
        owner's consume 0x72 (premium_cash-megaphone, F11)."""
        self.cashuse.megaphone(sock, session, rec)

    def _handle_cash_region_warp(self, sock, session, rec, no_enc=False):
        """C2S 0x70 RegionWarpStoneUse -> S2C 0x9A {1, serial} and the map load to the
        destination, or 0x9A {0} (premium_cash-region-warp, F13)."""
        self.cashuse.region_warp(sock, session, rec)

    def _handle_cash_friend_warp(self, sock, session, rec, no_enc=False):
        """C2S 0x71 FriendWarpStoneUse -> S2C 0x9B {1, serial} and the map load beside the
        named character, or 0x9B {0} (premium_cash-friend-warp, F14)."""
        self.cashuse.friend_warp(sock, session, rec)

    @staticmethod
    def _get_portals():
        """The portal table (en_content owns the cache and the GM /update reload, F7)."""
        return EC.portals()

    # ========================================================================
    # In-game opcode handlers (generated + verified by the opcode workflow).
    # Inbound handlers consume/ack so the client never hangs waiting on a reply.
    # ========================================================================
    def _quest_state(self, session):
        """The quests.QuestState of this session's character, or None outside the world.
        It replaces the session keys `quests` / `quests_done` / `quest_progress` /
        `quest_slot`: the log now lives in the character record, so a portal or a relog
        keeps it (quest_cards_misc-quest-state-model, Q-B8)."""
        char = self._session_char(session)
        return None if char is None else questmod.QuestState(char)

    # ------------------------------------------------------------------------
    # The quest loop (quest_cards_misc-accept-rework / -kill-progress / -turnin-0x17 /
    # -abandon-0x1E and the rest of item_inventory-quest-grant-mirror; quest doc F1-F6).
    #
    # The CLIENT is the bookkeeper. On S2C 0x26 it writes its own first empty slot (or files
    # a talk-only quest straight under Completed) and grants the hqi Send items itself; on
    # 0x27 it clears the slot, appends the completed list, adds Money to its gold, removes
    # the Demand items and grants the Reward items (learning a type-3 skill, applying a
    # type-4 class change); on 0x38 it zeroes the slot and keeps the Send items. All four
    # were live-verified by injection (quest_cards_misc#09 / #10 / #11 / #12).
    #
    # So every handler here follows one rule: validate first, then mirror exactly that
    # mutation in the persistent model, then send only the packets the client needs. An
    # extra 0x18 per Send/Reward item duplicated the item and overwrote gold with the
    # server's absolute value (Q-B2/Q-B11, live bug 4), and 0x23/0x19 for Demand items would
    # remove them twice. The one thing 0x27 does NOT do is exp (#11: the EXP bar did not
    # move), so S2C 0x21 stays.
    # ------------------------------------------------------------------------
    def _handle_accept_quest(self, sock, session, payload, no_enc):
        """C2S 0x16 QuestAcceptRequest {u16 quest_id} -> S2C 0x26 [+ 0x59 | + 0x21]
        (quest_cards_misc-accept-rework, F1/F2).

        0x16 is ALWAYS an accept. It is sent only by the offer dialog's "Yes" (Event 2),
        and across all 291 EN quests an Event-2 button occurs in no Ing/Finish/None chain
        (doc E3); the turn-in is C2S 0x17 below. The old handler treated a 0x16 for a held
        quest as a turn-in and counted an empty Demand list as met, which was the S1-11 /
        Q-B5 exploit: accept 26, portal (the 0x03 wiped the client's log), re-accept at the
        NPC, and the server paid Herb x5 + Double Jump with no Pupu killed (reproduced live
        in quest_cards_misc#02).

        The client already applies its own offer filter (FUN_004781C0, doc E2), but it
        filters what it DISPLAYS from the log it currently holds, so every one of those
        gates is re-checked here against the persistent model."""
        quest_id = self._quest_id_of(payload, '0x16')
        if quest_id is None:
            return
        q, state, char = self._quest_request(session, quest_id, '0x16')
        if state is None:                      # character select or a dev probe: no log
            return
        if q is None:
            self._quest_refuse(sock, session, quest_id, 'no such quest in the EN hqi',
                               'Unknown quest.')
            return

        # F1 step 3, in order. Every failure refuses: no 0x26, so the client keeps the log
        # it has (it closes its dialog either way).
        if not q.snpc:
            # ev-e5 (events.py): a quest of a running event's chain is the event's to push
            # (evb Q4: the Accept of quest 158's Finish window may send its 0x16 here).
            if self.events.accept_request(session, q):
                return
            # 45 EN quests have SNPC 0 and are offered by no NPC at all (open question Q13).
            self._quest_refuse(sock, session, quest_id, 'SNPC 0: no NPC offers it')
            return
        if state.is_active(quest_id):
            self._quest_refuse(sock, session, quest_id, 'already in an active slot (S1-11)',
                               'You are already on this quest.')
            return
        if state.exhausted(quest_id, q):
            # FUN_00477FF0: done means completed at least once AND (Repeat 0 or times >=
            # Repeat). 152 EN quests are repeatable and must come back (Q-B4).
            self._quest_refuse(sock, session, quest_id,
                               f'already completed {state.times(quest_id)}x (Repeat {q.repeat})',
                               'You have already completed this quest.')
            return
        level = int(session.get('level') or R.level_of(char))
        if not q.start_lev <= level <= q.end_lev:
            self._quest_refuse(sock, session, quest_id,
                               f'level {level} outside {q.start_lev}..{q.end_lev}',
                               'Your level does not match this quest.')
            return
        # Job flag `job[class + 7 * tier]` (doc E1). The store has `class`; `job2` is the
        # branch the 0x07 record carries as the tier byte (entity+0x111, open question Q7).
        # A short or missing flag string allows everyone, which is how the client reads it.
        job_class, job_tier = int(char.get('class') or 0), int(char.get('job2') or 0)
        if not q.job_allows(job_class, job_tier):
            self._quest_refuse(sock, session, quest_id,
                               f'job flag {q.job!r} refuses class {job_class} tier {job_tier}',
                               'Your class cannot take this quest.')
            return
        if q.prev_q and not state.times(q.prev_q):
            self._quest_refuse(sock, session, quest_id, f'PrevQuest {q.prev_q} not completed',
                               'You must finish the previous quest first.')
            return
        if q.needs_slot and state.first_free_slot() is None:
            # The client drops a 0x26 it has no slot for in silence, so sending it would
            # desync every later 0x59 (Q-B3).
            self._quest_refuse(sock, session, quest_id, 'all 3 active slots are used',
                               f'Quest log full ({MAX_ACTIVE_QUESTS}).')
            return
        # The Send items' space is checked and the grant mirrored under the combat lock, like
        # the turn-in (P7 review): the trade commit relies on bags changing only under it.
        with self._combat_lock(session):
            why = self._quest_bag_space(session, q.send)
            if why is not None:
                # The client's own FUN_00426040 check with the same wording.
                self._quest_refuse(sock, session, quest_id, f'no bag space for the Send items: {why}',
                                   "There isn't empty space in the inventory.")
                return

            slot = state.accept(quest_id, needs_slot=q.needs_slot)
            if slot is None:                       # first_free_slot raced another thread
                self._quest_refuse(sock, session, quest_id, 'the log filled up while validating')
                return
            self._quest_mirror_items(session, q.send, f'quest {quest_id} send')
            self.store.mark_dirty(f'quest {quest_id} accepted')
        self._send_quest_grant(sock, session, quest_id, no_enc)
        if slot >= 0:
            # F1 step 6: 0x26 does NOT reset the slot's progress byte, so a stale value from
            # a previous quest in the same slot would show. 0x59 writes it and runs the
            # client's readiness check (FUN_00426500) in case the Demand items are already
            # in the bag.
            self._send_progress_slot(sock, session, slot, 0)
            log.info(f'[QUEST] {quest_id} accepted into slot {slot + 1} '
                     f'(reqpro={q.reqpro} demand={q.demand})')
        else:
            # F2 talk-only (Money >= 0, no Demand, ReqPro 0): the client files it straight
            # under Completed and uses no slot. Its exp is not applied by 0x26.
            log.info(f'[QUEST] {quest_id} is talk-only: filed as completed '
                     f'({state.times(quest_id)}x), no slot used')
            if q.money > 0 or q.reward:
                log.warning(f'[QUEST] talk-only quest {quest_id} carries Money {q.money} and '
                            f'Reward {q.reward}, which the client does not grant on 0x26; '
                            f'no EN quest does this - content mismatch, nothing granted')
            if q.exp:
                self.award_exp(session, q.exp, 'quest')

    def _handle_turn_in_quest(self, sock, session, payload, no_enc):
        """C2S 0x17 QuestCompleteRequest {u16 quest_id} -> S2C 0x27 + 0x21 [+ 0x3F]
        (quest_cards_misc-turnin-0x17, F5).

        The client sends it from the ENPC talk once its own gates pass (gold for a negative
        Money, progress >= ReqPro, Demand items held, reward space) and opens the Finish
        dialog immediately, before the reply (live quest_cards_misc#03). It used to fall
        through to "Unhandled opcode 0x17", so no quest could ever be turned in (Q-B7)."""
        quest_id = self._quest_id_of(payload, '0x17')
        if quest_id is None:
            return
        q, state, _ = self._quest_request(session, quest_id, '0x17')
        if q is None or state is None:
            return
        # ev-e5 (events.py): an event quest's refusal shows nothing at all (evb A8 "0x27, or
        # nothing on refusal"; P13 exit criterion 4 "without the mushrooms, nothing").
        quiet = self.events.is_event_quest(q)

        def said(text):
            return None if quiet else text

        # Checked AND applied under the combat lock (P7 review): the bag and the wallet change
        # under it everywhere else, and the trade commit - which holds both players' combat
        # locks (trade.py "Lock order") - relies on that. A partner's confirm can then neither
        # commit between these checks and the mirror nor see half of the mirror.
        with self._combat_lock(session):
            slot = state.slot_of(quest_id)
            if slot is None:
                # Not held: ignore in silence. This is what makes a duplicate 0x17 idempotent,
                # and it is the gate the injected-0x27 hazard of #11 (rewards granted again)
                # would otherwise walk straight through.
                self._quest_refuse(sock, session, quest_id, 'not in an active slot (0x17 ignored)')
                return
            wallet = self._wallet_of(session)
            if wallet is None:
                return
            if q.money < 0 and wallet.gold < -q.money:
                self._quest_refuse(sock, session, quest_id,
                                   f'gold {wallet.gold} < fee {-q.money}', said('Not enough gold.'))
                return
            if q.reqpro and state.progress[slot] < q.reqpro:
                self._quest_refuse(sock, session, quest_id,
                                   f'progress {state.progress[slot]}/{q.reqpro}',
                                   said('This quest is not finished yet.'))
                return
            if not self._quest_demand_met(session, q):
                self._quest_refuse(sock, session, quest_id, f'Demand items {q.demand} not in the bag',
                                   said('This quest is not finished yet.'))
                return
            # trade-escrow-guards: Demand items and a fee may not come out of a trade offer.
            escrow = next((e for e in (self._escrow_refusal(session, item, need) for item, need in q.demand)
                           if e is not None), None)
            gold_why = self.trade.gold_refusal(session, -q.money) if escrow is None and q.money < 0 else None
            if gold_why is not None:
                escrow = (gold_why, trademod.GOLD_IN_TRADE_TEXT)
            if escrow is not None:
                self._quest_refuse(sock, session, quest_id, escrow[0], said(escrow[1]))
                return
            # Reward space is checked the way the client's own 0x27 handler runs: the Demand
            # items leave the bag first and the rewards go into the space they free.
            why = self._quest_bag_space(session, q.reward, freeing=q.demand)
            if why is not None:
                self._quest_refuse(sock, session, quest_id, f'no bag space for the rewards: {why}',
                                   said("There isn't empty space in the inventory."))
                return

            # Mirror, in the client's own order (F5 step 4): slot, completed list, gold, Demand
            # removal, Reward grant. Nothing has mutated before this point.
            _, times = state.complete(quest_id)
            if q.money:
                wallet.gold = max(0, wallet.gold + q.money)      # signed; the gate above holds
            for item, need in q.demand:
                self._inv_remove(session, item, need)            # "Gave %u item(%s)." client-side
            self._quest_mirror_items(session, q.reward, f'quest {quest_id} reward')
            self.store.mark_dirty(f'quest {quest_id} completed')
        log.info(f'[QUEST] {quest_id} turned in from slot {slot + 1} (completed {times}x): '
                 f'exp {q.exp:+} money {q.money:+} rewards {q.reward}')
        self._send_quest_complete(sock, session, quest_id, no_enc)
        if q.exp:
            self.award_exp(session, q.exp, 'quest')          # 0x27 applies no exp (#11)
        if q.money:
            # 0x3F is the silent absolute gold write (F5 step 7): the client has just added
            # Money to its own u64, and this makes both wallets agree even if they had
            # drifted (Q-B20). It names no item, so nothing is printed twice.
            self._send_gold(sock, session)
        # ev-e5 (events.py): a quest of a running event's chain pushes its NextQuest now
        # (0x26 + 0x59 after this 0x27 / 0x21 / 0x3F; 158 -> 159 -> 160).
        self.events.after_turn_in(session, q)

    def _handle_abandon_quest(self, sock, session, payload, no_enc):
        """C2S 0x1E QuestAbandonRequest {u16 quest_id} -> S2C 0x38 QuestAbandoned
        (quest_cards_misc-abandon-0x1E, F6).

        Window 0x29 "Would you like to discard this quest?" OK. One click produced exactly
        one packet live (quest_cards_misc#04), but the client checks no event type there, so
        a duplicate is tolerated: the second one finds no slot and is ignored. The Send
        items stay in the bag and no message is printed - that is what S2C 0x38 does
        client-side (#12), and the quest goes back on the NPC's offer list."""
        quest_id = self._quest_id_of(payload, '0x1E')
        if quest_id is None:
            return
        state = self._quest_state(session)
        if state is None:
            log.warning('[QUEST] no character in this session; 0x1E ignored')
            return
        slot = state.abandon(quest_id)
        if slot is None:
            self._quest_refuse(sock, session, quest_id, 'not in an active slot (0x1E ignored)')
            return
        self.store.mark_dirty(f'quest {quest_id} abandoned')
        log.info(f'[QUEST] {quest_id} abandoned from slot {slot + 1}; Send items stay in the bag')
        self._send_quest_abandoned(sock, session, quest_id, no_enc)

    # ---- quest helpers (live log via 0x26 grant / 0x59 progress / 0x27 complete) ----
    @staticmethod
    def _quest_id_of(payload, what):
        """The u16 quest_id of a C2S 0x16 / 0x17 / 0x1E, or None for a short payload."""
        if len(payload) < 2:
            log.warning(f'[QUEST] short {what} payload {len(payload)}B')
            return None
        return struct.unpack_from('<H', payload, 0)[0]

    def _quest_request(self, session, quest_id, what):
        """(QuestDef or None, QuestState or None, character or None) for one quest request.
        Logs the two reasons a request cannot be served at all."""
        q = EC.quests().get(quest_id)
        if q is None:
            log.warning(f'[QUEST] {what}: unknown quest_id={quest_id}')
        char = self._session_char(session)
        if char is None:
            log.warning(f'[QUEST] no character in this session; {what} ignored')
            return q, None, None
        return q, questmod.QuestState(char), char

    def _quest_refuse(self, sock, session, quest_id, why, text=None):
        """Refuse a quest request: no quest packet, one log line, and optionally the S2C
        0x15 system line that says why. The client has no refusal packet for 0x16/0x17/0x1E
        and waits for nothing after them, so silence is always safe; the notice is UX only
        (open question Q8) and replaces the blank "Server :" chat lines (Q-B19)."""
        log.info(f'[QUEST] {quest_id}: refused - {why}')
        if text:
            self._notice(sock, session, text, 'warn')

    def _quest_bag_space(self, session, items, freeing=()):
        """None when the bag model can take EVERY baggable ref in `items` at once, else the
        reason it cannot. `freeing` is removed first (the Demand items a turn-in gives up).

        The check runs on a scratch copy so several refs are counted together: two entries
        of the same id share a stack, and `fits` alone would pass both against the same free
        slot. Refs the client files in no bag tab (type 3 skill book, type 4 class change)
        and ids the EN client does not have are skipped: the client grants what it can and
        silently drops the rest, so they can never be the reason a quest is refused.

        Callers hold the combat lock (the bag changes only under it); the scratch copy is
        store.snapshot's, whose container copies are single C calls, so even a caller that did
        not could never hit "dictionary changed size during iteration" as copy.deepcopy can."""
        bag = self._bag(session)
        if bag is None:
            return 'no character'
        # the copy holds `inventory` alone: the bag's pet count rides along (a 2009 bagged pet
        # takes an equipment-tab slot, inventory.Inventory `pets`)
        scratch = invmod.Inventory({'inventory': storemod.snapshot(bag.data)}, pets=bag.pet_slots())
        for item, count in freeing:
            scratch.remove(item, count)
        for item, count in items:
            if scratch.tab_of(item) is None:
                continue
            if scratch.add(item, count) is None:
                return scratch.fits(item, count) or f'item {item} x{count}'
        return None

    def _quest_mirror_items(self, session, items, what):
        """Mirror a client-side quest item grant (0x26 Send, 0x27 Reward) into the model,
        with NO 0x18: the client grants them itself from its own hqi record, so a packet
        would put a second copy in the bag (Q-B2/Q-B11, item_inventory-quest-grant-mirror).
        Type 3 and type 4 are not bag items - the client learns / applies them."""
        for item, count in items:
            kind = EC.items().type_of(item)
            if kind == 3:
                self._quest_reward_skill(session, item, what)
            elif kind == 4:
                self._quest_reward_class(session, item, what)
            elif self._inv_add(session, item, count, what) is not None:
                log.info(f'[QUEST] mirrored client-granted item {item} x{count} ({what})')

    def _quest_reward_skill(self, session, item_id, what):
        """Type-3 Send/Reward hook (8 EN quest rewards: Double Jump 94 from quest 26, Dash 80
        from 28, Strong Attack, Guard, Open Stall, Herb Gathering, Mining, Reinforce).

        The client learns it on 0x26/0x27 itself and prints "You've learned skill(...)" (live
        quest_cards_misc#11), so nothing is sent: the server only MIRRORS the learn into
        char['skills'] (cs-skill-learn), with the client's own family rule and none of the
        level/class gates the client does not apply either. The item id IS the skill id - a
        skill is an item-table record (combat_skill.md finding 1; FUN_00440920 case 3 passes
        the item id straight to FUN_00426b80). Without this the 0x07 of the next portal or
        relog rebuilt the client's list without it: the live "Double Jump vanishes on
        relog" bug. An already-learned reward changes nothing on either side (C21). The
        class-change half of quest_cards_misc-quest-rewards-skill-class is lc-class-change."""
        self._learn_skill(session, item_id, check=False, notify=False, what=what)

    def _quest_reward_class(self, session, item_id, what):
        """Type-4 Send/Reward hook (quest_cards_misc-quest-rewards-skill-class): the 18 EN job
        rewards - 180 / 203..207 from quests 8 and 11..15 (Novice only), 3076..3088 from
        quests 166..221 (a base class at tier 0).

        The client applies it on 0x26/0x27 itself through FUN_00440920 case 4 ->
        FUN_004249E0, the popup included, so nothing is sent to the owner: the server only
        MIRRORS the change into char['class']/['job2'] with the same table (lc-class-change),
        which is what the next 0x07 and the select screen's 0x02 read. Observers get 0x58.
        An item the table refuses (wrong class, tier already taken) was refused by the client
        too, silently: nothing changes on either side. Before P3 stage 5 this only logged
        "NOT persisted yet", and a relog turned the new Warrior back into a Novice."""
        res = self._apply_job_item(session, item_id, notify=False, what=what)
        if not res.ok:
            log.info(f'[QUEST] {what}: job reward {item_id} refused by FUN_004249E0 on the client '
                     f'as well ({res.why}) - class unchanged')

    def _quest_slot(self, session, quest_id):
        """The client slot number (1..3) of a held quest, or None. The slot is the index in
        the persisted active array, which is exactly the "first empty slot" the client
        itself writes on S2C 0x26 (Q-B3: the old per-session slot map could drift from it,
        and gave the 9 talk-only quests a slot the client never used)."""
        state = self._quest_state(session)
        slot = None if state is None else state.slot_of(quest_id)
        return None if slot is None else slot + 1

    def _send_quest_grant(self, sock, session, quest_id, no_enc=False):
        """0x26 GRANT - add quest to the client's active log live (no 0x03 reset)."""
        log.info(f'[QUEST] -> 0x26 GRANT quest_id={quest_id}')
        P.send(self, sock, session, '0x26', {'quest_id': quest_id})

    def _send_progress_slot(self, sock, session, slot, progress):
        """0x59 PROGRESS {u8 slot 1..3, u8 progress} by slot INDEX (0..2). Slot 0 on the
        wire is never valid: the client writes it one byte short of the array and corrupts
        slot 3's quest id (live test T-S59), so it is refused here."""
        if not 0 <= slot < MAX_ACTIVE_QUESTS:
            log.error(f'[QUEST] refusing 0x59 for slot index {slot}')
            return
        log.info(f'[QUEST] -> 0x59 PROGRESS slot={slot + 1} progress={progress}')
        P.send(self, sock, session, '0x59', {'slot': slot + 1, 'progress': progress & 0xFF})

    def _send_quest_progress(self, sock, session, quest_id, current, no_enc=False):
        """0x59 for a held quest id (the slot comes from the model)."""
        slot = self._quest_slot(session, quest_id)
        if slot is None:
            return
        self._send_progress_slot(sock, session, slot - 1, current)

    def _send_quest_complete(self, sock, session, quest_id, no_enc=False):
        """0x27 COMPLETE - clears the slot, files it under Completed and credits Money and
        the Reward items client-side."""
        log.info(f'[QUEST] -> 0x27 COMPLETE quest_id={quest_id}')
        P.send(self, sock, session, '0x27', {'quest_id': quest_id})

    def _send_quest_abandoned(self, sock, session, quest_id, no_enc=False):
        """0x38 ABANDONED - zeroes the matching slot and its progress. No message, and the
        Send items stay in the bag (live quest_cards_misc#12)."""
        log.info(f'[QUEST] -> 0x38 ABANDONED quest_id={quest_id}')
        P.send(self, sock, session, '0x38', {'quest_id': quest_id})

    def _quest_demand_met(self, session, q):
        """The client's own readiness rule for the Demand items (FUN_00426500): they must be
        IN THE BAG at turn-in. The persistent bag is the truth for them, so nothing tracks a
        separate collected count any more - the progress byte is only the ReqPro kill
        counter (Q-B10). An EMPTY demand list is not "met" by itself: readiness also needs
        ReqPro and gold, which is why only the turn-in handler ever asks (Q-B5)."""
        bag = self._inventory(session)
        return all(bag.get(item, 0) >= need for item, need in q.demand)

    def _quest_credit_kill(self, sock, session, npccode, no_enc=False):
        """ReqPro credit for one kill (quest_cards_misc-kill-progress, F3). Exactly one slot
        is credited per kill, the first whose quest wants this monster and is not yet full -
        the same rule as the client's own P2P path FUN_0041A230 (doc E5), which no packet
        feeds under server-driven combat, so 88 EN quests (quest 26 among them) could never
        become ready (Q-B9)."""
        state = self._quest_state(session)
        if state is None:
            return
        credited = state.credit_kill(npccode, EC.quests())
        if credited is None:
            return
        slot, progress = credited
        self.store.mark_dirty(f'quest {state.active[slot]} progress')
        log.info(f'[QUEST] kill of npccode {npccode} credits quest {state.active[slot]} '
                 f'slot {slot + 1}: {progress}/{EC.quests().get(state.active[slot]).reqpro}')
        self._send_progress_slot(sock, session, slot, progress)

    def _quest_credit_item(self, sock, session, item_id, qty, no_enc):
        """A collected item can satisfy a Demand, and the client re-evaluates its readiness
        flag whenever it receives a 0x59 for that slot. The packet carries the slot's
        UNCHANGED ReqPro progress: writing the collected count into the progress byte showed
        a wrong counter and corrupted the 5 quests that have both a ReqPro and a Demand
        (Q-B10). The kill counter itself is _quest_credit_kill above."""
        if qty <= 0:
            return
        state = self._quest_state(session)
        if state is None:
            return
        catalog = EC.quests()
        for slot, qid in enumerate(state.active):
            if not qid:
                continue
            q = catalog.get(qid)
            if q is None or not any(d_item == item_id for d_item, _ in q.demand):
                continue
            self._send_progress_slot(sock, session, slot, state.progress[slot])

    # ------------------------------------------------------------------------
    # Items: equip, unequip, use (item_inventory-equip / -unequip / -use-consumable;
    # item_inventory.md F3, F4, F5).
    #
    # All three answer a request whose effect the CLIENT applies itself, from its own hii
    # record, the moment the reply lands: 0x1D moves the bag entry into the grid, 0x1E moves
    # it back, 0x25 removes one unit and adds the HP/MP. So all three follow one rule:
    # validate everything first, mutate the model exactly the way the client will, then send
    # exactly ONE packet. A second packet duplicates or deletes an item (the 0x1D + 0x23
    # double removal, live bug 5), and a model mutation the client refuses (unknown id, full
    # tab, running cooldown) survives until the next portal rebuilds the bag from the model.
    #
    # Ownership is the one check the client cannot make for us, and the only one whose
    # refusal needs a packet: 0x0F and 0x15 are sent from a bag double-click, so when the
    # server has no such item the client is plainly showing one (a GM-injected item, or a
    # stale bag). S2C 0x23 removes that phantom and the two sides agree again (F3 step 3,
    # F5 step 2). The wearer gates below are refused with nothing at all: the client checks
    # all of them before it sends and none of them leave pending client state.
    # ------------------------------------------------------------------------
    def _wearer_gates(self, session, char, info):
        """None when this character may wear `info`, else why not (inventory.equip_refusal
        mirrors the client's gender / level / job checks). The gender flag is the ACCOUNT's
        (scene+0x130 = the S2C 0x02 account_gender_flag), the level is exp-derived like
        every other level, and class 0 (Novice) reads the Job[0] any-class flag."""
        # 2009: the equip check FUN_0044FE20 tests the local entity's own gender (+0x11B,
        # the S2C 0x02 record bool), not an account flag - R.record_gender either way.
        return invmod.equip_refusal(info, level=R.level_of(char), class_id=char.get('class', 0),
                                    gender_flag=self._record_gender(session, char))

    def _record_gender(self, session, char):
        """The gender bool this character's records carry (R.record_gender: the account flag
        in 2008, the character's own in 2009). The client composes the look with the entity's
        copy of it, so the server composes the stored look (inventory.compose) with it too."""
        return R.record_gender(char, self._session_account(session), self.client_build)

    @staticmethod
    def _look_note(char, info):
        """'look <layer>=<word>' for a log line: the appearance word the item's Kind composes
        (inventory.LAYER_NAMES), as the next 0x07 / 0x02 will carry it."""
        _words, limit = invmod.compose_limits(EC.items())
        kind = getattr(info, 'kind', -1)
        if not 0 <= kind < limit:
            return f'no look layer (Kind {kind})'
        words = R.appearance(char) + R.look_ext(char)
        return f'look {invmod.LAYER_NAMES[kind]}={words[kind]}'

    @staticmethod
    def _observer_item_fields(key, session, item, words):
        """S2C 0x1D / 0x1E / 0x24 for the OTHER clients (item_inventory-observer-broadcast):
        the uid of `session`'s player and the MODEL's block - the 12 bytes their copy's grid
        slot holds, because their records (records.equip_grid) and every earlier observer
        0x1D carry the stored words. The owner gets the echo of his request instead (his
        bag holds the bytes he sent); the two differ only for a block with a zero gap,
        which the model stores packed (inventory.pack_words)."""
        return invmod.item_fields(key, item, words=words, uid=P.session_uid(session) or 0)

    def _send_phantom_remove(self, sock, session, item, stones=(), extra=0, why=''):
        """S2C 0x23 InventoryItemRemove for an item the client shows and the model does not
        have. The block is echoed exactly as it arrived, because the client finds the bag
        entry by a memcmp of those 12 bytes (a packed rebuild matches nothing)."""
        P.send(self, sock, session, '0x23',
               invmod.item_fields('0x23', item, count=1,
                                  block=invmod.echo_block_fields('0x23', stones, extra)))
        log.warning(f'[ITEM] item={item} ("{EC.item_name(item)}") {why}: S2C 0x23 removes the '
                    f'client copy (the server bag has no matching instance)')

    def _handle_equip_item(self, sock, session, rec, no_enc):
        """C2S 0x0F EquipItem {u16 item_id, u8 stone_count, stone_id[], u16 extra_option}
        -> S2C 0x1D {uid, item_id, stone_count, stone_id[], block_tail} (item_inventory F3).

        0x1D alone moves the item: for the local player (uid == scene+0x220) the client
        writes the equip grid, recomposes the sprite, removes the bag entry whose id AND
        12-byte option block match, and puts the previously equipped item back in the bag.
        The old follow-up S2C 0x23 silent-remove then deleted a second identical copy:
        "2 sticks in the bag, 0 after equipping one" (item_inventory-no-double-remove,
        S2-07, LIVE_TEST_LOG bug 5, P0 exit criterion 7).

        The model mirrors that in the client's own order (Inventory.wear, live-proven by
        item_inventory#05): the requested instance leaves the bag and the tab compacts, then
        the displaced one is appended - so the freed slot is always there for it, and the
        0x03 list order after a portal is the order the client is showing.

        The block is echoed VERBATIM (spec 0x1D trigger: the live capture B3 00 00 00 00 is
        item 179, 0 stones, tail 0). A rebuilt or packed block is a different 12 bytes, the
        client's bag lookup fails, and the item ends up both worn and in the bag (S1-16).
        stone_count > 5 is refused: 0x1D would write past the 12-byte block.

        The client recomposes the look itself on this 0x1D (FUN_004282c0 at 0x458215, before
        the local-player check, so for its own entity and for an observer's copy alike), but
        only S2C 0x02 / 0x07 / 0x04 / 0x05 set the whole array, verbatim. So `Inventory.wear`
        applies the same composition to the stored look: the next portal, relog, select
        screen or late joiner shows the worn gear (P2 exit criteria 1 and 6; the old record
        sent the creation outfit with only the weapon merged into slot 11). No follow-up
        packet: a refresh 0x07 would re-create the entity."""
        item = int(rec.get('item_id', 0))
        stones = [int(e.get('stone_id', 0)) for e in rec.get('repeat[stone_count]', [])]
        extra = int(rec.get('extra_option', 0))
        if len(stones) > P.OPTION_LIST_MAX:
            log.warning(f'[EQUIP] item={item} with {len(stones)} stones refused '
                        f'(max {P.OPTION_LIST_MAX}; 0x1D would overflow the option block)')
            return
        if not en_item_exists(item):
            log.warning(f'[EQUIP] item={item} is not an EN client item - ignoring')
            return
        info = en_item(item)
        if info.type != EC.TYPE_EQUIPMENT:
            log.info(f'[EQUIP] item={item} not equippable (EN Type {info.type} != 1) - ignoring')
            return
        char = self._session_char(session)
        if char is None:
            return
        bag = invmod.Inventory(char)
        words = invmod.block_from_wire(stones, extra)
        with self._combat_lock(session):
            if bag.instance(item, words) is None:
                self._send_phantom_remove(sock, session, item, stones, extra, 'equip of an item the bag lacks')
                return
            escrow = self._escrow_refusal(session, item, 1, words)
            if escrow is not None:
                # trade-escrow-guards: an offered instance stays in the bag until the commit;
                # nothing is pending client-side, so the request is dropped.
                log.info(f'[EQUIP] item={item} refused: {escrow[0]}')
                self._notice(sock, session, escrow[1], 'warn')
                return
            why = self._wearer_gates(session, char, info)
            if why is not None:
                # The client never sends one of these by hand: a forged packet, or a client
                # whose hii is not ours. Nothing is pending client-side, so drop it.
                log.warning(f'[EQUIP] item={item} ("{EC.item_name(item)}") refused: {why}')
                return
            # The grid slot comes from the EN Kind through the FUN_00426680 table
            # (item_inventory-kind-slot-table): the old _KIND_TO_GRID was PySlayer's KR slot
            # order and put Kinds 15/16 in slot 0, and it read the Kind from the KR gamedef,
            # which differs from EN for 289 items.
            slot, displaced = bag.wear(item, words, gender=self._record_gender(session, char))
            if slot is None:
                log.info(f'[EQUIP] item={item} Kind={info.kind} has no equip slot in '
                         f'FUN_00426680 - ignoring')
                return
            self.store.mark_dirty(f'equip {item}')
            fields = invmod.item_fields('0x1D', item, uid=P.session_uid(session) or 0,
                                        block=invmod.echo_block_fields('0x1D', stones, extra))
            P.send(self, sock, session, '0x1D', fields)
            # The 0x1D handler reruns FUN_0041ace0 + FUN_00427d40/f40(0): equipment Int/Tol and
            # option stones change the maxima; the current values are only clamped.
            self._refresh_vitals(session, char, reason=f'equip {item}')
            # item_inventory-observer-broadcast (F3 step 7, P5 stage 4): 0x1D to the clients
            # that hold this player. For a uid that is not their scene+0x220 (2009 +0x224)
            # the handler only writes the grid slot with the block, recomposes the sprite -
            # the weapon in the hand - and recalculates stats: no bag, no window, no
            # quickslot (spec 0x1D, both builds).
            seen = presence.to_holders(self, session, '0x1D', self._observer_item_fields(
                '0x1D', session, item, words), 'EQUIP')
        returned = f", item {displaced['id']} returned to the bag" if displaced else ''
        log.info(f"[EQUIP] {EC.item_name(item)} item={item} Kind={info.kind} slot={slot} "
                 f"stones={stones} tail={extra} -> 0x1D (+{seen} observer(s)), "
                 f"{self._look_note(char, info)}{returned}")

    def _handle_unequip_item(self, sock, session, rec, no_enc):
        """C2S 0x11 UnequipItem {u16 item_id, u8 enchant_count, enchant[], u16 enchant_last}
        -> S2C 0x1E {uid, item_id, option_count, option_value[], option_extra}
        (item_inventory-unequip, F4; B9: 0x11 had no route at all, so a double-click in the
        Equipment window logged "Unhandled opcode 0x11" and the item stayed worn forever -
        live-confirmed as item_inventory#17, with the identical 5 B body 'B3 00 00 00 00').

        0x1E does the whole move client-side: it clears the grid slot whose id AND 12 bytes
        match, recomposes the sprite with Spr 0 (the empty hand of P2 exit criterion 6; an
        emptied shirt / pant layer shows the underwear, a bare head the hair), recalculates
        max HP/MP, and - only for uid == scene+0x220 - adds the item to the first empty bag
        slot with its block, which is what makes dragging it back work. `Inventory.take_off`
        recomposes the stored look the same way.

        DUPLICATION HAZARD (spec 0x1E, live-proven as item_inventory#04b): the grid removal's
        result is ignored, so a block that differs from the stored one leaves the item worn
        AND puts a copy in the bag. The request is therefore matched against the model by id
        plus the exact block, and only then echoed back byte for byte.

        The request carries no pending client state and the client re-sends it on every
        double-click, so a request for something that is no longer worn is dropped in
        silence (idempotent). So is one the bag has no room for: the client refuses to send
        in that case ("You have no more slots for the item."), and 0x1E into a full bag
        silently drops the item."""
        item = int(rec.get('item_id', 0))
        stones = [int(e.get('enchant', 0)) for e in rec.get('repeat[enchant_count]', [])]
        extra = int(rec.get('enchant_last', 0))
        if len(stones) > P.OPTION_LIST_MAX:
            log.warning(f'[UNEQUIP] item={item} with {len(stones)} enchant words refused '
                        f'(max {P.OPTION_LIST_MAX}; 0x1E would overflow the 12-byte record)')
            return
        if not en_item_exists(item):
            log.warning(f'[UNEQUIP] item={item} is not an EN client item - ignoring')
            return
        info = en_item(item)
        if info.type != EC.TYPE_EQUIPMENT:
            # The client's own gate 6 and the 0x1E handler's first check (def+0x154 == 1).
            log.info(f'[UNEQUIP] item={item} is EN Type {info.type} != 1 - ignoring')
            return
        char = self._session_char(session)
        if char is None:
            return
        bag = invmod.Inventory(char)
        words = invmod.block_from_wire(stones, extra)
        with self._combat_lock(session):
            slot, entry = bag.worn(item, words)
            if entry is None:
                log.info(f'[UNEQUIP] item={item} block={words} is not equipped - ignoring '
                         f'(a repeated double-click, or a block the grid never held)')
                return
            full = bag.fits(item, 1, words)
            if full is not None:
                log.info(f'[UNEQUIP] item={item} refused: {full}')
                return
            bag.take_off(item, words, gender=self._record_gender(session, char))
            self.store.mark_dirty(f'unequip {item}')
            fields = invmod.item_fields('0x1E', item, uid=P.session_uid(session) or 0,
                                        block=invmod.echo_block_fields('0x1E', stones, extra))
            P.send(self, sock, session, '0x1E', fields)
            # 0x1E "recalculates max HP/MP" client-side (FUN_00427d40/f40 with 0): a lower
            # maximum cuts the current value, a higher one never heals.
            self._refresh_vitals(session, char, reason=f'unequip {item}')
            # F4 step 5 (P5 stage 4): 0x1E to the clients that hold this player - the grid
            # clear and the empty hand. Their bag add is gated on uid == their own
            # scene+0x220 (spec 0x1E step 3), so the duplication hazard cannot reach them.
            seen = presence.to_holders(self, session, '0x1E', self._observer_item_fields(
                '0x1E', session, item, entry['w']), 'EQUIP')
        log.info(f'[UNEQUIP] {EC.item_name(item)} item={item} slot={slot} enchant={stones} '
                 f'tail={extra} -> 0x1E (+{seen} observer(s)), back in the bag, '
                 f'{self._look_note(char, info)}')

    # ------------------------------------------------------------------------
    # Ground items (item_inventory-ground-loot-pickup / -drop-bag-item / -drop-equipped;
    # item_inventory.md 1.5, F6-F9; the wire layout and its derivation from both clients'
    # disassembly are in ground.py). P4 stage 2.
    #
    # One registry per server (self.ground), keyed by map. Who gets what:
    #   - the actor (killer / dropper): S2C 0x12 with source_uid = the corpse / himself, so his
    #     client puts the item on that entity's OWN position (roaming mobs are wherever the
    #     client walked them; the server's x/y is only the wire fallback);
    #   - monster loot, the other sessions on the map whose client holds the corpse: the same
    #     0x12 - since world-shared-monsters (P5 stage 3) the corpse's uid is the same monster
    #     on every client, so the item falls at the corpse on each screen, wherever that
    #     client's copy of the mob died;
    #   - a dropped bag/worn item, the other sessions whose client holds the dropper: the same
    #     0x12 with source = the dropper, so it falls at their copy of him (item_inventory-
    #     observer-broadcast, P5 stage 4; presence.holders);
    #   - everyone else on the map: S2C 0x11 state 1 at the wire x/y (0x11 needs no entity at
    #     all);
    #   - map entry: S2C 0x11 state 0 for everything lying there (spec correction C19: the
    #     0x03/0x08 map load frees the client's ground list);
    #   - pickup and despawn: S2C 0x13 to every session on the map (an unknown id is a no-op).
    # drop_time is per receiver: its own clock minus the item's age (ground.client_drop_time).
    # Nothing here opens a client waiting box, so a refused request needs no reply packet.
    # ------------------------------------------------------------------------
    DROP_REFUSED_TEXT = "That item can't be dropped."

    def _ground_sessions(self, map_code, exclude=None):
        """In-world sessions on `map_code`: the receivers of a ground packet (world
        registry map audience)."""
        return self.world.map_sessions(map_code, exclude=exclude)

    def _ground_send(self, session, key, fields):
        """One ground packet to one session. A dead socket is logged, never raised: the
        sender is often another player's handler or the tick thread."""
        return self._push(session, key, fields, 'GROUND')

    @staticmethod
    def _groundable(item_id):
        """None when `item_id` may lie on the ground and be picked up again, else why not:
        an EN id whose Type files it in a bag tab - 0 / 1 / 2 are the only ground categories
        the pickup key ever requests (FUN_0043d5c0 / 2009 FUN_0043d870), so a Type 3 skill
        book on the ground could never be taken - and not a Cash item (def+0x1F0: the client
        never drops or trades one)."""
        if not en_item_exists(item_id):
            return f'item {item_id} is not in the EN client catalog'
        info = en_item(item_id)
        if info.type not in (EC.TYPE_CONSUMABLE, EC.TYPE_EQUIPMENT, EC.TYPE_ETC):
            return f'EN Type {info.type}: the pickup key never requests that category'
        if info.is_cash:
            return 'a Cash item (def+0x1F0: never dropped or traded)'
        return None

    def _place_ground_item(self, session, item_id, qty, words, *, x, y, owner_uid, source_uid, what,
                           source_holders=()):
        """Register one ground item on the session's map, arm its despawn timer (tick
        scheduler, GROUND_ITEM_SECS) and show it: S2C 0x12 to `session` and to the other
        sessions in `source_holders` (their client holds the source entity: a shared
        monster's corpse, or the player who dropped it), 0x11 state 1 to the rest of the
        map. The caller has checked _groundable. Returns the item."""
        map_code = session.get('current_map')
        tab = EC.items().tab_of(item_id)
        limit = invmod.STACK_MAX.get(tab, 1)
        if qty > limit:
            # The pickup gate refuses a consume record over 999 / an etc record over 99 for
            # good (FUN_00424c80 / FUN_00424d00), and equipment is always one instance.
            log.warning(f'[GROUND] {what}: {qty} x {item_id} clamped to {limit} ({tab} tab)')
            qty = limit
        item, evicted = self.ground.add(map_code, item_id, qty, words, x, y, owner_uid=owner_uid,
                                        source_uid=source_uid, what=what)
        for old in evicted:
            self._despawn_ground_item(old, f'map {map_code} holds {self.ground.max_per_map} items',
                                      registered=False)
        item.timer = self.ticks.call_later(self.config.get('GROUND_ITEM_SECS', 60.0),
                                           self._despawn_ground_item, item, 'timeout',
                                           name=f'ground-despawn-{map_code}-{item.ground_id}')
        now = time.monotonic()
        if session.get('in_world') and session.get('sock') is not None:
            drop_time = groundmod.client_drop_time(self._client_clock(session, now))
            for body in groundmod.drop_fields([item], [drop_time]):
                self._ground_send(session, '0x12', body)
        holders = {id(s) for s in source_holders}
        for peer in self._ground_sessions(map_code, exclude=session):
            drop_time = groundmod.client_drop_time(self._client_clock(peer, now), item.age_ms(now))
            if id(peer) in holders:
                for body in groundmod.drop_fields([item], [drop_time]):
                    self._ground_send(peer, '0x12', body)
                continue
            for body in groundmod.list_fields([item], [drop_time], state=groundmod.STATE_POP_IN):
                self._ground_send(peer, '0x11', body)
        log.info(f'[GROUND] map {map_code} id {item.ground_id}: {item.qty} x {EC.item_name(item.item_id)} '
                 f'({item.item_id}) at ({item.x:g},{item.y:g}) owner={item.owner_uid} '
                 f'source={item.source_uid:#x} ({what}); {self.ground.count(map_code)} on the map')
        return item

    def _loot_to_ground(self, session, mob, item_id, count):
        """A kill's loot on the ground at the corpse (item_inventory F6 steps 3-4): owner =
        the killer (15 s of loot rights), source = the monster; every other client that holds
        the corpse sees it fall there too (0x12). Called by _kill_monster under the map's
        monster lock. Returns the item or None."""
        why = self._groundable(item_id)
        if why is not None:
            log.info(f'[LOOT] {mob.name} drop {item_id} not placed: {why}')
            return None
        mons = self._mob_home(session)
        holders = self._mob_viewers(mons, mob, exclude=session) if mons is not None else ()
        return self._place_ground_item(session, item_id, count, None, x=mob.x, y=mob.y,
                                       owner_uid=P.session_uid(session) or 0, source_uid=mob.uid,
                                       what=f'{mob.name} drop', source_holders=holders)

    def _despawn_ground_item(self, item, why='timeout', registered=True):
        """Take an unpicked item off its map: S2C 0x13 {ground_id, 0, picker 0} to every
        session there - the client marks the record picked and frees it 1 s later with no bag
        change (T-S13b: live-safe on 2008, results_destructive item_inventory#08; the 2009
        draw FUN_0043d0f0 and tick FUN_0042e790 do the same). The despawn timer's callback
        (registered: still in the registry unless it was picked meanwhile) and the per-map
        cap's eviction (already out of the registry)."""
        if item.timer is not None:
            item.timer.cancel()
        if registered and not self.ground.remove(item):
            return False
        fields = groundmod.despawn_fields(item)
        receivers = self._ground_sessions(item.map_code)
        for peer in receivers:
            self._ground_send(peer, '0x13', fields)
        log.info(f'[GROUND] map {item.map_code} id {item.ground_id}: {item.qty} x '
                 f'{EC.item_name(item.item_id)} despawned ({why}) -> 0x13 to {len(receivers)} session(s)')
        return True

    def _send_ground_list(self, sock, session, map_code):
        """S2C 0x11 state 0 with every item lying on `map_code`, at the wire x/y, after a map
        load (item_inventory F6 step 5). Returns the number of items sent."""
        items = self.ground.items(map_code)
        if not items:
            return 0
        now = time.monotonic()
        clock = self._client_clock(session, now)
        times = [groundmod.client_drop_time(clock, i.age_ms(now)) for i in items]
        for body in groundmod.list_fields(items, times, state=groundmod.STATE_RESTING):
            P.send(self, sock, session, '0x11', body)
        log.info(f'[GROUND] map {map_code}: {len(items)} ground item(s) re-sent (0x11 state 0)')
        return len(items)

    def _handle_ground_pickup(self, sock, session, rec, no_enc=False):
        """C2S 0x1F GroundItemPickupRequest {u16 ground_item_uid} -> S2C 0x13 {ground_id,
        quantity, picker_uid} to every session on the map (item_inventory F7).

        The client checked range, ownership and bag space before sending, and waits for
        nothing. It re-sends the request while the key is held (and a 2009 pet every 30 ms),
        so the first valid request takes the item out of the registry and every repeat finds
        nothing: exactly one grant. The server repeats the two checks it can make - loot
        protection (owner, else 15 s) and room in the bag, by the SAME capacities it sent in
        0x03, because the client marks the item picked even when its own bag add fails and
        the item would be lost - and refuses in silence: the client keeps showing the item.
        The bag gets exactly what the client adds from its ground record: `quantity` of a
        stack, or one equipment instance with the record's 6 option words."""
        gid = int(rec.get('ground_item_uid', 0))
        if not session.get('in_world'):
            log.info(f'[GROUND] pickup {gid} outside the world - ignored')
            return
        char = self._session_char(session)
        if char is None:
            return
        map_code = session.get('current_map')
        uid = P.session_uid(session) or 0
        with self._combat_lock(session):
            item = self.ground.get(map_code, gid)
            if item is None:
                log.debug(f'[GROUND] pickup {gid} on map {map_code}: nothing there (a repeat of a '
                          f'granted request, or it despawned)')
                return
            bag = invmod.Inventory(char)
            full = bag.fits(item.item_id, item.qty, item.words)
            if full is not None:
                log.info(f'[GROUND] pickup {gid} ({item.item_id}) refused: {full}')
                return
            taken, why = self.ground.take(map_code, gid, uid)
            if taken is None:
                log.info(f'[GROUND] pickup {gid} refused: {why}')
                return
            if bag.add(taken.item_id, taken.qty, taken.words) is None:
                self.ground.restore(taken)       # unreachable after fits(); never lose it
                log.warning(f'[GROUND] pickup {gid}: the bag refused {taken.item_id} x{taken.qty}')
                return
            if taken.timer is not None:
                taken.timer.cancel()
            self.store.mark_dirty(f'pickup {taken.item_id}')
            fields = groundmod.picked_fields(taken, uid)
            P.send(self, sock, session, '0x13', fields)
        for peer in self._ground_sessions(map_code, exclude=session):
            self._ground_send(peer, '0x13', fields)
        log.info(f'[GROUND] {session.get("char_name")} picked id {gid}: {fields["quantity"]} x '
                 f'{EC.item_name(taken.item_id)} ({taken.item_id}) -> 0x13; bag now '
                 f'{bag.count(taken.item_id)}')
        # A collected item can satisfy a quest Demand: the 0x59 re-check, after the 0x13 put
        # it in the client's bag (quest doc F3 step 2; the P0 loot path did the same).
        self._quest_credit_item(sock, session, taken.item_id, taken.qty, no_enc)

    def _refuse_drop(self, sock, session, what, why, text=None):
        """A drop the server will not do: the bag stays as it is (the client removed nothing
        - it waits for the server's 0x23 / 0x24) and a warning line says so."""
        log.info(f'[DROP] {what} refused: {why}')
        if session.get('in_world') and sock is not None:
            self._notice(sock, session, text or self.DROP_REFUSED_TEXT, 'warn')

    def _handle_drop_bag_item(self, sock, session, rec, no_enc=False):
        """C2S 0x13 DropInventoryItem {u16 item_id, u16 amount, u8 opt_count, opt[], u16 opt6}
        -> S2C 0x23 {item_id, count, block} to self, then S2C 0x12 with source = the dropper
        (item_inventory F8; live item_inventory#18: '05 00 01 00 00 00 00' = Herb x1).

        The client gates the send (not cash, amount 1..999 / 1..99, owned) and changes
        nothing itself: 0x23 is what takes the amount out of its bag - by id and, for
        equipment, by a memcmp of the 12 bytes it sent, so the block is ECHOED. The server
        re-checks ownership against the model (equipment: id AND exact block), removes, and
        puts the same amount / instance on the ground: owner 0 (anyone may take it) and
        source = the dropper, whose own entity positions the 0x12 on his client."""
        item = int(rec.get('item_id', 0))
        amount = int(rec.get('amount', 0))
        opts = [int(e.get('opt', 0)) for e in rec.get('repeat[opt_count]', [])]
        extra = int(rec.get('opt6', 0))
        what = f'drop of {amount} x {item}'
        if not session.get('in_world'):
            log.info(f'[DROP] {what} outside the world - ignored')
            return
        char = self._session_char(session)
        if char is None:
            return
        if len(opts) > P.OPTION_LIST_MAX:
            return self._refuse_drop(sock, session, what, f'{len(opts)} option words (max {P.OPTION_LIST_MAX})')
        why = self._groundable(item)
        if why is not None:
            return self._refuse_drop(sock, session, what, why)
        bag = invmod.Inventory(char)
        tab = bag.tab_of(item)
        # Equipment is one instance per slot: the popup shows 1 and the amount is ignored.
        qty = 1 if tab == 'equip' else amount
        if amount < 1 or qty > invmod.STACK_MAX[tab]:
            # The client's own "999 is maximum number." / "99 ..." gates (the amount is cut
            # to u16 before them): a request past them is not from the drop popup.
            return self._refuse_drop(sock, session, what, f'amount {amount} outside 1..{invmod.STACK_MAX[tab]}')
        words = invmod.block_from_wire(opts, extra) if tab == 'equip' else None
        uid = P.session_uid(session) or 0
        with self._combat_lock(session):
            if not bag.has(item, qty, words):
                return self._refuse_drop(sock, session, what,
                                         f'the bag holds {bag.count(item, words)} (block {words})',
                                         "You've exceeded the amount you have.")
            escrow = self._escrow_refusal(session, item, qty, words)
            if escrow is not None:
                return self._refuse_drop(sock, session, what, *escrow)     # trade-escrow-guards
            ground_words = list(bag.instance(item, words)['w']) if tab == 'equip' else None
            bag.remove(item, qty, words)
            self.store.mark_dirty(f'drop {item}')
            P.send(self, sock, session, '0x23',
                   invmod.item_fields('0x23', item, count=qty,
                                      block=invmod.echo_block_fields('0x23', opts, extra)))
            x, y = session.get('pos') or (0.0, 0.0)
            # The clients that hold the dropper get the same 0x12 (source = the dropper): it
            # falls at THEIR copy of him (spec 0x12 position rule), which follows his own
            # input stream, where the wire x/y is only the server's estimate (P5 stage 4).
            placed = self._place_ground_item(session, item, qty, ground_words, x=x, y=y, owner_uid=0,
                                             source_uid=uid, what=f'{session.get("char_name")} dropped it',
                                             source_holders=presence.holders(self, session))
        log.info(f'[DROP] {session.get("char_name")} dropped {qty} x {EC.item_name(item)} ({item}) '
                 f'-> 0x23 + 0x12 (ground id {placed.ground_id}); bag now {bag.count(item)}')
        # The bag lost what may have been a quest Demand item: the 0x59 re-check lets the
        # client clear a ready flag it no longer earns.
        self._quest_credit_item(sock, session, item, qty, no_enc)

    def _handle_drop_worn_item(self, sock, session, rec, no_enc=False):
        """C2S 0x14 DropEquippedItem {u16 item_id, u8 opt_count, opt[], u16 opt6} -> S2C 0x24
        {uid, item_id, block} to self and the observers, then S2C 0x12 (item_inventory F9;
        live item_inventory#19: 'B3 00 00 00 00' = the worn Wooden Stick).

        0x24 is 0x1E without the bag add (the opcode check at 0x45424F skips it): the grid
        slot whose id AND 12 bytes match is cleared, the sprite is recomposed - the empty
        hand of P4 exit criterion 4 - and max HP/MP are recalculated; live item_inventory#05
        showed exactly that with no bag change. The block is echoed as the client sent it
        (its own grid record), the model's grid slot is cleared and the stored look
        recomposed with Spr 0 exactly as the 0x24 handler recomposes the client's (the
        FUN_004282c0 call it shares with 0x1E), so the next 0x07 (portal, relog) shows the
        same empty hand, and the instance lands on the ground with its words, so picking it
        back up restores the same item."""
        item = int(rec.get('item_id', 0))
        opts = [int(e.get('opt', 0)) for e in rec.get('repeat[opt_count]', [])]
        extra = int(rec.get('opt6', 0))
        what = f'drop of worn {item}'
        if not session.get('in_world'):
            log.info(f'[DROP] {what} outside the world - ignored')
            return
        char = self._session_char(session)
        if char is None:
            return
        if len(opts) > P.OPTION_LIST_MAX:
            return self._refuse_drop(sock, session, what, f'{len(opts)} option words (max {P.OPTION_LIST_MAX})')
        why = self._groundable(item)
        if why is None and en_item_type(item) != EC.TYPE_EQUIPMENT:
            why = f'EN Type {en_item_type(item)} != 1 (the client sends only equipment)'
        if why is not None:
            return self._refuse_drop(sock, session, what, why)
        words = invmod.block_from_wire(opts, extra)
        uid = P.session_uid(session) or 0
        with self._combat_lock(session):
            bag = invmod.Inventory(char)
            slot, entry = bag.unequip_item(item, words)
            if entry is None:
                return self._refuse_drop(sock, session, what, f'not worn with block {words}')
            bag.recompose(item, 0, self._record_gender(session, char))
            self.store.mark_dirty(f'drop worn {item}')
            fields = invmod.item_fields('0x24', item, uid=uid, block=invmod.echo_block_fields('0x24', opts, extra))
            P.send(self, sock, session, '0x24', fields)
            # The clients that hold this player: the grid clear and the empty hand (F9 step 4),
            # with the model's block their copy holds (P5 stage 4: holders only, see
            # presence.to_holders; it was every map peer before).
            presence.to_holders(self, session, '0x24',
                                self._observer_item_fields('0x24', session, item, entry['w']), 'EQUIP')
            # The 0x24 handler reruns FUN_0041ace0 + FUN_00427d40/f40(0): the maxima drop with
            # the item's bonuses and the current values are clamped (never healed).
            self._refresh_vitals(session, char, reason=f'drop worn {item}')
            x, y = session.get('pos') or (0.0, 0.0)
            placed = self._place_ground_item(session, item, 1, entry['w'], x=x, y=y, owner_uid=0,
                                             source_uid=uid, what=f'{session.get("char_name")} dropped it (worn)',
                                             source_holders=presence.holders(self, session))
        log.info(f'[DROP] {session.get("char_name")} dropped worn {EC.item_name(item)} ({item}) from slot '
                 f'{slot} -> 0x24 + 0x12 (ground id {placed.ground_id}), {self._look_note(char, en_item(item))}')

    # ------------------------------------------------------------------------
    # Crafting completions (P4 stage 3: item_inventory-crafting, -reinforcement, -gathering;
    # item_inventory.md F11-F13). crafting.py decides the result and applies to the bag model
    # exactly the change the client's result handler makes from its own item table, so these
    # handlers only add what needs the session: the rate limit, the ONE reply, persistence,
    # the quest re-check and the log. They replace the interim failure replies of
    # item_inventory-interim-craft-replies (B12), which left every craft failing.
    #
    # The client waits for nothing but its busy flag ([ctx+0x2C] craft / reinforce, [ctx+0x28]
    # gather), which only the result clears ("Not allowed during concoction, mineral
    # refining, or reinforcement." until then), so there is no silent refusal: a request the
    # server will not honour still gets the no-change result.
    # ------------------------------------------------------------------------
    # The bar is 5000 ms (4500 for a 2009 gather with a certain pet out); two completions of
    # one bar closer than this are not from the client's own timer (crafting.MIN_INTERVAL_SECS).
    CRAFT_MIN_INTERVAL_SECS = craftmod.MIN_INTERVAL_SECS
    # The dice (random.randrange). An instance attribute in tests pins the rolls.
    CRAFT_RNG = random

    def _craft_too_soon(self, session, key):
        """None when this completion may be processed, else why not. `key` is the session
        stamp of its progress bar: 'craft_last_ms' (window 0x293, shared by Concoction,
        Mineral Refining and Reinforcement) or 'gather_last_ms' (window 0x295) - the names of
        item_inventory.md 3.3. Stamps the accepted one."""
        now = time.monotonic() * 1000.0
        last = session.get(key)
        if last is not None and now - last < self.CRAFT_MIN_INTERVAL_SECS * 1000.0:
            return (f'{(now - last) / 1000.0:.1f} s after the previous completion '
                    f'(the progress bar takes 5 s)')
        session[key] = now
        return None

    def _handle_craft_complete(self, sock, session, rec, no_enc=False):
        """C2S 0x67 RefiningConcoctionComplete {u16 product_item_id} -> S2C 0x8D {result,
        product_item_id} (item_inventory F11; spec 0x46873D/0x67, 2009 0x4724C7/0x67).

        The reply echoes the client's id (an id without a client record would be dropped
        with the busy flag still set). Results (crafting.craft): 1 materials out + product
        in; 0 roll failed, materials out; 0x11 no room for the product, materials out; 0x0F
        short of a material / 0x10 skill not learned / 2 not a recipe or too soon: nothing
        changes. The success chance is the one the client's window printed (its own table,
        crafting.CRAFT_SUCCESS_PCT). No 0x18 / 0x23 follows: the client applied it all."""
        product = int(rec.get('product_item_id', 0))
        char = self._session_char(session)
        if char is None or not session.get('in_world'):
            log.info(f'[CRAFT] 0x67 product {product} outside the world - the refusal answers it')
            return
        with self._combat_lock(session):
            too_soon = self._craft_too_soon(session, 'craft_last_ms')
            if too_soon is not None:
                outcome = craftmod.CraftOutcome(craftmod.RESULT_GENERIC, product, None, 0, 0, [], False, too_soon)
            else:
                outcome = craftmod.craft(char, product, rng=self.CRAFT_RNG)
            if outcome.consumed or outcome.granted:
                self.store.mark_dirty(f'craft {product}')
            P.send(self, sock, session, '0x8D', {'result': outcome.result, 'product_item_id': product & 0xFFFF})
        kind = craftmod.CRAFT_KIND_NAMES.get(outcome.kind, 'craft')
        used = ', '.join(f'{m.item_id} x{m.count}' for m in outcome.consumed) or 'nothing'
        log.info(f'[CRAFT] {session.get("char_name")} {kind} {EC.item_name(product)} ({product}) -> 0x8D '
                 f'result {outcome.result:#x}: {outcome.why}; used {used}'
                 + (f', +1 {product}' if outcome.granted else ''))
        # What left or entered the bag may be a quest Demand item (the 0x59 re-check).
        for m in outcome.consumed:
            self._quest_credit_item(sock, session, m.item_id, m.count, no_enc)
        if outcome.granted:
            self._quest_credit_item(sock, session, product, 1, no_enc)

    def _handle_reinforce_complete(self, sock, session, rec, no_enc=False):
        """C2S 0x68 ReinforcementComplete {u16 equip_item_id, u16 stone_item_id, 6 x u16
        equip_option_word} -> S2C 0x8E (item_inventory F12; spec 0x4686E8/0x68, 2009
        0x472478/0x68).

        Success (19 B): {1, equip, stone, the SIX words the client sent, new option id} -
        the client finds its bag slot by id + a memcmp of those 12 bytes, writes the new id
        into the first zero word and removes one stone; the model does the same
        (crafting.reinforce), so the option shows in the tooltip now and in the S2C 0x03 of
        every later portal and relog. Failure (5 B): {0 or 0x11, equip, stone} changes
        nothing on either side - the stone is kept."""
        equip = int(rec.get('equip_item_id', 0))
        stone = int(rec.get('stone_item_id', 0))
        words = [int(e.get('equip_option_word', 0)) for e in rec.get('repeat[6]', [])]
        char = self._session_char(session)
        if char is None or not session.get('in_world'):
            log.info(f'[REINFORCE] 0x68 {equip} + {stone} outside the world - the refusal answers it')
            return
        with self._combat_lock(session):
            too_soon = self._craft_too_soon(session, 'craft_last_ms')
            if too_soon is not None:
                outcome = craftmod.ReinforceOutcome(craftmod.RESULT_FAILED, equip, stone, words, 0, too_soon)
            else:
                outcome = craftmod.reinforce(char, equip, stone, words, rng=self.CRAFT_RNG,
                                             success_rate=self.config.get('REINFORCE_SUCCESS_PCT',
                                                                          craftmod.DEFAULT_REINFORCE_SUCCESS_PCT))
            fields = {'result': outcome.result, 'equip_item_id': equip & 0xFFFF, 'stone_item_id': stone & 0xFFFF}
            if outcome.result == craftmod.RESULT_OK:
                fields['repeat[6]'] = [{'equip_option': w & 0xFFFF} for w in outcome.old_words]
                fields['new_option_item_id'] = outcome.new_option
                self.store.mark_dirty(f'reinforce {equip}')
            P.send(self, sock, session, '0x8E', fields)
        log.info(f'[REINFORCE] {session.get("char_name")} {EC.item_name(equip)} ({equip}) block {words} + '
                 f'{EC.item_name(stone)} ({stone}) -> 0x8E result {outcome.result:#x}: {outcome.why}'
                 + (f'; option {EC.item_name(outcome.new_option)} ({outcome.new_option}) added'
                    if outcome.result == craftmod.RESULT_OK else '; nothing consumed'))
        if outcome.result == craftmod.RESULT_OK:
            self._quest_credit_item(sock, session, stone, 1, no_enc)

    def _handle_gather_complete(self, sock, session, rec, no_enc=False):
        """C2S 0x69 GatheringComplete {u32 gather_node_record_index, u16 tool_item_id} -> S2C
        0x8F (item_inventory F13; spec 0x4685B2/0x69, 2009 0x472602/0x69).

        The index is the node's hni template position (en_content.gather_nodes). Success
        (5 B): {1, tool, reward}; failure (3 B): {0x11 no room | 0 anything else, tool}. The
        client removes one tool on EVERY result when the tool has an item record, so the
        model removes it first whatever happens (crafting.gather) - the interim stub already
        mirrored that - and adds the reward only on result 1."""
        tool = int(rec.get('tool_item_id', 0))
        index = int(rec.get('gather_node_record_index', 0))
        char = self._session_char(session)
        if char is None or not session.get('in_world'):
            log.info(f'[GATHER] 0x69 node {index} tool {tool} outside the world - the refusal answers it')
            return
        map_code = session.get('current_map')
        with self._combat_lock(session):
            too_soon = self._craft_too_soon(session, 'gather_last_ms')
            outcome = craftmod.gather(char, map_code, index, tool, rng=self.CRAFT_RNG,
                                      divisor=self.config.get('GATHER_RATE_DIVISOR',
                                                              craftmod.DEFAULT_GATHER_RATE_DIVISOR),
                                      refuse=too_soon)
            if outcome.tool_removed or outcome.reward:
                self.store.mark_dirty(f'gather {tool}')
            fields = {'result': outcome.result, 'tool_item_id': tool & 0xFFFF}
            if outcome.result == craftmod.RESULT_OK:
                fields['reward_item_id'] = outcome.reward
            # The reward is read only when the tool has a client record (spec 0x8F grammar).
            P.send(self, sock, session, '0x8F', fields,
                   {'item_def(tool_item_id) == null': not en_item_exists(tool)})
        node = outcome.node
        log.info(f'[GATHER] {session.get("char_name")} map {map_code} node {index} '
                 f'({node.name if node is not None else "?"}) tool {EC.item_name(tool)} ({tool}) -> 0x8F '
                 f'result {outcome.result:#x}: {outcome.why}; tool '
                 f'{"used up" if outcome.tool_removed else "not in the server bag"}'
                 + (f', +1 {EC.item_name(outcome.reward)} ({outcome.reward})' if outcome.reward else ''))
        if outcome.tool_removed:
            self._quest_credit_item(sock, session, tool, 1, no_enc)
        if outcome.reward:
            self._quest_credit_item(sock, session, outcome.reward, 1, no_enc)

    def _handle_stone_extract(self, sock, session, rec, no_enc=False):
        """C2S 0x72 ElementalStoneExtract {u16 mode_id, u16 equip_item_id, u16 stone_item_id,
        5 x u16 socket_stone_id, u16 equip_extra} -> S2C 0x9C + 0x18 (item_inventory F14,
        item_inventory-stone-extraction; spec 0x46C0B8/0x72, 2009 0x4765EE/0x72 identical).

        mode_id is the Element Separator the window was opened with (crafting.EXTRACT_*): its
        cash record must be usable (cash.find = the client's FUN_0045E760). Success, 21 B:
        0x9C {1, equip, the 6 words the client sent, the removed stone (never 0), the tool's
        serial} - the client removes that stone from the bag slot matching id + those 12
        bytes, shifts the rest and consumes one use of the serial (FUN_00464380 / 2009
        FUN_0046df70); the model does the same (crafting.extract + cash.consume). Then 0x18
        {gold, victy, stone, 1}: 0x9C grants nothing. Every refusal is 0x9C {0} ("Elemental
        stone extraction failed.") and changes nothing on either side. One reply per request
        (the client's busy flag, registry.MUST_REPLY 0x72): a request from outside the world
        (the mall, a map load) gets the policy's refusal."""
        mode = int(rec.get('mode_id', 0))
        equip = int(rec.get('equip_item_id', 0))
        stone = int(rec.get('stone_item_id', 0))
        words = [int(e.get('socket_stone_id', 0)) for e in rec.get('repeat[5]', [])][:invmod.WIRE_OPTION_WORDS]
        words = words + [0] * (invmod.WIRE_OPTION_WORDS - len(words)) + [int(rec.get('equip_extra', 0))]
        char = self._session_char(session)
        if char is None or not session.get('in_world'):
            log.info(f'[EXTRACT] 0x72 tool {mode} on {equip} outside the world - the refusal answers it')
            return
        tool = left = None
        with self._combat_lock(session):
            if craftmod.extract_tool_kind(mode) is not None:
                tool = self.cash.find(char, mode)
            outcome = craftmod.extract(char, mode, equip, stone, words, rng=self.CRAFT_RNG,
                                       tool_owned=tool is not None)
            if outcome.result == craftmod.RESULT_OK:
                left = self.cash.consume(char, tool['serial'], 1, what=f'extract {mode}')
                self.store.mark_dirty(f'extract {equip}')
                P.send(self, sock, session, '0x9C', {
                    'result': craftmod.RESULT_OK, 'equip_item_id': equip & 0xFFFF,
                    'repeat[5]': [{'socket_stone': w & 0xFFFF} for w in outcome.old_words[:invmod.WIRE_OPTION_WORDS]],
                    'socket_extra': outcome.old_words[invmod.WIRE_OPTION_WORDS] & 0xFFFF,
                    'stone_id': outcome.stone_id, 'cash_item_serial': tool['serial'] & 0xFFFFFFFF})
                P.send(self, sock, session, '0x18', invmod.currency_fields(self._wallet_of(session),
                                                                          outcome.stone_id, 1))
            else:
                P.send(self, sock, session, '0x9C', {'result': craftmod.RESULT_FAILED})
        if outcome.result != craftmod.RESULT_OK:
            log.info(f'[EXTRACT] {session.get("char_name")} tool {EC.item_name(mode) or mode} ({mode}) on '
                     f'{EC.item_name(equip) or equip} ({equip}) block {words}, stone {stone} -> 0x9C {{0}}: '
                     f'{outcome.why}; nothing changed')
            return
        state = 'not in the model' if left is None else f'{left} use(s) left' if left else 'record gone'
        log.info(f'[EXTRACT] {session.get("char_name")} {EC.item_name(equip)} ({equip}) block {words}: '
                 f'{EC.item_name(outcome.stone_id)} ({outcome.stone_id}) out ({outcome.why}) -> 0x9C + 0x18; '
                 f'tool {EC.item_name(mode)} ({mode}) serial {tool["serial"]:#x}: {state}')
        self._quest_credit_item(sock, session, outcome.stone_id, 1, no_enc)

    def _handle_room_query(self, sock, session, payload, no_enc):
        """0x2C inbound = room/arena list query (u8 code), sent once at spawn.
        Consume; answering with 0x33 unprompted opens the arena UI."""
        code = payload[0] if payload else 0
        log.info(f'[0x2C] room query code={code} - consumed')

    # spec_2009 0x4455DD/0x2C list_type -> the reset packet that starts that list (empty:
    # no rooms exist before the pvp room model). 0 = a list window closed: never answered.
    ROOM_LIST_RESET_2009 = {
        1: ('0x33', {'room_count': 0}),                       # arena list, window 0x2A (F key)
        4: ('0xA1', {'room_count': 0}),                       # play room list, window 0x478 (P key)
        5: ('0xC2', {'total_rooms': 0, 'room_count': 0}),     # instance dungeon list, window 0x4C4
    }

    def _handle_room_list(self, sock, session, rec, no_enc=False):
        """C2S 0x2C RoomListWindowOpenClose {u8 list_type} (2009 only; spec_2009 0x4455DD/0x2C).

        One opcode for the three room-list windows: 1 / 4 / 5 = the arena / play room /
        instance dungeon list was opened, 0 = any of them closed (also sent unsolicited when
        S2C 0x2F closes the windows). The client does not wait, but an opened list shows what
        the server sends: the matching reset packet with no rows (S2C 0x33 / 0xA1 / 0xC2 -
        0xC2 also closes message box 0x16) until pvp rooms exist. The resets walk the local
        player for every row they store (spec_2009 0x33 hazard), so only in world. 0 and
        unknown types get nothing."""
        list_type = int(rec.get('list_type', 0))
        reply = self.ROOM_LIST_RESET_2009.get(list_type)
        if reply is None:
            log.info(f'[ROOMS] 0x2C list_type {list_type} ({"closed" if list_type == 0 else "unknown"}) '
                     f'- consumed, no reply')
            return
        if not session.get('in_world'):
            log.info(f'[ROOMS] 0x2C list_type {list_type} outside the world - no reply')
            return
        key, fields = reply
        P.send(self, sock, session, key, fields)
        log.info(f'[ROOMS] 0x2C list_type {list_type} opened -> {key} empty list (no rooms yet)')

    # spec_2009 0xB3 GuildMessage sub-codes the server uses (s2c_new_1 0xB3 analysis).
    GUILD_SUB_INFO_FAILED = 15     # u8 result: 1 = "Failed to get guild info" (+0x12 = 0xFFFF),
    GUILD_NOT_IN_GUILD = 0         #   anything else = not in a guild (local entity+0x12 = 0)
    GUILD_SUB_REREQUEST = 19       # makes the client send C2S 0x8A again: NEVER an answer to 0x8A
    GUILD_SUB_FLAG_GM = 37         # u32 uid: that entity's +0x12 = 1 ("Game Master" nameplate)

    def _receiver_guild_id(self, session):
        """The guild id the receiving client holds at local entity+0x12 (spec_2009 0x07
        gm_or_guild_id, 0xB3 sub 3). No guild model exists, so 0: "not in a guild"."""
        return 0

    def _handle_guild_info(self, sock, session, payload, no_enc=False):
        """C2S 0x8A GuildInfoRequest (spec_2009 0x453581/0x8A, 0 B): the 2009 client sends it
        by itself at the end of every S2C 0x03 (right after 0x2F and 0x63), i.e. after every
        map load. Reply: S2C 0xB3 sub 3 (full guild info) for a guild member, else sub 15 with
        result != 1 - the client then sets its entity+0x12 guild id to 0 and shows nothing.
        Nobody is in a guild yet, so it is always sub 15 result 0.

        Never sub 19: it makes the client re-send 0x8A (0x48456D/0x8A), an endless loop
        (spec_2009 0xB3 gates_and_hazards). The sub handler needs the guild object the 0x03
        handler just initialised, so outside the world nothing is sent."""
        if not session.get('in_world'):
            log.info('[GUILD] 0x8A outside the world - no reply')
            return
        guild = self._receiver_guild_id(session)
        fields = {'sub': self.GUILD_SUB_INFO_FAILED, 's15_result': self.GUILD_NOT_IN_GUILD}
        assert fields['sub'] != self.GUILD_SUB_REREQUEST
        P.send(self, sock, session, '0xB3', fields)
        log.info(f'[GUILD] 0x8A guild info (guild id {guild}) -> 0xB3 sub 15 result 0 (not in a guild)')
        # Sub 15 sets the local entity+0x12 to 0, and +0x12 == 1 is also what draws "Game
        # Master" on the nameplate (FUN_0043c300): the 0x07 said 1, the answer wiped it (live
        # 2009: the tag vanished after the first map change). Sub 37 {uid} puts the 1 back.
        if session.get('gm') == 1 and not session.get('gm_hidden'):
            P.send(self, sock, session, '0xB3', {'sub': self.GUILD_SUB_FLAG_GM,
                                                 's37_uid': P.session_uid(session) or 0})
            log.info('[GUILD] GM nameplate restored: 0xB3 sub 37 after the sub 15')

    # S2C 0x1A per-record size (spec 0x1A length): 82 B + 6 per effect entry + 4 when
    # server_controlled carries cmd_hold_ms. One packet holds at most MAX_PKT - header -
    # opcode = 2038 payload bytes (make_raw_packet masks the size to 11 bits, so a longer
    # one would be cut, not refused) and a u8 count.
    MOB_RECORD_BASE_BYTES = 82
    MAX_S2C_PAYLOAD = MAX_PKT - HEADER_SIZE - 1

    def _monster_spawn_record(self, mob):
        """One S2C 0x1A NpcMonsterSpawn block for `mob` (spec 0x1A, handler 0x451D8D) with the
        client's own idle defaults (world-1a-defaults, world_movement_npc.md 3.5, gate G3).

        The values are what RegisterLocalPlayer seeds a type-4 entity with (spec 0x1A
        semantics step 5: 0x8B3 = 8, 0x8BD = 2, 0x8CF = 8, 0x904 = 8) - the 0x1A fields
        overwrite those defaults, so anything else is a state the client never starts in:
        - action_state (+0x904) 8 = idle; unk_8cf 8, unk_8bd 2, unk_954 0, action_elapsed 0.
          The old builder copied 0x07's non-idle seed (904 = 0, 8CF = 1, 954 = 32,
          E00 = 501, facing 127,0,0,1 = the IP octets): live world_movement_npc#20 showed
          that seed, not the missing effect, was what hid the body (world doc Q4, B12).
        - facing 8 (neutral), motion/motion_8b5/motion_8b6 0.
        - effect_count 0: with the idle defaults the body renders without any entry
          (T-1A-1). The old 0x0B3B entry is Mutation Lv1, a status effect (FUN_00424e20),
          and config MOB_SPAWN_EFFECT_ID can put one back.
        - template_index: the hni position (world-template-map); an unbound index leaves
          +0x11DC NULL (no body, no name, nameplate NULL-deref risk), so _spawn_map_monsters
          never builds a monster without one.
        - home = spawn point (the leash home), pos = current position, respawn_tick 0 for a
          live entity (the client self-revive only runs in the dead action 0x16).
        - cur_hp = current HP; 0 would spawn a corpse the client never registers hits on.
        - server_controlled 1 + cmd_hold_ms 0 (config MOB_SERVER_CONTROLLED): the mob stands
          idle on its floor where the server's x/y say it is (world_movement_npc#20: idle
          904 = 8 for 7 s). With 0 the client wander AI walks it off at 82.5 px/s while the
          memory combat driver keeps hitting the static server position.
          Decomp consequence to watch live: a type-4 entity is only ticked (FUN_00413920,
          which also advances the +0xE00 animation clock) while (hold +0xE48 && +0x8E4) or
          the wander-AI flag +0x8E1 is set, and FUN_00412100 zeroes the hold of a fully idle
          entity (904 8, motion 0, facing 8). A server-controlled idle mob therefore shows
          its idle pose without cycling frames; animated idling needs the client AI
          (MOB_SERVER_CONTROLLED false) or server 0x1B commands (world-monster-ai-1b)."""
        effect = int(self.config.get('MOB_SPAWN_EFFECT_ID', 0) or 0)
        controlled = bool(self.config.get('MOB_SERVER_CONTROLLED', False))
        rec = {
            'template_index': int(mob.template or mob.npccode), 'uid': mob.uid,
            'effect_count': 1 if effect else 0,
            'repeat[effect_count]': [{'effect_id': effect, 'effect_duration_ms': 0}] if effect else [],
            'wander_motion': 0, 'wander_rng_cursor': 0, 'wander_mode': 0, 'wander_timer_ms': 0,
            'home_x': int(mob.spawn_x), 'home_y': int(mob.spawn_y), 'respawn_tick': 0,
            'unk_954': 0, 'unk_8d9': 0, 'unk_8cf': 8, 'action_state': 8, 'action_elapsed_ms': 0,
            'unk_8bd': 2, 'pos_x': float(mob.x), 'pos_y': float(mob.y), 'elapsed_e50': 0,
            'facing': 8, 'motion': 0, 'motion_8b5': 0, 'motion_8b6': 0,
            'unk_8da': 0, 'unk_8df': 0, 'unk_8dc': 0, 'unk_8dd': 0, 'unk_8de': 0,
            'cur_hp': max(0, min(0xFFFF, int(mob.hp))), 'timer_d94': 0,
            'server_controlled': 1 if controlled else 0,
        }
        if controlled:
            rec['cmd_hold_ms'] = 0
        if getattr(self, 'client_build', None) == cfgmod.BUILD_2009:
            # spec_2009 0x1A: same fields and widths, pos_x/pos_y moved to right after the
            # effect list and the entity-offset names moved with the 2009 entity (+0x8C..
            # +0xA0). The wire order is the grammar's; only the names change here, so the
            # values stay the live-proven idle ones (wsproto would send 0 for a 2008 name).
            rec = {self.MOB_NAMES_2009.get(k, k): v for k, v in rec.items()}
        return rec

    # 0x1A record 2008 name -> 2009 name (spec_2009 0x1A diff_vs_2008).
    MOB_NAMES_2009 = {
        'unk_954': 'unk_9e4', 'unk_8d9': 'unk_965', 'unk_8cf': 'unk_95b', 'unk_8bd': 'unk_949',
        'elapsed_e50': 'elapsed_eec', 'motion_8b5': 'motion_941', 'motion_8b6': 'motion_942',
        'unk_8da': 'unk_966', 'unk_8df': 'unk_96b', 'unk_8dc': 'unk_968', 'unk_8dd': 'unk_969',
        'unk_8de': 'unk_96a', 'timer_d94': 'timer_e24',
    }

    @classmethod
    def _monster_record_bytes(cls, rec):
        return (cls.MOB_RECORD_BASE_BYTES + 6 * int(rec['effect_count'])
                + (4 if rec['server_controlled'] else 0))

    @classmethod
    def monster_spawn_batches(cls, records):
        """Split 0x1A records into packets: as many per packet as fit the 2038-byte payload
        (1 count byte + records; 23 with server_controlled, 24 without, F6 step 6)."""
        batches, cur, size = [], [], 1
        for rec in records:
            n = cls._monster_record_bytes(rec)
            if cur and (size + n > cls.MAX_S2C_PAYLOAD or len(cur) == 0xFF):
                batches.append(cur)
                cur, size = [], 1
            cur.append(rec)
            size += n
        if cur:
            batches.append(cur)
        return batches

    def _send_monster_spawns(self, sock, session, mobs):
        """S2C 0x1A for `mobs`, batched. The client never de-duplicates uids, so a caller
        must only send it for uids with no entity in the receiver's scene (fresh map load,
        or after 0x06 removed the corpse)."""
        records = [self._monster_spawn_record(m) for m in mobs]
        for batch in self.monster_spawn_batches(records):
            payload = P.build('0x1A', {'count': len(batch), 'repeat[count]': batch},
                              receiver_uid=P.session_uid(session), client_build=self.client_build)
            # world doc 3.7: every builder asserts the 2038-byte limit, because the frame
            # size would be masked to 11 bits and the client would read a cut packet.
            assert len(payload) <= self.MAX_S2C_PAYLOAD, f'0x1A {len(payload)} B > {self.MAX_S2C_PAYLOAD}'
            self._send_encrypted(sock, session, 0x1A, payload, use_by_array=session.get('no_enc', True))

    def _send_monster_spawn(self, sock, session, mob):
        """S2C 0x1A for one monster (the respawn path)."""
        self._send_monster_spawns(sock, session, [mob])

    def _send_hp(self, sock, session, hp, no_enc=None):
        """S2C 0x28 SetLocalHp {u16 hp}: the ABSOLUTE current HP of the local player. Built
        from the spec (F1). no_enc None = the receiver's own mode (session['no_enc'])."""
        body = P.build('0x28', {'hp': max(0, int(hp)) & 0xFFFF}, client_build=self.client_build)
        self._send_encrypted(sock, session, 0x28, body,
                             use_by_array=session.get('no_enc', True) if no_enc is None else no_enc)

    def _send_mp(self, sock, session, mp, no_enc=None):
        """S2C 0x44 SetLocalMp {u16 mp}: the ABSOLUTE current MP of the local player."""
        body = P.build('0x44', {'mp': max(0, int(mp)) & 0xFFFF}, client_build=self.client_build)
        self._send_encrypted(sock, session, 0x44, body,
                             use_by_array=session.get('no_enc', True) if no_enc is None else no_enc)

    # ------------------------------------------------------------------------
    # cs-hp-mp-model / cs-regen: the server's HP/MP writes. hpmp.py holds the formulas;
    # these are the only places that change session['hp'] / ['mp'] outside a map load,
    # always under the session's combat lock (cs-combat-lock), and the absolute value
    # goes out inside the same critical section so a potion's 0x25 (a client-side DELTA,
    # sent under the same lock) can never be reordered around it.
    # ------------------------------------------------------------------------
    def _refresh_vitals(self, session, char, *, full=False, reason=''):
        """Recompute the maxima after an event that changes them on the client too (level,
        stat point, equip/unequip) and mirror the client's clamp or level-up full heal.
        Returns the hpmp.Derived values. Nothing is sent: the client applies the same rule
        to its own copy (a level-up heal via 0x21, a clamp via the recompute)."""
        with self._combat_lock(session):
            before = (session.get('max_hp'), session.get('max_mp'), session.get('hp'), session.get('mp'))
            d = hpmp.refresh(session, char, full=full)
            after = (d.max_hp, d.max_mp, session['hp'], session['mp'])
        if before != after:
            log.info(f'[HPMP] {reason or "refresh"}: HP {after[2]}/{after[0]} MP {after[3]}/{after[1]}'
                     f'{" (full heal)" if full else ""}')
        return d

    def _set_vitals(self, session, hp=None, mp=None, *, reason=''):
        """Set the current HP and/or MP (clamped to [0, max]) and send the absolutes to an
        in-world client. The primitive every server-side change goes through: dev commands
        now, cs-player-death's damage_player and the skill costs later. Returns (hp, mp)."""
        sock = session.get('sock')
        with self._combat_lock(session):
            char = self._session_char(session)
            if char is not None and not session.get('max_hp'):
                hpmp.refresh(session, char)
            if hp is not None:
                session['hp'] = hpmp.clamp_hp(session, hp)
            if mp is not None:
                session['mp'] = hpmp.clamp_mp(session, mp)
            if sock is not None and session.get('in_world'):
                if hp is not None:
                    self._send_hp(sock, session, session['hp'])
                if mp is not None:
                    self._send_mp(sock, session, session['mp'])
            cur = (session.get('hp'), session.get('mp'))
        log.info(f'[HPMP] {reason or "set"}: HP {cur[0]}/{session.get("max_hp")} '
                 f'MP {cur[1]}/{session.get("max_mp")}')
        return cur

    # How often the regen ticker looks for due 15 s windows. The windows are per session, so
    # this only bounds how late one fires (<= 1 s after the client's own 15000 ms would).
    REGEN_TICK_SECS = 1.0

    def _tick_regen(self, now=None):
        """cs-regen: run every in-world session's due regen timers (hpmp.regen_step) and push
        the new absolutes with plain S2C 0x28 / 0x44 - no 0x3D, whose heal effect and sound
        are for received heals (combat_skill.md F11). Runs on the tick scheduler under the
        world lock every REGEN_TICK_SECS; the 15 s windows themselves are per session
        (session['regen'], restarted by every map load and by C2S 0x0D idle changes), so each
        player regenerates on his own phase like the client's per-entity timers.

        Skipped: sessions not in world (character select, a map load in flight), dead ones
        (cs-player-death sets session['dead']), and mode-1 room maps where the UDP host owns
        HP (roadmap 1.11a). Returns the number of packets sent (tests)."""
        now = time.monotonic() if now is None else now
        period = float(self.config.REGEN_SECS)
        sent = 0
        for session in list(self.sessions.values()):
            st = session.get('regen')
            if (st is None or not session.get('in_world') or session.get('dead')
                    or session.get('current_map') in hpmp.ROOM_MAPS):
                continue
            hp_due = st.get('hp_due')
            if now < st['mp_due'] and (hp_due is None or now < hp_due):
                continue
            char = self._session_char(session)
            sock = session.get('sock')
            if char is None or sock is None:
                continue
            try:
                with self._combat_lock(session):
                    d = hpmp.derive(session, char)
                    session['max_hp'], session['max_mp'] = d.max_hp, d.max_mp
                    old = (session.get('hp'), session.get('mp'))
                    new_hp, new_mp = hpmp.regen_step(session, d, now, period)
                    if new_hp is not None:
                        self._send_hp(sock, session, new_hp)
                        sent += 1
                    if new_mp is not None:
                        self._send_mp(sock, session, new_mp)
                        sent += 1
            except OSError as e:
                # A socket that died between the in_world check and the send: its own
                # connection thread cleans the session up.
                log.debug(f'[REGEN] {session.get("char_name")!r}: send failed ({e})')
                continue
            if new_hp is not None or new_mp is not None:
                log.info(f'[REGEN] {session.get("char_name")!r}: HP {old[0]}->{session.get("hp")}/{d.max_hp} '
                         f'MP {old[1]}->{session.get("mp")}/{d.max_mp}')
        return sent

    # S2C 0x22 SetLevel goes only to OBSERVERS (grant_exp -> presence.to_holders,
    # lc-level-broadcast): the owner must never get one - his client already levelled itself
    # on 0x21 and a second effect/sound 0x24 plays twice (S2-43/B4).

    # ---- LOCAL-MEMORY COMBAT DRIVER -------------------------------------------
    # The EN basic attack is client-side only (sends no packet). Since the server
    # runs on the same machine, read the local client's swing flag (2008 +0x8b8, 2009
    # +0x944) and position from memory and apply damage to nearby monsters on each swing.
    # Every address is the server build's (self.client_layout, client_layout.py).
    CLIENT_EXE = CL.EXE_NAMES['1']          # WindSlayer_patched.exe in both installs

    def _find_client_pid(self, pids=None, stamp_of=None):
        """First WindSlayer_patched.exe of the server's build, or 0. The 2008 and 2009 exes
        share that name, so the PE TimeDateStamp of the running image decides (2008
        0x48240544, 2009 0x49797E29); a process the driver cannot read is skipped (it needs
        ReadProcessMemory anyway). pids / stamp_of: injectable for offline tests."""
        if pids is None:
            pids = CL.process_ids(self.CLIENT_EXE)
        mine = CL.select_pids(pids, stamp_of or CL.image_timestamp,
                              self.client_layout.pe_timestamp, strict=True)
        return mine[0] if mine else 0

    def _memory_melee(self, px, py, uid=None):
        """Apply one melee hit to the nearest in-range monster of the map of the session
        whose account uid is `uid`: the client the driver read the swing from (lc-uid-online,
        F10 dev path; party-dep-combat-multiclient: the swing is that session's, so the kill
        credit, exp and drop are too). uid None swings once per shared monster set of the
        sessions online (offline tests, the pre-P1 behaviour) - never twice at one map's
        monsters because two players stand on it.

        Target pick and hit run under the session's combat lock and its map's monster lock
        (cs-combat-lock, S1-08c: the driver thread used to iterate mons.values() while a
        portal on the connection thread replaced the dict)."""
        if uid is None:
            targets, seen = [], set()
            for session in list(self.sessions.values()):
                mons = session.get('monsters')
                if mons and id(mons) not in seen:
                    seen.add(id(mons))
                    targets.append(session)
        else:
            owner = self.world.session(uid)
            targets = [owner] if owner is not None else []
        for session in targets:
            sock = session.get('sock')
            if not sock or not session.get('monsters'):
                continue
            with self._combat(session):
                best, bd = None, None
                for mob in (session.get('monsters') or {}).values():
                    if not mob.alive:
                        continue
                    dx, dy = abs(mob.x - px), abs(mob.y - py)
                    if dx <= MELEE_RANGE_X and dy <= MELEE_RANGE_Y:
                        d = dx + dy
                        if bd is None or d < bd:
                            best, bd = mob, d
                if best is not None:
                    try:
                        self._resolve_hit(sock, session, 0, best.uid, no_enc=True)
                    except Exception as ex:
                        log.info(f'[ATK] memory-melee send failed: {ex}')

    def _tick_wander(self, sock, session, no_enc=True):
        # DISABLED: 0x12 is the ground-item DROP opcode, not entity-walk. The real
        # entity-move opcode is still unknown, so monsters stay static.
        return

    # Client memory the driver reads: self.client_layout (client_layout.py; 2008 scene
    # 0x70EECC, spec 0x02: scene+0x220 = account_id, entity uid +0x84; 2009 0x54F0C0 / +0x224
    # / +0x88). A bare GameServer.__new__ (tests) reads the 2008 layout.
    client_layout = CL.LAYOUT_2008
    # Incremented by _map_transfer; the combat driver drops its cached entity when it
    # changes (see the comment there). Plain int writes/reads are atomic under the GIL.
    _driver_epoch = 0

    def _driver_local_player(self, u32):
        """(entity address, uid) of the attached client's local player, read with `u32(addr)`.

        The uid is the client's own scene+0x220 (2009 +0x224), which S2C 0x02 set from the
        account uid of the session that logged in on it (lc-uid-online,
        party-dep-combat-multiclient). The entity walk used to look for uid 1, so an admin
        (uid 2) client had no local player and every client's swings hit monsters of uid 1.
        Returns (0, 0) when the client is not in a scene or no logged-in session holds that
        uid, (0, uid) when the entity is not spawned yet."""
        lay = self.client_layout
        scene = u32(lay.scene_ptr)
        if not scene:
            return 0, 0
        uid = u32(scene + lay.scene_local_uid)
        if not uid or self.world.session(uid) is None:
            return 0, 0
        node = u32(scene + lay.scene_entity_list); n = 0
        while node and 0x400000 <= node < 0x7FFF0000 and n < 60:
            e = u32(node + 8)
            if e and u32(e + lay.ent_uid) == uid:
                return e, uid
            node = u32(node); n += 1
        return 0, uid

    def _driver_read_player(self, player, u8, u32, f64):
        """(attacking, x, y, facing) of the local player entity at `player`, read with the
        build's offsets: swing flag 2008 +0x8B8 / 2009 +0x944, render state +0x15B4 /
        +0x166C, f64 position +0x11F8,+0x1288 / +0x1298,+0x1328, facing byte +0x8BD / +0x949
        (2 left, 6 right, kept after release).

        ATTACK-specific: the swing flag is set by any action (incl. jump), so the render
        state must also be 4 (idle=8, jump=other), or the jump key would trigger hits."""
        lay = self.client_layout
        attacking = u8(player + lay.ent_swing) == 1 and u32(player + lay.ent_render_state) == 4
        return (attacking, f64(player + lay.ent_pos_x), f64(player + lay.ent_pos_y),
                u8(player + lay.ent_facing))

    def _driver_scene_monsters(self, u32, f64, u8):
        """{uid: (x, y)} of the attached client's type-4 entities (monsters), read with the
        driver's memory readers and the active build's layout. One walk of the scene list."""
        lay = self.client_layout
        scene = u32(lay.scene_ptr)
        out = {}
        if not scene:
            return out
        node = u32(scene + lay.scene_entity_list); n = 0
        while node and 0x400000 <= node < 0x7FFF0000 and n < 120:
            e = u32(node + 8)
            if e and u8(e + lay.ent_type) == 4:
                out[u32(e + lay.ent_uid)] = (f64(e + lay.ent_pos_x), f64(e + lay.ent_pos_y))
            node = u32(node); n += 1
        return out

    def _track_driver_monsters(self, uid, positions, now=None):
        """Client-side positions of the session's roaming monsters. With MOB_SERVER_CONTROLLED
        false the client's wander AI moves them and no packet says where to (only a hit
        report's interact tail names a victim's point), so the server's mob.x/y stay at the
        spawn point and cs-skill-damage's box never contains a roaming Pupu (live: Ice Spear
        drew "KRAKK" on a Pupu while the server found no monster in the box). Dev path, like
        _track_driver_position: it reads the local client. Returns how many monsters moved.
        Each sample is also the position fix the monster AI's dead reckoning restarts from
        (fix_t) - but only where that client is the authority for the monster's position
        (_mob_fix_allowed: the monster chases him, or chases nobody)."""
        session = self.world.session(uid)
        if session is None or not session.get('in_world'):
            return 0
        now = time.monotonic() if now is None else now
        moved = 0
        with self._combat(session):
            for mob in (session.get('monsters') or {}).values():
                p = positions.get(mob.uid)
                if (p is None or not mob.alive or not self._mob_fix_allowed(mob, uid)
                        or not all(v == v and 0 < abs(v) < 1e7 for v in p)):
                    continue
                mob.x, mob.y = float(p[0]), float(p[1])
                mob.fix_t = now
                moved += 1
        return moved

    def _track_driver_position(self, uid, px, py, facing=None):
        """Write the attached client's own coordinates into that account's session
        (premium_cash-map-replay-helper "track session['pos'] from the combat driver's
        memory read"). Returns True when they were taken. `facing` is the entity's +0x8BD
        byte (2 left, 6 right): cs-skill-damage lays the skill hitbox in front of it.

        Dev aid only (world-position-estimate): C2S 0x0D carries coordinates only in its
        interact branch, so the server dead-reckons a walk (presence.advance) and this read
        of client memory is a truth sample for ONE local client. Every sample measures the
        estimate's error (spike S-1 / decision G1, `!where`); config POSITION_DRIVER_FIX
        (default true) also makes it the position, false leaves the estimate running on its
        own so the error can be measured. Skipped while a map load is in flight - the
        client is between maps and its entity still holds the old map's point - and while
        the mall is open. A failed read gives 0.0, which is never a real field position."""
        session = self.world.session(uid)
        if session is None or not session.get('in_world') or session.get('in_cash_shop'):
            return False
        if not (px and py) or px != px or py != py or abs(px) > 1e7 or abs(py) > 1e7:
            return False
        # After a map transfer the client keeps the old map's entity (and its coordinates)
        # until its load finishes. Live P5 (2026-09-23): a sample taken 0.1 s after a dev
        # warp wrote the old map's x back over the arrival point, and a player arriving next
        # got the stale x in its 0x04 (600 px off). Wait for the client's first 0x0D that
        # names the new map (_handle_world_sync sets map_confirmed).
        if session.get('map_confirmed') != session.get('current_map'):
            return False
        presence.driver_sample(session, float(px), float(py),
                               apply=bool(self.config.get('POSITION_DRIVER_FIX', True)))
        side = combat.facing_from_entity(facing) if facing is not None else None
        # A turn reaches the server first as a C2S 0x0D with the direction bits; the driver's
        # memory sample can be older than that packet (live: turn + cast 270 ms apart laid
        # the Ice Spear box behind the player). The packet wins for FACING_PACKET_HOLD_SECS.
        if side is not None and (time.monotonic() - float(session.get('facing_t') or 0.0)
                                 >= FACING_PACKET_HOLD_SECS):
            session['facing'] = side
        return True

    def _combat_driver(self):
        import ctypes
        from ctypes import wintypes as wt
        k = ctypes.windll.kernel32; k.OpenProcess.restype = wt.HANDLE
        proc = [None]; last = [0.0]; lw = [0.0]; lp = [0.0]; lv = [0.0]
        pcache = [0, 0, -1]   # [entity, uid, map-transfer epoch]
        def rd(a, n):
            b = (ctypes.c_ubyte * n)(); r = ctypes.c_size_t(0)
            k.ReadProcessMemory(proc[0], ctypes.c_void_p(a), b, n, ctypes.byref(r))
            return bytes(b[:r.value]) if r.value else b''
        def u32(a): d = rd(a, 4); return struct.unpack('<I', d)[0] if len(d) == 4 else 0
        def u8(a):  d = rd(a, 1); return d[0] if d else 0
        def f64(a): d = rd(a, 8); return struct.unpack('<d', d)[0] if len(d) == 8 else 0.0
        lay = self.client_layout
        log.info(f'[COMBAT] local-memory combat driver started (client build {lay.build}: '
                 f'scene 0x{lay.scene_ptr:X}, uid +0x{lay.ent_uid:X}, pos +0x{lay.ent_pos_x:X})')
        while True:
            try:
                if not proc[0]:
                    pid = self._find_client_pid()
                    proc[0] = k.OpenProcess(0x10 | 0x400, False, pid) if pid else None
                    if not proc[0]:
                        time.sleep(1.0); continue
                    log.info(f'[COMBAT] attached to client PID {pid}')
                # Liveness/identity check: if the held handle's process has died
                # (e.g. the client was relaunched to a NEW pid), the image-base 'MZ'
                # read fails -> drop the stale handle and re-resolve the current pid.
                # Without this the driver clings to the dead handle forever and
                # combat silently stops working after any client relaunch.
                if rd(0x400000, 2)[:2] != b'MZ':
                    try: k.CloseHandle(proc[0])
                    except Exception: pass
                    proc[0] = None; pcache[:] = [0, 0, -1]
                    time.sleep(0.5); continue
                # Cached player lookup: the full entity-list walk is ~120 memory
                # reads; doing it every 50ms (~2400 reads/sec) hammered CPU and
                # stuttered the game. Cache the player address and its uid and only
                # re-walk when the entity no longer carries that uid (map change,
                # relog as another account). Steady-state is ~4 reads/tick.
                player, my_uid, cached_epoch = pcache
                epoch = self._driver_epoch
                # Re-walk on a map change AND at least every DRIVER_REVALIDATE_SECS.
                # The epoch alone is not enough: it fires the instant the transfer commits,
                # while the client is still tearing down and rebuilding its entity list, so
                # the walk latches onto the entity that is about to be freed. The uid check
                # never frees it either, because the recycled block still reads uid 1 at
                # +0x84 - live P2 verification saw the driver keep the OLD map's entity, so
                # hits resolved against stale coordinates and the 60 s world save wrote a
                # map-201 point onto a character standing on map 102.
                if (epoch != cached_epoch or not player or not my_uid
                        or u32(player + lay.ent_uid) != my_uid
                        or time.monotonic() - lv[0] >= DRIVER_REVALIDATE_SECS):
                    lv[0] = time.monotonic()
                    player, my_uid = self._driver_local_player(u32)
                    pcache[:] = [player, my_uid, epoch]
                if not player:
                    time.sleep(0.3); continue
                # swing flag + attack render state, position, facing (_driver_read_player)
                attacking, px, py, facing = self._driver_read_player(player, u8, u32, f64)
                now = time.monotonic()
                # DEBUG self-test hook: a `_dbg_attack` sentinel file (created by
                # wsdev.py `hit`) forces one melee resolve on the nearest mob, so
                # combat feedback/leveling can be tested without a real keypress
                # (injected 's' doesn't set the swing flag).
                dbg = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_dbg_attack')
                if os.path.exists(dbg):
                    try: os.remove(dbg)
                    except OSError: pass
                    self._memory_melee(px, py, uid=my_uid)
                if attacking and self.config.get('DRIVER_MELEE') and now - last[0] >= 0.45:
                    last[0] = now
                    self._memory_melee(px, py, uid=my_uid)
                # Follow the player's own position (see _track_driver_position).
                if now - lp[0] >= POSITION_SAMPLE_SECS:
                    lp[0] = now
                    self._track_driver_position(my_uid, px, py, facing)
                    if not self.config.get('MOB_SERVER_CONTROLLED'):
                        self._track_driver_monsters(my_uid, self._driver_scene_monsters(u32, f64, u8))
                # periodic monster wander (server-sent 0x12 walk)
                if WANDER_ENABLED and now - lw[0] >= WANDER_PERIOD:
                    lw[0] = now
                    for s in list(self.sessions.values()):
                        if s.get('sock') and s.get('monsters'):
                            self._tick_wander(s['sock'], s, no_enc=True)
                time.sleep(0.05)
            except Exception:
                proc[0] = None
                time.sleep(0.5)

    # ========================================================================
    # PHASE 1 COMBAT — spawn monsters per map; resolve attack -> hp -> death ->
    # exp/drop -> corpse despawn -> respawn. 0x28/0x44 are LOCAL-player only.
    #
    # world-shared-monsters (P5 stage 3): ONE set of monsters per map - world.MapMonsters on
    # the MapInstance - with uids from the server-wide space (ids.MONSTER) that are the same
    # on every client there. A session whose map load sent it the map's 0x1A burst is
    # attached: session['monsters'] IS that shared dict, so a hit report, a trap, a contact
    # event or a skill box finds the same Monster whichever player named it. The spawn 0x1A,
    # the kill's 0x29, the corpse's 0x06 and the respawn's 0x1A reach every attached client
    # (the per-viewer book MapMonsters.held: never a duplicate 0x1A, never a 0x06 / 0x29 for a
    # uid a client does not hold). Exp, quest credit, loot rights and the gold 0x18 are the
    # killer's alone; the loot itself is visible to everyone on the map (ground.py). A map
    # nobody stands on keeps its monsters for MOB_MAP_KEEP_SECS - dead ones stay dead until
    # their own respawn - and is then discarded and rebuilt fresh, with the same uids, on the
    # next arrival (world F6 step 5: no kill -> leave -> return free respawn).
    #
    # party-dep-combat-multiclient: each client's hit report damages the one monster its
    # client named, credited to that client's session; the others see the hit through the
    # relay (_relay_hit) and never the attacker (his own client drew it).
    #
    # cs-combat-lock: every read-modify-write of a Monster or the session hp/mp runs under the
    # acting session's combat lock (an RLock) and then the map's monster lock (_combat):
    # the connection thread (hit report, skill, trap, contact, map load), the combat driver
    # (_memory_melee); the tick thread takes the monster lock alone for the corpse despawn /
    # respawn, the monster AI and the discard, and the caster's combat lock first for a DoT
    # (a kill's exp takes the killer's combat lock). A kill is therefore exactly once:
    # _resolve_hit checks mob.alive and _kill_monster clears it inside the same critical
    # section, whichever player hit.
    # ========================================================================
    _combat_lock_guard = threading.Lock()
    # How often the tick scheduler looks for empty maps whose monsters have been kept long
    # enough (MOB_MAP_KEEP_SECS).
    MOB_MAP_GC_SECS = 30.0

    def _combat_lock(self, session):
        """The session's combat RLock (created with the session in _handle_fireway; created
        here for sessions built elsewhere, e.g. offline tests calling handlers directly)."""
        lock = session.get('combat_lock')
        if lock is None:
            with GameServer._combat_lock_guard:
                lock = session.setdefault('combat_lock', threading.RLock())
        return lock

    @contextlib.contextmanager
    def _combat(self, session):
        """The session's combat lock, then the monster lock of the map it is attached to
        (world.MapMonsters) - the order every monster-touching path takes them in.
        session['monsters'] only changes under the combat lock (_spawn_map_monsters /
        _clear_map_monsters), so the monster lock taken here is the one of the dict the
        caller will read."""
        with self._combat_lock(session):
            lock = getattr(session.get('monsters'), 'lock', None)
            if lock is None:
                yield
            else:
                with lock:
                    yield

    @staticmethod
    def _mob_home(session):
        """The shared monster set `session` is attached to (world.MapMonsters), or None."""
        mons = session.get('monsters') if session is not None else None
        return mons if isinstance(mons, worldmod.MapMonsters) else None

    @staticmethod
    def _viewer_key(session):
        uid = session.get('uid')
        return uid if uid is not None else id(session)

    def _mob_viewers(self, mons, mob=None, exclude=None):
        """The attached, reachable sessions of `mons` - only those whose client holds `mob`
        when one is given - minus `exclude` and any session with its uid. Caller holds
        mons.lock."""
        skip = exclude.get('uid') if exclude is not None else None
        out = []
        for key, s in list(mons.viewers.items()):
            if s is exclude or (skip is not None and s.get('uid') == skip):
                continue
            if s.get('monsters') is not mons or not worldmod.reachable(s):
                continue
            if mob is not None and mob.uid not in mons.held.get(key, ()):
                continue
            out.append(s)
        return out

    def _mob_broadcast(self, mons, mob, key, fields, *, exclude=None, receivers=None):
        """S2C `key` about `mob` to every client that holds it (or to `receivers`), built for
        each receiver: `fields` is a dict or fn(receiver) -> dict (the 0x2A self-form carries
        the receiver's OWN uid). A dead peer is skipped (_push never raises). Returns the
        number sent. Caller holds mons.lock."""
        targets = self._mob_viewers(mons, mob, exclude=exclude) if receivers is None else receivers
        sent = 0
        for s in targets:
            sent += bool(self._push(s, key, fields(s) if callable(fields) else fields, 'MOB'))
        return sent

    def _spawn_map_monsters(self, sock, session, map_id, no_enc=True):
        """Attach the session to map_id's shared monsters and send its client the live ones
        (S2C 0x1A, batched) - right after its map load (0x03/0x07/0x28/0x44), which freed every
        client entity, so the client holds nothing and each 0x1A is safe. The spawn list is
        built on the map's first arrival, or again after a discard (_populate_map_monsters).

        A monster that is chasing someone goes out at the server's position of it; its next
        chase word (at most MOB_CMD_KEEPALIVE_SECS later, _mob_send_decision) takes this
        client's copy over as well. A corpse is not sent: the respawn's 0x1A reaches this
        client like everyone else's."""
        with self._combat_lock(session):
            self._clear_map_monsters(session)
            mons = self.world.map(map_id).monsters
            with mons.lock:
                if not mons.populated:
                    self._populate_map_monsters(mons)
                key = self._viewer_key(session)
                mons.viewers[key] = session
                mons.held[key] = set()
                for mob in mons.values():
                    mob.ai_takers.discard(key)       # a fresh client entity: +0x971 = 0
                session['monsters'] = mons
                if not mons:
                    log.info(f'[MOB] map {map_id} has no monster spawns (town/none)')
                    return
                live = [m for m in mons.values() if m.alive]
                if live:
                    self._send_monster_spawns(sock, session, live)
                    mons.held[key].update(m.uid for m in live)
                log.info(f'[MOB] {session.get("char_name") or session.get("username")!r} sees {len(live)} '
                         f'of the {len(mons)} shared monsters of map {map_id} '
                         f'({len(mons.viewers)} player(s) there)')

    def _populate_map_monsters(self, mons):
        """Build the map's monsters (caller holds mons.lock). Placement and stats are the EN
        client's (world-template-map): the map's event-2 tiles with a monster template
        (en_content.map_spawns) and that template's hni record. A spawn whose template the
        client does not have is skipped, never sent: its 0x1A would leave a body-less entity
        with a NULL nameplate (+0x11DC). The uids are the map's block of the server-wide
        space (World.alloc_monster_uids), allocated once and reused by a rebuild."""
        map_id = mons.map_code
        spawns = EC.map_spawns(map_id)
        catalog = EC.npcs()
        valid = []
        for sp in spawns or ():
            tpl = catalog.get(sp.npc)
            index = catalog.template_index(sp.npc)
            if tpl is None or index is None:
                log.warning(f'[MOB] map {map_id}: no EN template for npc {sp.npc}, skipping')
                continue
            valid.append((sp, tpl, index))
        if len(mons.uids) < len(valid):
            mons.uids.extend(self.world.alloc_monster_uids(len(valid) - len(mons.uids)))
        mons.clear()
        placed = []
        for (sp, tpl, index), uid in zip(valid, mons.uids):
            mob = self._make_monster(tpl, index, uid, sp.x, sp.y)
            mons[uid] = mob
            placed.append((sp, mob))
            log.debug(f'[MOB] spawn {mob.name} npc={tpl.idx} template={index} uid={uid:#x} '
                      f'at ({mob.x:g},{mob.y:g}) hp={mob.hp}')
        mons.populated = True
        # P13 boss-b1 (bosses.py, evb B5/C): a field boss the ledger still has down is built as
        # a corpse that is gone - no client gets its 0x1A - with a respawn timer for the rest
        # of its time, so a discard / rebuild or a server restart is no free boss respawn.
        for mob, wait in self.bosses.on_populate(mons, placed):
            self._cancel_monster_timers(mob)
            mob.timers = [self.ticks.call_later(wait, self._respawn_monster, mons, mob,
                                                name=f'boss-respawn-{mob.uid:#x}')]
        if mons:
            names = ', '.join(sorted({m.name for m in mons.values()}))
            log.info(f'[MOB] spawned {len(mons)} monsters on map {map_id} ({names}), uids '
                     f'{min(mons):#x}..{max(mons):#x}, shared by every player there')

    def _make_monster(self, tpl, index, uid, x, y):
        """A live Monster of EN template `tpl` (hni record; `index` = its 0x1A template_index)
        at its spawn point (x, y): the template's stats, swing stats and AI flags, its drop
        table and gold."""
        drops = [d for d in tpl.drop_ids if self._grantable_item(d, f'{tpl.name} drop table')]
        grantable = set(drops)
        hp = max(1, tpl.hp)
        return Monster(uid=uid, npccode=tpl.idx, name=tpl.name, level=tpl.lv,
                       hp=hp, max_hp=hp, body_atk=tpl.body_atk, defense=tpl.defense,
                       exp=tpl.exp, x=float(x), y=float(y),
                       spawn_x=float(x), spawn_y=float(y), drop_items=drops,
                       drop_table=[(i, r) for i, r in tpl.drops if i in grantable],
                       gold=monster_gold(tpl.idx, tpl.exp), template=index,
                       weak_atk=tpl.weak_atk, strong_atk=tpl.strong_atk, ai=mobai.pad_ai(tpl.ai),
                       elem_type=tpl.type, attri_atk=tpl.attri_atk, attri_def=tpl.attri_def,
                       walk_px_s=self.template_walk_px_s(tpl))

    @classmethod
    def template_walk_px_s(cls, tpl):
        """Desync fix P3: a template's walk speed, 1e7 / its hni speed px/s (Monster.walk_px_s);
        MOB_WALK_PX_PER_SEC when the record has none."""
        speed = int(getattr(tpl, 'speed', 0) or 0)
        return 1e7 / speed if speed > 0 else cls.MOB_WALK_PX_PER_SEC

    def _clear_map_monsters(self, session):
        """Detach the session from its map's shared monsters (map change, disconnect,
        character deleted). Sends nothing: a map load already destroyed the client entities,
        and a closed socket has no client. The monsters stay with their map: their despawn /
        respawn timers keep running for whoever is still there, and a map left empty is
        discarded after MOB_MAP_KEEP_SECS (_tick_monster_maps; 0 = at once, here)."""
        with self._combat_lock(session):
            mons = session.get('monsters')
            # First, under the combat lock alone: from here no broadcast of that map counts
            # this session (_mob_viewers checks session['monsters'] is mons).
            session['monsters'] = {}
            if not isinstance(mons, worldmod.MapMonsters):
                for mob in (mons or {}).values():          # a hand-built private dict
                    self._cancel_monster_timers(mob)
                return
            with mons.lock:
                key = self._viewer_key(session)
                if mons.viewers.get(key) is session:
                    del mons.viewers[key]
                    mons.held.pop(key, None)
                    for mob in mons.values():
                        mob.ai_takers.discard(key)
                if not mons.viewers and float(self.config.get('MOB_MAP_KEEP_SECS', 300.0)) <= 0:
                    self._discard_map_monsters(mons, 'the last player left (MOB_MAP_KEEP_SECS 0)')

    def _discard_map_monsters(self, mons, why):
        """Forget a map's monsters and cancel their timers (caller holds mons.lock); the next
        arrival rebuilds them fresh with the same uids. Only for a map with no viewer."""
        for mob in mons.values():
            self._cancel_monster_timers(mob)
        n = len(mons)
        mons.clear()
        mons.held.clear()
        mons.populated = False
        if n:
            log.info(f'[MOB] map {mons.map_code}: {n} monster(s) discarded ({why})')

    def _tick_monster_maps(self, now=None):
        """'monster-maps' ticker (MOB_MAP_GC_SECS, world lock held): discard the monsters of
        every map nobody has stood on for MOB_MAP_KEEP_SECS (world F6 step 5). Returns the
        number of maps discarded (tests drive it with an explicit clock)."""
        now = time.monotonic() if now is None else now
        keep = float(self.config.get('MOB_MAP_KEEP_SECS', 300.0))
        done = 0
        for inst in self.world.instances():
            mons = inst.monsters
            with mons.lock:
                empty = inst.empty_since
                if not mons.populated or mons.viewers or empty is None or now - empty < keep:
                    continue
                self._discard_map_monsters(mons, f'nobody on the map for {now - empty:.0f} s')
                done += 1
        return done

    @staticmethod
    def _cancel_monster_timers(mob):
        for t in mob.timers:
            t.cancel()
        mob.timers = []

    def _compute_damage(self, session, skill_id, mob):
        """A basic swing (skill_id 0: the victim event 7, Weak_Atk) or a strong attack (non-zero:
        event 9, Strong_Atk, no skill term) of the session's player on `mob`.

        DAMAGE_FORMULA 'client' (default): the client's own number (damage.py; FUN_0041b830's
        derived stats through FUN_004194f0), capped at the mob's HP. Live 2026-09-24 the
        placeholder had a Lv14 Berserker (STR 12, Wooden Blade) do 1 to a Monkey Soldier while
        his client drew 7-10: the formula gives 8 (x the client patch's grade roll on the digit).

        'placeholder': STR + the W_Att of every equipped item (x1.5 for a strong attack) minus
        the monster's Def, never below 1 (combat_skill design 4.2).

        Either way the value goes through the server's own grade roll when it is on (config
        DAMAGE_GRADE_ROLL, _grade_roller; by default the 2009 build only): the same table as
        the combo HUD v2 digit's, an independent draw."""
        if self._client_damage():
            ev = dmgmod.EV_STRONG if skill_id else dmgmod.EV_WEAK
            return self._formula_hit(session, mob, ev).total
        atk = combat.attack_power(self._session_char(session) or {})
        raw = int(atk * (1.5 if skill_id else 1.0))
        return self._graded(max(1, raw - int(mob.defense)), 'strong attack' if skill_id else 'swing')

    def _resolve_hit(self, sock, session, skill_id, mob_uid, no_enc=True, ack=False):
        """Apply one hit by `session`'s player to `mob_uid`: the one monster his client named
        (party-dep-combat-multiclient - the damage, and a kill's exp and drop, are this
        session's, whoever else stands on the map). `ack`: the hit came from the client's own
        report, so a surviving mob gets the 0x2A release and the other clients the hit relay
        (_release_hit_lock); the kill's 0x29 releases it by itself."""
        with self._combat(session):
            mons = session.get('monsters') or {}
            mob = mons.get(mob_uid)
            # client sends the FULL uid (MOB_UID_BASE+i); it may arrive truncated to
            # the low 16 bits, so match on either form.
            if mob is None:
                for m in mons.values():
                    if (m.uid & 0xFFFF) == (mob_uid & 0xFFFF):
                        mob = m; break
            if mob is None or not mob.alive:
                return
            # The flinch, damage number and hit sound are the client's own (its local hit
            # detection on an action-8 mob); the server only answers the report.
            # ack: the 0x2A release takes the survivor over; a driver swing (no report) set no
            # client hit-lock, so the monster AI takes it over itself (_mob_aggro).
            killed = self._damage_monster(sock, session, mob,
                                          self._compute_damage(session, skill_id, mob), 'basic',
                                          take_control=not ack)
            if ack and not killed:
                self._release_hit_lock(sock, session, mob, HIT_REPORT_EVENT)

    def _kill_monster(self, sock, session, mob, no_enc=True, *, mons=None):
        """Death of a live monster (combat_skill F6). The alive check and the flip share one
        critical section with the caller's damage, so this runs exactly once per life.

        world-shared-monsters: the 0x29 goes to every client that holds the monster, so the
        corpse falls on every screen. `session` is the killer: exp (0x21), quest credit, the
        loot's rights and the gold 0x18 are his alone, and only while he is on the monster's
        map (a DoT outlives its caster's stay; None: nobody is credited). The caller holds
        the killer's combat lock and the map's monster lock."""
        mons = mons if mons is not None else self._mob_home(session)
        killer = session if session is not None and (mons is None or session.get('monsters') is mons) else None
        with contextlib.ExitStack() as held:
            if killer is not None:
                held.enter_context(self._combat_lock(killer))
            if mons is not None:
                held.enter_context(mons.lock)
            if not mob.alive:
                return
            mob.alive = False
            mob.hp = 0
            mob.despawned = False
            # Monster aggro: the 0x29 below clears +0x971 and +0x96E itself (0x459B4A/0x459B51)
            # on every client, so the chase state goes without a 0x9E; alive is False in this
            # same critical section, so the AI never commands this corpse.
            mobai.clear(mob)
            who = (f' by {killer.get("char_name") or killer.get("username")!r}' if killer is not None
                   else ' (no killer on the map: nobody credited)')
            log.info(f'[KILL] {mob.name} uid={mob.uid:#x} dead{who}; exp={mob.exp} gold={mob.gold}')
            # 0x29 {uid, 0x7FFFFFFF, spawn x, y}: the corpse stays a corpse (no client
            # self-revive at (0, 0)) and its leash home stays the spawn point (S1-07).
            fields = {'uid': mob.uid, 'respawn_tick': MOB_RESPAWN_HOLD_TICK,
                      'respawn_x': int(mob.spawn_x), 'respawn_y': int(mob.spawn_y)}
            if mons is not None:
                self._mob_broadcast(mons, mob, '0x29', fields)
            elif killer is not None:
                P.send(self, sock, killer, '0x29', fields)
            # P13 boss-b1: a field boss stays down value_num * BOSS_RESPAWN_SCALE s in the
            # ledger (before the lifecycle timers below read it), whoever or nothing killed it.
            self.bosses.on_kill(mob, killer)
            if killer is None:
                if mons is not None:
                    self._schedule_monster_lifecycle(mons, mob)
                return
            session = killer
            if mob.exp:
                # party-exp-share (party.md 5, config PARTY_EXP_SHARE): the kill is the
                # killer's (the shared-monster credit above), and his party members alive on
                # this map share it. The killer's part is granted here; every other member's
                # from the tick thread (world_lock -> that member's combat lock), because
                # this holds the killer's combat and monster locks.
                shares = self.party.exp_shares(session, self._kill_exp(session, mob),
                                               mons.map_code if mons is not None else session.get('current_map'))
                gained = self.award_exp(session, shares[0][1], 'kill')
                # social_friend-mentor-exp-share: the killer's mentor gets a share (0x7F),
                # deferred to the tick thread - this holds the killer's combat and monster locks.
                self.messenger.mentee_exp(session, gained)
                for member, amount in shares[1:]:
                    self.ticks.call_later(0, self._party_exp_share, member, amount,
                                          session.get('char_name'), mob.name, name='party-exp')
            # ReqPro kill credit, after the 0x29/0x21 sends (quest doc F3 step 1). Only the
            # killer is credited; party-shared credit is undefined by the client (Q5).
            self._quest_credit_kill(sock, session, mob.npccode, no_enc)
            # P13 boss-b2 (bosses.roll_drops): DROP_MODE 'rates' rolls every hni Drop entry at
            # rate / DROP_RATE_UNIT (a boss trophy 99.99 %), 'single' one draw over the column,
            # 'legacy' the P0 roll (60 %, one entry uniformly: exactly the old random calls).
            drops = bossmod.roll_drops(mob, self.config.get('DROP_MODE', 'rates'),
                                       self.config.get('DROP_RATE_UNIT', bossmod.DEFAULT_DROP_RATE_UNIT),
                                       random)
            bagged = []
            for item in drops:
                if self.config.get('GROUND_LOOT', True):
                    # item_inventory-ground-loot-pickup (F6): the loot falls at the corpse. 0x12
                    # goes out after the 0x29, while the corpse entity still exists (its 0x06
                    # comes MOB_CORPSE_SECS later), so the client places the item on the
                    # monster's OWN position (source_uid lookup) - a roaming mob is wherever the
                    # client walked it, not at mob.x/y. owner = the killer: 15 s of loot rights
                    # (a boss trophy too: "You don't have ownership of this item." for the
                    # others, P13 exit criterion 5). Only the 0x18 below (gold) stays; the
                    # item reaches the bag on C2S 0x1F.
                    self._loot_to_ground(session, mob, item, 1)
                elif self._inv_add(session, item, 1, f'{mob.name} drop') is not None:
                    # GROUND_LOOT false: the P0 path, straight into the bag with the 0x18.
                    self._quest_credit_item(sock, session, item, 1, no_enc)
                    bagged.append(item)
            # The first bagged item rides on the gold 0x18; any further one (only 'rates' can
            # drop two) gets its own 0x18 {wallet unchanged, item} - the client adds each.
            first = bagged[0] if bagged else 0
            self._send_drop(sock, session, item=first, count=1 if first else 0, gold_gain=mob.gold,
                            no_enc=no_enc)
            for item in bagged[1:]:
                self._send_drop(sock, session, item=item, count=1, no_enc=no_enc)
            if mons is not None:
                self._schedule_monster_lifecycle(mons, mob)

    def _party_exp_share(self, member, amount, killer_name, mob_name):
        """party-exp-share, on the tick thread: a member's part of a party kill (S2C 0x21
        through grant_exp; his own mentor's 0x7F share follows as for his own kills).
        Nothing for a member that died, left the world or closed meanwhile."""
        if not member.get('in_world') or member.get('dead') or member.get('closed') or member.get('kicked'):
            log.info(f'[PARTY] {member.get("char_name")!r}: share of {killer_name!r}\'s {mob_name} kill '
                     f'dropped (no longer in world / dead)')
            return 0
        gained = self.award_exp(member, amount, 'party')
        self.messenger.mentee_exp(member, gained)
        log.info(f'[PARTY] {member.get("char_name")!r} +{gained} exp: share of {killer_name!r}\'s {mob_name} kill')
        return gained

    def _kill_exp(self, session, mob):
        """Stage 1 of arch09-exp-pipeline (award_exp), on the whole kill before the party
        split. The exp one kill grants: the template's Exp, +10% when the monster's Monster Card
        is in the killer's deck (quest_cards_misc-card-exp-bonus, F11: the card tooltip's
        "Card effect: EXP + 10%"; cards.bonus_exp has the rounding). The client adds the
        S2C 0x21 delta as sent and applies no bonus itself, so the server is the only place
        it can happen. Config CARD_EXP_BONUS false = the plain template exp (Q2: retail
        behaviour unproven)."""
        exp = int(mob.exp)
        if not self.config.get('CARD_EXP_BONUS', True):
            return exp
        total, card = cardsmod.bonus_exp(self._session_char(session), mob.npccode, exp)
        if card is not None:
            log.info(f'[CARD] {mob.name} (npccode {mob.npccode}): card {card} registered -> '
                     f'exp {exp} + {total - exp}')
        return total

    def _schedule_monster_lifecycle(self, mons, mob):
        """Corpse despawn and respawn on the tick scheduler (roadmap F8): nothing depends on
        client traffic, so kills made by the memory combat driver respawn too. The timers
        belong to the map's monsters, not to a player: they run whoever is on the map."""
        self._cancel_monster_timers(mob)
        # P13 boss-b1: a field boss comes back when the ledger says (value_num *
        # BOSS_RESPAWN_SCALE s after the kill), every other monster after MOB_RESPAWN_SECS.
        respawn = self.bosses.respawn_delay(mob)
        respawn = MOB_RESPAWN_SECS if respawn is None else respawn
        mob.timers = [
            self.ticks.call_later(MOB_CORPSE_SECS, self._despawn_monster, mons, mob,
                                  name=f'mob-despawn-{mob.uid:#x}'),
            self.ticks.call_later(respawn, self._respawn_monster, mons, mob,
                                  name=f'mob-respawn-{mob.uid:#x}'),
        ]

    @staticmethod
    def _mob_is_current(mons, mob):
        """The timer's monster is still a live entry of its map (not discarded since)."""
        return mons.populated and mons.get(mob.uid) is mob

    def _send_monster_despawn(self, mons, mob):
        """S2C 0x06 {uid} to every client holding the monster: FUN_00422b90 unlinks and frees
        the entity (spec 0x06). Never to a receiver whose own uid it is, which would leave
        scene+0x970 dangling (T-06-3). Afterwards no client holds it. Caller holds mons.lock.
        Returns the number sent."""
        sent = 0
        for s in self._mob_viewers(mons, mob):
            if mob.uid == P.session_uid(s):
                log.error(f'[MOB] refusing 0x06 for uid {mob.uid:#x}: it is the receiver\'s local player')
                continue
            sent += bool(self._push(s, '0x06', {'uid': mob.uid}, 'MOB'))
        for held in mons.held.values():
            held.discard(mob.uid)
        mob.despawned = True
        return sent

    def _despawn_monster(self, mons, mob):
        """Tick callback, MOB_CORPSE_SECS after death: remove the corpse (0x06) everywhere."""
        with mons.lock:
            if mob.alive or mob.despawned or not self._mob_is_current(mons, mob):
                return
            n = self._send_monster_despawn(mons, mob)
            log.info(f'[MOB] corpse {mob.name} uid={mob.uid:#x} despawned (0x06 to {n} client(s))')

    def _respawn_monster(self, mons, mob):
        """Tick callback, MOB_RESPAWN_SECS after death: a fresh monster with the same uid at
        its spawn point, sent ONCE to every client on the map (0x1A). The client never
        de-duplicates 0x1A, so a corpse still held anywhere is removed first (S1-07)."""
        with mons.lock:
            if mob.alive or not self._mob_is_current(mons, mob):
                return
            if not mob.despawned:
                self._send_monster_despawn(mons, mob)
            mob.hp, mob.alive, mob.despawned = mob.max_hp, True, False
            mob.x, mob.y = mob.spawn_x, mob.spawn_y
            # cs-debuffs: the fresh 0x1A is a new client entity with no slots; nor has it an
            # aggro target, and the 0x1A resets +0x971 (0x457081): the client wander runs it.
            debuffmod.clear(mob)
            mobai.clear(mob)
            self._cancel_monster_timers(mob)
            self.bosses.on_respawn(mob)                  # P13 boss-b1: the ledger has it up
            sent = 0
            for s in self._mob_viewers(mons):
                key = self._viewer_key(s)
                if mob.uid in mons.held.get(key, ()):
                    continue
                try:
                    self._send_monster_spawn(s['sock'], s, mob)
                except OSError as e:
                    log.info(f'[MOB] respawn uid={mob.uid:#x} not sent to {s.get("char_name")!r}: {e}')
                    continue
                mons.held.setdefault(key, set()).add(mob.uid)
                sent += 1
            log.info(f'[MOB] respawn {mob.name} uid={mob.uid:#x} at ({mob.x:g},{mob.y:g}) hp={mob.hp} '
                     f'(0x1A to {sent} client(s))')

    # ========================================================================
    # MONSTER AGGRO (re_tools/docs/MONSTER_AGGRO_RE_2026-09-23.md; the rules are mobai.py).
    #
    # The field client never gives a monster a target: both writers of +0xE44 [+0xDB4] sit
    # behind the host flag scene+0xF40 [+0xF24] (gates 0x41A6C8/0x41A70E, 0x41927A), which is
    # only ever stored as 0 (0x4128B3 [0x411FC3]). Retail ran that engine on the SERVER: the
    # hate list on every surviving hit, a chase decision about every 990 ms shipped as a
    # command node (+0x971 = 1: the client replays it), and S2C 0x9E to hand the mob back to
    # the client wander. Here:
    #   - _mob_aggro: every surviving hit (the reported swing, skills, DoT, traps) feeds the
    #     hate list and turns the mob on its attacker (0x41A737..0x41A815);
    #   - _tick_monster_ai ('monster-ai', MOB_AI_TICK_SECS): each chasing mob's decision -
    #     face / walk / jump / drop / attack / dash with the client's own constants - as the
    #     16-byte 0x2A self-form (MOB_AI_COMMAND '2A') re-sent on a change or every
    #     MOB_CMD_KEEPALIVE_SECS under its 960 ms hold, or as 0x1B nodes ('1B');
    #   - _mob_hand_back: 0x9E on the target's death or departure, past the leash, or after
    #     the timeout; a kill needs none (0x29 clears +0x971 itself).
    #
    # Shared monsters (world-shared-monsters, P5 stage 3):
    #   - the hate list spans every player on the map (_aggro_target finds any of them), and
    #     the 2009 AI[5] scan picks the nearest player in its box;
    #   - every command word and every hand-back goes to EVERY client holding the mob, the
    #     0x2A self-form built per receiver (its last u32 is the receiver's own uid), so the
    #     mob moves the same way on every screen. The user's aggro-rules exe patches key two
    #     rules on +0x971 (RETAIL_COMBAT_FEEL 2.1 / 2.2, open question 4): the red name box
    #     and body contact. So a chased mob is red on every client - retail showed an aggroed
    #     mob red to all - and each client's player can touch it; the server still hurts only
    #     a player the mob is after (MOB_CONTACT_AGGRO_ONLY checks the hate list);
    #   - a client that arrives mid-chase, or whose copy the client wander still runs, gets
    #     the current word at once (Monster.ai_takers), without resetting the keep-alive;
    #   - position authority (decision G1 for monsters, _mob_fix_allowed): the chase target's
    #     client, plus dead reckoning in between.
    # Everything runs under the map's monster lock and checks mob.alive in the same critical
    # section as the kill: no command ever follows a 0x29.
    # ========================================================================
    MOB_1B_HOLD_MS = 300          # the 0x1B fallback node (spec errata C29: the walk stops at hold end)
    MOB_1B_LEAD_SECS = 0.03       # send the next 0x1B once the last has <= one 30 ms tick left (C3)
    # live (C29): a commanded Pupu walks 82.5 px/s - every monster's speed with
    # MOB_SPEED_FROM_TEMPLATE off, and a template's with no hni speed (desync fix P3)
    MOB_WALK_PX_PER_SEC = 82.5
    MOB_DASH_FACTOR = 2.7         # state 6 moves 2.7 x the walk step (0x416403)
    MOB_GIVE_UP_PX = 400.0        # the aggro timeout only lets go of a target this far away
    MOB_ATTACK_EVENT_SECS = 1.5   # a swing event needs an attack of its kind commanded this recent
    # Timer jitter the keep-alive allows for: on the 0.3 s tick a re-send due at 0.6 s must not
    # slip to the 0.9 s tick because that tick came a few ms early (the node holds 960 ms).
    MOB_AI_JITTER_SECS = 0.05
    # The hit relay (_relay_hit): the victim's hit event in the node's action nibble (lo bits
    # 12-15 -> +0x9DC via the consumer 0x4129F0: 7 = hit, as the attacker's own client caught
    # it) and the 0x1B form's hold while no server AI drives the watchers' copy.
    MOB_HIT_RELAY_EVENT = 7
    MOB_HIT_RELAY_HOLD_MS = 300

    def _mob_ai_enabled(self):
        return bool(self.config.get('MOB_AGGRO'))

    @staticmethod
    def _mob_rng_bit():
        """The chase branch's attack A/B pick bit (+0x9F0[+0x948] & 1, 0x4190B4)."""
        return random.getrandbits(1)

    def _mob_command(self, mons, mob, lo, now=None, *, form=None, only=None, touch=True):
        """Command `mob` with input word `lo` (mobai.lo: direction | motion << 2; hi 0, bits
        12-19 never set) on every client that holds it - the chase moves the same way on every
        screen - or on `only` (a list of sessions). form (default config MOB_AI_COMMAND):

        - '2A': S2C 0x2A {mob, lo, 0, target = the RECEIVER's own uid}, 16 B, built per
          receiver (walk right to uid 1: 00 00 0F 00 02 00 00 00 00 00 00 00 01 00 00 00).
          Handler 0x459C3D [0x454E6D] sets +0x971 = 1 (0x459C89), flushes the queue (so it acts
          on the next tick and never builds a backlog), releases the hit-lock (target ==
          local uid, 0x459F04 [0x455136]) keeping the node's direction and motion, and holds
          it 960 ms. Under the aggro-rules exe patches the same +0x971 draws the red name box
          and allows body contact on that client (RETAIL_COMBAT_FEEL 2.1 / 2.2).
        - '1B': S2C 0x1B {mob, hold 300, lo, 0} (0x45712B [0x4523F9]) only appends a node and
          never touches +0x971 - the fallback if 0x2A turns out to carry no motion.
        touch=False only sends: the mob's command clock (word, keep-alive, hold end) stays
        as it is - a top-up for clients that joined since the last command. Caller holds
        mons.lock. Returns the number of packets sent."""
        now = time.monotonic() if now is None else now
        if touch:
            self._mob_dead_reckon(mob, now)
        lo = int(lo) & mobai.LO_MASK
        blob = struct.pack('<II', lo, 0)
        form = form or self.config.get('MOB_AI_COMMAND', '2A')
        receivers = self._mob_viewers(mons, mob) if only is None else list(only)
        if form == '1B':
            sent = self._mob_broadcast(mons, mob, '0x1B', {'uid': mob.uid, 'hold_ms': self.MOB_1B_HOLD_MS,
                                                           'state_blob': blob}, receivers=receivers)
            if touch:
                # queued behind what is left of the last node (the consumer pops it at hold 0)
                mob.ai_hold_end = max(now, mob.ai_hold_end) + self.MOB_1B_HOLD_MS / 1000.0
        else:
            sent = self._mob_broadcast(mons, mob, '0x2A', lambda s: {
                'mover_uid': mob.uid, 'move_bits': blob, 'target_uid': P.session_uid(s) or 0},
                receivers=receivers)
            mob.ai_takers.update(self._viewer_key(s) for s in receivers)
            mob.ai_owned = True
            if touch:
                # An idle node's hold is zeroed at once by the consumer's idle check
                # (0x412ABC..0x412AF6), so it never holds back a queued 0x1B.
                mob.ai_hold_end = now + (mobai.HOLD_2A_SECS if lo != mobai.STOP else 0.0)
        if not touch:
            return sent
        if only is None:
            self._mob_word_after_stun(mob, now)
        if lo != mob.ai_lo and self._mob_ai_enabled():
            log.info(f'[AGGRO] {mob.name} uid={mob.uid:#x} -> {mobai.describe(lo)} (0x{form} lo {lo:02X} '
                     f'to {sent} client(s))')
        if mobai.is_attack(lo) and lo != mob.ai_lo:
            # A new swing (the attack starts, turns or changes kind) - never a keep-alive: the
            # age of the attack hold (MOB_ATTACK_HOLD_SECS, _mob_ai_step_in) counts from here.
            mob.ai_attack_start_t = now
        mob.ai_lo, mob.ai_sent_t = lo, now
        if mobai.is_attack(lo):
            # Every send, the keep-alive included: each 0x2A flushes the client queue and pops
            # the attack motion back into +0x940 (MONSTER_AGGRO_RE 7), so a mob parked in its
            # attack is still being commanded to swing - and the swing filter
            # (_monster_contact, MOB_ATTACK_EVENT_SECS) must keep taking the events of the
            # commanded kind: note_command refreshes ai_attack_a_t (7/8) or ai_attack_b_t
            # (4/5/9/10, P13 boss-b3), which is what bosses.swing_age reads. ai_attack_t (either
            # kind) gates nothing (Monster.ai_attack_t).
            mob.ai_attack_t = now
            bossmod.note_command(mob, lo, now)
        return sent

    def _mob_hand_back(self, mons, mob, why):
        """S2C 0x9E {mob, 8 zero bytes} (12 B, e.g. 00 00 0F 00 + 8 x 00) to every client
        holding the mob: +0x971 = 0 at receipt (0x459C89) and a one-tick node; after it each
        client's own wander AI runs the mob again and walks it back inside its 600 px home
        leash (and the patched clients draw its box black again). The aggro state goes.
        Caller holds mons.lock. Returns the number sent."""
        sent = self._mob_broadcast(mons, mob, '0x9E', {'mover_uid': mob.uid, 'move_bits': bytes(8)})
        log.info(f'[AGGRO] {mob.name} uid={mob.uid:#x} handed back to the client wander (0x9E to '
                 f'{sent} client(s)): {why}')
        mobai.clear(mob)
        return sent

    def _aggro_target(self, map_code, uid, hint=None):
        """The session of aggro target `uid` while it can be chased - online, in the world on
        the monster's map and alive (FUN_00419450 finds it, not in state 0x10/0x16) - else
        None. Any player on the map qualifies: the hate list spans all of them (world-shared-
        monsters). `hint`: a session that may be that player (one not in the online index,
        e.g. a hand-built test session). The index is read without the world lock: the caller
        holds a monster lock, which comes after it (a plain dict get is atomic)."""
        if not uid:
            return None
        target = hint if hint is not None and P.session_uid(hint) == uid else self.world.by_uid.get(uid)
        if (target is None or not target.get('in_world') or target.get('dead')
                or target.get('sock') is None or not target.get('pos')
                or target.get('current_map') != map_code):
            return None
        return target

    def _mob_aggro(self, sock, session, mob, attacker, dmg, now=None, *, take_control=True, mons=None):
        """A surviving hit on `mob` by `attacker` (monster aggro): the hate list and target rule
        of FUN_004194f0 0x41A737..0x41A815 (mobai.add_hate) - the hate list spans every player
        on the map. take_control: the hit set no client hit-lock (a skill, DoT, detonation,
        the dev !trap, a driver swing), so nothing released the mob and the server takes it
        over now with its first decision; after a client-caught hit the caller's 0x2A release
        does that. A stationary template (AI[9]) never chases (0x418C86) and is not aggroed.
        `session` is the hitting player's (None for a DoT whose caster left); `mons` the
        mob's map (default: the session's). Returns True when the mob now chases
        `attacker`."""
        mons = mons if mons is not None else self._mob_home(session)
        if (not self._mob_ai_enabled() or mons is None or not mob.alive or not attacker
                or mob.flags.stationary):
            return False
        with mons.lock:
            now = time.monotonic() if now is None else now
            code = mons.map_code
            first = not mob.aggro_uid
            if mobai.add_hate(mob, attacker, dmg, lambda uid: self._aggro_target(code, uid, session) is not None):
                target = self._aggro_target(code, mob.aggro_uid, session)
                if first and target is not None:
                    mob.ai_dir = mobai.facing_toward(mob.x, target['pos'][0])
                log.info(f'[AGGRO] {mob.name} uid={mob.uid:#x} turns on uid {mob.aggro_uid:#x} '
                         f'(hate {mob.hate})')
            mob.ai_hit_t = now
            if take_control and not mob.ai_owned:
                self._mob_ai_step_in(mons, mob, now)
            return mob.aggro_uid == attacker

    def _release_aggro(self, session, uid, why):
        """Hand back (0x9E, to every client on the map) every live monster of `session`'s map
        chasing `uid` - his death (_player_death). Caller holds the session's combat lock.
        Returns the number handed back."""
        mons = self._mob_home(session)
        if mons is None or not uid:
            return 0
        done = 0
        with mons.lock:
            for mob in list(mons.values()):
                if mob.alive and mob.aggro_uid == uid:
                    self._mob_hand_back(mons, mob, why)
                    done += 1
        return done

    def _mob_walk_px_s(self, mob):
        """The walk speed the server reckons `mob` with: its template's (Monster.walk_px_s)
        with MOB_SPEED_FROM_TEMPLATE on (desync fix P3), else MOB_WALK_PX_PER_SEC."""
        if self.config.get('MOB_SPEED_FROM_TEMPLATE', True):
            return float(getattr(mob, 'walk_px_s', 0.0) or self.MOB_WALK_PX_PER_SEC)
        return self.MOB_WALK_PX_PER_SEC

    def _mob_dead_reckon(self, mob, now):
        """Advance a commanded monster's x at its walk speed (_mob_walk_px_s; x 2.7 for a dash
        with MOB_SPEED_FROM_TEMPLATE on) while its last command moves it sideways and that
        node's hold runs, from the last step or position fix (driver sample, 0x0D interact
        tail, a hit's knockback) - nothing else reports where a commanded mob walked when the
        dev driver is not attached - and never before the copies' hurt ends (ai_stun_until:
        an ice hit's chase word is queued inside the stun). y is left alone (jump / drop
        arcs are not modelled). Returns the distance moved."""
        start = max(mob.ai_step_t, mob.fix_t, mob.ai_sent_t, getattr(mob, 'ai_stun_until', 0.0))
        mob.ai_step_t = max(mob.ai_step_t, now)
        lo = mob.ai_lo
        if (lo < 0 or not mob.ai_owned or mobai.motion_of(lo) not in mobai.MOVING_MOTIONS
                or mobai.direction_of(lo) not in (mobai.DIR_LEFT, mobai.DIR_RIGHT)):
            return 0.0
        dt = min(now, mob.ai_hold_end) - start
        if dt <= 0:
            return 0.0
        speed = self._mob_walk_px_s(mob)
        if mobai.motion_of(lo) == mobai.MOTION_SKILL and self.config.get('MOB_SPEED_FROM_TEMPLATE', True):
            speed *= self.MOB_DASH_FACTOR
        dx = speed * dt * (1 if mobai.direction_of(lo) == mobai.DIR_RIGHT else -1)
        mob.x += dx
        return abs(dx)

    @staticmethod
    def _mob_fix_allowed(mob, reporter_uid):
        """May a position report from `reporter_uid`'s client move the server's x/y of `mob`?
        (world-shared-monsters; decision G1 for monsters, LIVE_TEST_LOG P5 stage 3.)

        Each client runs its own copy of a monster: while it wanders, every client's wander
        AI walks its copy somewhere else (retail too: 0x1A server_controlled 0); once the
        server chases, every client replays the same command words from wherever its copy
        stood. So there is no single truth, and the server picks one:
        - a mob chasing a player: THAT player's client is the authority - his hit reports'
          and trap catches' interact tails, and the dev driver's memory sample when it reads
          his client. The chase decisions compare the mob with him, the contact damage is
          his, and his client is the one that sees the bumps; reports from other clients
          (their copies may lag or lead) would make the decisions flip between two copies.
          Between fixes the server dead-reckons the commanded walk (_mob_dead_reckon).
        - a mob chasing nobody: any client's report is a fix (the first hit makes its author
          the target anyway).
        The one exception is the hit relay: every surviving client-caught hit re-syncs the
        OTHER clients' copies to the HITTER's tail (_relay_hit's full 0x2A, _hitter_point)
        and moves the server's point there, target or not - his copy stands there and is
        knocked from there, so after the relay every copy starts from it."""
        return not mob.aggro_uid or mob.aggro_uid == reporter_uid

    def _mob_give_up(self, mons, mob, target, now):
        """Why a chasing monster lets go, or None: the target is dead, gone or on another map;
        the mob chased more than MOB_LEASH_PX from where the chase began; or
        MOB_AGGRO_TIMEOUT_SECS passed with no hit while the target is more than MOB_GIVE_UP_PX
        away. The client chase itself has neither leash nor timeout. The leash counts from the
        chase's start, not the spawn: the client wander already roams up to 600 px from home
        (@0x52EFC0), so a spawn-based 600 px leash let a mob hit near that edge give up after a
        few steps (live 2009: released 4 s into its first chase)."""
        if target is None:
            return f'target uid {mob.aggro_uid:#x} is dead, gone or on another map'
        leash = float(self.config.MOB_LEASH_PX)
        if abs(mob.x - mob.aggro_x) > leash:
            return f'chased {abs(mob.x - mob.aggro_x):.0f} px from where it was hit (leash {leash:g})'
        dist = combat.distance((mob.x, mob.y), target['pos'])
        idle = now - mob.ai_hit_t
        if idle >= float(self.config.MOB_AGGRO_TIMEOUT_SECS) and dist > self.MOB_GIVE_UP_PX:
            return f'no hit for {idle:.1f} s and {dist:.0f} px from its target'
        return None

    def _mob_ai_step(self, sock, session, mob, now):
        """One decision for a chasing monster of `session`'s map (_mob_ai_step_in). Returns
        the packets sent."""
        mons = self._mob_home(session)
        if mons is None:
            return 0
        with mons.lock:
            return self._mob_ai_step_in(mons, mob, now)

    def _mob_ai_step_in(self, mons, mob, now):
        """One decision for a chasing monster (caller holds mons.lock): hand it back if it
        gives up (_mob_give_up), else the chase branch's input (mobai.decide; STOP while a
        control effect runs) and its command (_mob_send_decision). Returns the packets
        sent."""
        if not mob.alive or not mob.aggro_uid:
            return 0
        self._mob_dead_reckon(mob, now)
        target = self._aggro_target(mons.map_code, mob.aggro_uid)
        why = self._mob_give_up(mons, mob, target, now)
        if why:
            return self._mob_hand_back(mons, mob, why)
        if debuffmod.controlled(mob, now):
            word = mobai.STOP                      # a stunned mob stands: 0x2A 00
        else:
            prev = mobai.motion_of(mob.ai_lo) if mob.ai_lo is not None and mob.ai_lo > 0 else None
            # hold_x: an attack under way holds until the target is clearly out of reach, so
            # the word does not flip walk / attack every tick at the reach edge (P7 live L2) -
            # for MOB_ATTACK_HOLD_SECS after the swing began only: a commanded attack never
            # moves the mob, so an endless hold parked it swinging at a target standing just
            # beyond reach. Past that the plain box decides and the mob walks in again. Attack
            # A and attack B alike (mobai.ATTACK_MOTIONS): a boss's B swing holds the same way.
            swing = (now - mob.ai_attack_start_t
                     if prev in mobai.ATTACK_MOTIONS and mob.ai_attack_start_t else None)
            # P13 boss-b3: attack B (AI[8]) and the dash (AI[3]) only where config MOB_ATTACK_B /
            # MOB_DASH allow them (not live-verified yet, evb B10); attack A always.
            d = mobai.decide((mob.x, mob.y), target['pos'], mob.ai_dir,
                             bossmod.command_flags(mob, self.config),
                             rng_bit=self._mob_rng_bit(), prev_motion=prev,
                             reach_x=float(self.config.MOB_ATTACK_REACH_X),
                             reach_y=float(self.config.MOB_ATTACK_REACH_Y),
                             hold_x=float(self.config.MOB_ATTACK_HOLD_X),
                             swing_secs=swing, hold_secs=float(self.config.MOB_ATTACK_HOLD_SECS))
            mob.ai_dir = d.direction
            word = mobai.lo(d.direction, d.motion)
        return self._mob_send_decision(mons, mob, word, now)

    def _mob_send_decision(self, mons, mob, word, now):
        """Ship a decision to every client holding the mob (caller holds mons.lock). Returns
        the packets sent.

        '2A': when the word changed, or every MOB_CMD_KEEPALIVE_SECS while it moves the mob -
        each re-send restarts the 960 ms hold before it runs out, so a chasing mob never
        stops. A chasing mob never gets 00: an idle node's hold is zeroed (0x412ABC..0x412AF6)
        and the tick gate (+0xEE4 && +0x971) || +0x96D (0x4142B8, 0x41602B, 0x416B50) then stops
        its state machine, movement and contact detection. A client that has not had a 0x2A
        for this mob since it got the mob (it arrived mid-chase; or only the attacker got the
        hit's release) gets the current word at once, without restarting the others' clock.
        '1B': a node only when the last one has <= MOB_1B_LEAD_SECS left (no backlog, C3; no
        stop on expiry, C29); a client whose copy the client wander still runs gets the 0x2A
        take-over first, because a 0x1B on +0x971 = 0 freezes it for the hold.
        Both forms (desync fix M1, MOB_HIT_RECOVER_SECS): until mob.ai_recover_until - the
        hurt of a client-caught swing or skill hit (events 7 / 9, _release_hit_lock), or the
        action of an attack skill the server's box landed (MOB_HIT_CAST_GATE, _skill_attack) -
        no word goes out, only the STOP top-up (touch=False) to a client that has had no 0x2A
        for this mob; then the chase word reaches every client in the same tick."""
        sent = 0
        fresh = [s for s in self._mob_viewers(mons, mob) if self._viewer_key(s) not in mob.ai_takers]
        if now < mob.ai_recover_until:
            if fresh:
                return self._mob_command(mons, mob, mobai.STOP, now, form='2A', only=fresh, touch=False)
            keepalive = float(self.config.MOB_CMD_KEEPALIVE_SECS) - self.MOB_AI_JITTER_SECS
            if (now < getattr(mob, 'ai_ice_until', 0.0) and mob.ai_recover_until > getattr(mob, 'ai_ice_chase_t', 0.0)
                    and mob.ai_owned and mob.ai_lo is not None and mob.ai_lo >= mobai.STOP
                    and now - mob.ai_sent_t >= keepalive and self.config.get('MOB_AI_COMMAND', '2A') == '2A'):
                # An ice hit's stun under a gate someone's later cast armed: the word every copy
                # already holds (the release's STOP, or the chase word queued inside the stun)
                # again - it cannot cut the stun short (state 3 is not idle, 0x412ABC keeps the
                # hold) and without it the 960 ms node hold runs out and their +0xE9C stalls.
                return self._mob_command(mons, mob, mob.ai_lo, now, form='2A')
            return 0
        if self.config.get('MOB_AI_COMMAND', '2A') == '1B':
            if fresh or not mob.ai_owned:
                sent += self._mob_command(mons, mob, mobai.STOP, now, form='2A', only=fresh,
                                          touch=not mob.ai_owned)
            if now < mob.ai_hold_end - self.MOB_1B_LEAD_SECS:
                return sent
            return sent + self._mob_command(mons, mob, word, now, form='1B')
        keepalive = float(self.config.MOB_CMD_KEEPALIVE_SECS) - self.MOB_AI_JITTER_SECS
        if (mob.ai_owned and word == mob.ai_lo and (word == mobai.STOP or now - mob.ai_sent_t < keepalive)
                and not self._ice_word_due(mob, now, keepalive)):
            if fresh:
                return self._mob_command(mons, mob, word, now, form='2A', only=fresh, touch=False)
            return 0
        return self._mob_command(mons, mob, word, now, form='2A')

    def _tick_monster_ai(self, now=None):
        """'monster-ai' ticker (MOB_AI_TICK_SECS on the tick scheduler, world lock held): the
        decisions of every chasing monster of every map, the 2009 proximity aggro, and the
        hand-back of a mob the server owns but does not chase. Each map runs under its own
        monster lock. Returns the packets sent (tests drive it with an explicit clock)."""
        if not self._mob_ai_enabled():
            return 0
        now = time.monotonic() if now is None else now
        sent = 0
        for mons in self.world.monster_maps():
            try:
                with mons.lock:
                    if mons.populated:
                        sent += self._run_monster_ai(mons, now)
            except OSError as e:
                log.debug(f'[AGGRO] map {mons.map_code}: send failed ({e})')
        return sent

    def _run_monster_ai(self, mons, now):
        """One map's monster AI pass (caller holds mons.lock)."""
        players = None
        sent = 0
        for mob in list(mons.values()):
            if not mob.alive:
                continue
            if (not mob.aggro_uid and self.client_build == cfgmod.BUILD_2009
                    and mob.flags.proximity and not mob.flags.stationary):
                # The 2009 AI[5] scan (0x41927A..0x41930C): a player within 200 x 200 px becomes
                # the target with no hit (Slow Peach) - any player on the map, the nearest first.
                if players is None:
                    players = [s for s in self.world.map_sessions(mons.map_code)
                               if self._aggro_target(mons.map_code, P.session_uid(s), s) is not None]
                near = [s for s in players if mobai.in_proximity((mob.x, mob.y), s['pos'])]
                if near:
                    victim = min(near, key=lambda s: combat.distance((mob.x, mob.y), s['pos']))
                    me = P.session_uid(victim)
                    mob.aggro_uid, mob.ai_hit_t, mob.aggro_x = me, now, mob.x
                    mob.ai_dir = mobai.facing_toward(mob.x, victim['pos'][0])
                    log.info(f'[AGGRO] {mob.name} uid={mob.uid:#x} turns on uid {me:#x} (AI[5] proximity)')
            if mob.aggro_uid:
                sent += self._mob_ai_step_in(mons, mob, now)
            elif mob.ai_owned and now - mob.ai_sent_t >= mobai.HOLD_2A_SECS:
                # A mob the 0x2A release took over that has nothing to chase (a stationary
                # template) would stand frozen for good (Gate B): once its node's hold ran out
                # (the flinch played), the client wander gets it back.
                sent += self._mob_hand_back(mons, mob, 'no target to chase')
        return sent

    def award_exp(self, session, amount, source, *, menti_id=None):
        """arch09-exp-pipeline (P13 skeleton; ROADMAP_2009_ADDENDUM 3.0, evb A6/E2, X9): THE
        path of every EARNED exp grant - a kill ('kill'), a party member's share of one
        ('party'), a quest ('quest') and a mentor's share of a mentee's kill ('mentor'). Later
        writers (boss kills, the P16 dungeon party share) call it too and never send 0x21
        themselves. The stages, in this order:
          1. card bonus: +10% for a registered Monster Card, applied by _kill_exp to the WHOLE
             kill before the party split (kills only, the killer's deck);
          2. cash EXP item (P8 premium_cash-use-generic): cashuse.CashUse.boost_exp - the
             RECEIVER's best active +50% / +100% period item (they do not stack), for
             cashuse.EXP_BOOST_SOURCES ('kill', 'party') only: the killer's part is raised
             by his own item, each member's share by that member's. No item: unchanged;
          3. event multiplier: events.Events.scale_exp - int(x * mult), at least 1, per
             receiver; never for 'mentor' (a percentage of the mentee's already scaled exp),
             for 'quest' only with EVENT_EXP_QUESTS. No event: the amount is unchanged;
          4. S2C 0x21 (0x7F with menti_id) through grant_exp, which persists and levels.
        Nothing else raises earned exp: a P8 cash grant, a mall purchase or a mileage credit
        carries none, and every writer of an earned 0x21 comes through here.
        P14 guild tail hook: the 2009 0x21 carries a u32 guild_points exactly when the
        RECEIVER's client-side guild id is >= 2 (_exp_delta_packet reads _receiver_guild_id,
        0 until P14, so the tail is never sent yet). Guild points come from the PRE-multiplier
        exp by default (X9): that amount travels to the builder as `guild_base` for P14 to
        credit; 0x7F never has the tail. GM !exp / !level and the death penalty are not earned
        grants: they call grant_exp directly and are never scaled (nor is the 1891 Waive EXP
        Penalty item a stage: it only skips the death penalty). Returns the exp applied."""
        scaled = int(amount)
        cashuse = getattr(self, 'cashuse', None)        # a bare GameServer in unit tests
        if cashuse is not None and source in cashusemod.EXP_BOOST_SOURCES:
            scaled = cashuse.boost_exp(session, scaled)
        events = getattr(self, 'events', None)
        if events is not None:
            scaled = events.scale_exp(scaled, source)
        return self.grant_exp(session, scaled, menti_id=menti_id, guild_base=int(amount))

    def grant_exp(self, session, exp_delta, *, menti_id=None, guild_base=None):
        """Credit (or take) exp and let the client level itself (lc-exp-persist, F6).
        Returns the exp actually applied. Earned exp comes through award_exp (the
        arch09-exp-pipeline); `guild_base` is its pre-multiplier amount, for P14's 0x21 tail.

        menti_id (social_friend-mentor-exp-share / lc-menti-exp): the credit is a mentor's
        share of that mentee's kill. The owner then gets S2C 0x7F {delta, menti_id} instead
        of 0x21 - the same client path (0x21/0x7F share handler 0x4542C4: exp, level-up,
        message) plus "(Menti[name])" when the uid is in his mentee list.

        - `char['exp']` in the store is the only exp total (the old session['exp'] default
          restarted at 0 every login, B3), clamped to the client's range [0, EXP_MAX].
        - The owner gets **S2C 0x21 only**. The client adds the delta to scene+0x278,
          prints the line and, when it crosses a threshold of its own table, plays the
          level-up effect once and recomputes level/max HP/MP itself (login_character.md
          1.7). The extra owner 0x22 the server used to send played that effect a second
          time, at the server's own (wrong) threshold: S2-43 / B1 + B4.
        - The other clients on the map that hold the player get **S2C 0x22 {uid, new level}**
          (lc-level-broadcast, P5 stage 4; login_character.md F6 step 3), never the owner:
          it sets their copy's level (entity+0x99, 2009 +0x9D: what their Char. Info shows)
          with the level-up effect and sound on it, and heals their copy to its new maxima
          as the owner's client healed itself (spec 0x22, both builds wire-identical). Only a
          level-UP is sent: 0x22 plays the level-up effect and sound on every receipt, even
          without a change (spec 0x22 gates_and_hazards). A level-down (only GM
          `!exp`/`!level`; the death penalty never crosses a level, combat.death_penalty)
          re-sends the player's record instead (presence.reshow_to_holders: 0x06, then a
          0x05 whose level is the lower one, records.level_of) - the observers kept the old
          level before (livetest bug 8). The owner's own client lowers its level from the
          negative 0x21 itself (live: !level 14 from 15 showed 14).
        - A level-up mirrors the client's full heal so the next 0x28/0x44 and the next 0x07
          agree with what the player sees: HP/MP go to the new level's maxima (hpmp.refresh,
          cs-hp-mp-model); a level-down only re-clamps."""
        char = self._session_char(session)
        if char is None:
            log.warning(f'[COMBAT] {exp_delta:+} exp for {session.get("username")!r}: no character; dropped')
            return 0
        # Lock order (F5): combat_lock -> db_lock. Both are held for the read-modify-write
        # so a concurrent kill cannot lose a delta or heal against a stale level.
        with self._combat_lock(session):
            with self.store.lock:
                old = progression.clamp_exp(char.get('exp', 0))
                new = progression.clamp_exp(old + int(exp_delta))
                delta = new - old
                if delta == 0:
                    return 0
                char['exp'] = new
                old_lv = progression.level_for_exp(old)
                new_lv = progression.level_for_exp(new)
                session['level'] = new_lv
                if new_lv != old_lv:
                    # The client's own rule on the same 0x21 (handler 0x4542C4): a level-up
                    # calls FUN_00427d40/FUN_00427f40 with 1 = full heal at the NEW maxima; a
                    # level-down (negative delta across a threshold) only re-clamps (live:
                    # 434/434 -> 432/432 at Lv13 -> 12). cs-hp-mp-model owns the formula. A
                    # corpse (a DoT kill credited to a caster who died since) is only
                    # re-clamped: the heal would leave a live model under the death dialog.
                    hpmp.refresh(session, char, full=new_lv > old_lv and not session.get('dead'))
                    char['hp'], char['mp'] = R.vitals(session, char)
        self.store.mark_dirty(f'exp {char.get("name")}')
        log.info(f'[COMBAT] {delta:+d} exp (total={new}, lv={new_lv})')
        # 0x21 is dropped by a client with no local player (scene+0x970 == 0), so it only
        # goes out once our own 0x07 has registered one.
        sock = session.get('sock')
        if sock is not None and session.get('in_world'):
            if menti_id is not None and delta > 0:
                P.send(self, sock, session, '0x7F', {'exp_delta': delta, 'menti_id': int(menti_id) & 0xFFFFFFFF})
            else:
                P.send(self, sock, session, '0x21', *self._exp_delta_packet(session, delta, guild_base))
        if new_lv < old_lv:
            # No 0x22 for a level-down (docstring): every holder's copy is replaced with a
            # record that carries the lower level (livetest bug 8), and a record in flight is
            # rebuilt with it (touch). Same lock rules as the level-up branch below.
            seen = presence.reshow_to_holders(self, session)
            log.info(f'[LEVEL] {old_lv} -> {new_lv} at exp {new} (level-down: no 0x22 to anyone; '
                     f'0x06 + a Lv{new_lv} record to {seen} observer(s))')
        elif new_lv > old_lv:
            # lc-level-broadcast, after the store lock is released. A kill's caller may still
            # hold its combat and monster locks: presence takes only each observer's presence
            # lock and then its send lock, both below those on the ladder (world.py "Locks").
            # A player in a map load reaches nobody here; his next arrival's record carries
            # the new level.
            seen = presence.to_holders(self, session, '0x22',
                                       {'uid': P.session_uid(session) or 0, 'level': new_lv & 0xFF},
                                       'LEVEL')
            log.info(f'[LEVEL] {old_lv} -> {new_lv} at exp {new} (client-side for the owner: no '
                     f'0x22 to him; 0x22 to {seen} observer(s))')
        return delta

    def _exp_delta_packet(self, session, delta, guild_base=None):
        """(fields, assume) of S2C 0x21 for this receiver. 2009 (spec_2009 0x21): a positive
        delta carries a trailing u32 guild_points only when the receiver's OWN entity+0x12
        guild id is >= 2 (the client prints "(+%u) guild points are gained." and stores
        nothing). No guild model exists (_receiver_guild_id 0), so the tail is never sent -
        but the gate is the receiver's guild, never a constant, so a guild model only has
        to answer _receiver_guild_id and fill guild_points. `guild_base` (award_exp): the
        pre-multiplier exp of an earned grant, which P14 turns into guild_points (X9); None
        for a GM or penalty grant."""
        fields = {'exp_delta': int(delta)}
        if self.client_build != cfgmod.BUILD_2009:
            return fields, None
        guild = self._receiver_guild_id(session)
        in_guild = guild >= 2
        if in_guild and delta > 0:
            fields['guild_points'] = 0          # the guild model credits these
        return fields, {'local_player_guild_id > 1': in_guild}

    def _send_drop(self, sock, session, item=0, count=0, gold_gain=0, winnie_gain=0, no_enc=False):
        """S2C 0x18 GetItem {gold, victy, item_id, count}: absolute wallet values plus an
        optional item grant. item 0 is a currency-only update; an id the EN client lacks
        is never sent (en_item_exists, S2-11)."""
        if item and not self._grantable_item(item, 'S2C 0x18'):
            item, count = 0, 0
        wallet = self._wallet_of(session)
        if wallet is None:
            log.info(f'[DROP] item={item}x{count} gold+={gold_gain} dropped: no character')
            return
        wallet.earn(gold_gain, winnie_gain)
        gold, victy = self._wallet_commit(session, wallet, 'drop')
        log.info(f'[DROP] item={item}x{count} gold+={gold_gain} -> gold={gold}')
        P.send(self, sock, session, '0x18', invmod.currency_fields(wallet, item, count))

    def _build_opcode_03(self, session, char, current_map=None, clock=None):
        """S2C 0x03 EnterWorldState, built from the character record and nothing else
        (world-03-real-state + item_inventory-seed-03-07 + quest_cards_misc-03-quest-card-fields,
        S1-05).

        This packet IS the client's state: its handler memsets all four bag tabs, zeroes the
        quest arrays and the ready flags, overwrites gold, Victy and the exp baseline, and
        then reads whatever the server sends. It goes out at enter world AND on every map
        load, so the old hardcoded values (clock 1000, gold 100000, exp_total 30000, four
        zero list counts) wiped the bag and the quest log on every portal and reset the
        wallet the next 0x18 then jumped to 999999 (world B2/B3, item B5/B6, Q-B8).

        Everything below now comes from the store: the map the caller is loading, the
        session clock, wallet + capacities + the three bag lists (inventory.fields_for_03),
        the 3 active quest slots with their progress, the completed list and the card deck
        count (quests.fields_for_03), exp, fame and the battle record.

        The mentor block (social_friend F8.1, P6 stage 2) is the mentee's mentor: account uid,
        name and channel (0x66 while offline) - messenger.mentor_fields_03. Both handlers
        read it right AFTER the messenger reset (2008 FUN_0046f860 at 0x44E52F, 2009
        FUN_0047a1a0), so re-sending it on every map load never duplicates the entry. No
        mentor = mentor_id 0, the 64-byte empty form the live 101 -> 102 capture shows. The
        battle W/L/KO/Down counters stay zero until pvp records exist.
        """
        if current_map is None:
            current_map = session.get('current_map') or self.config.START_MAP
        battle = char.get('battle') or {}          # pvp records fill this (pvp-match-records)
        fields = {
            # scene+0x217, the local character's channel number; PySlayer: "must be >= 1".
            # The same value the 0x99 sub 8 "In channel N." line prints (_channel_no).
            'channel_id': self._channel_no(session),
            'map_code': int(current_map) & 0xFFFF,
            'game_clock_ms': self._game_clock(session) if clock is None else int(clock) & 0xFFFFFFFF,
            # scene+0x278: the client computes the exp bar and its own level from this
            # against the cumulative curve at 0x6F0EB0, so a constant here auto-levelled a
            # fresh character to 13 on the first kill (S1-05).
            'exp_total': progression.clamp_exp(char.get('exp', 0)),
            'fame_points': int(char.get('fame', 0)) & 0xFFFFFFFF,
            'battle_win': int(battle.get('win', 0)) & 0xFFFFFFFF,
            'battle_lose': int(battle.get('lose', 0)) & 0xFFFFFFFF,
            'battle_ko': int(battle.get('ko', 0)) & 0xFFFFFFFF,
            'battle_down': int(battle.get('down', 0)) & 0xFFFFFFFF,
        }
        fields.update(self.messenger.mentor_fields_03(session, char))
        fields.update(invmod.fields_for_03(char))       # gold, Victy, capacities, 3 bag lists
        fields.update(questmod.fields_for_03(char))     # 3 slots + progress, completed, deck count
        if self.client_build == cfgmod.BUILD_2009:
            self._fields_03_2009(session, char, fields)
        return P.build('0x03', fields, client_build=self.client_build)

    def _fields_03_2009(self, session, char, fields):
        """The 2009 S2C 0x03 changes (spec_2009 0x03 diff_vs_2008), in place: a bool `gender`
        after battle_down (the character's own, R.char_gender), FIVE active quest slots
        (repeat(5) ids + repeat(5) progress; the server's quest model has three, so slots 4
        and 5 go out empty until the quest stage widens it) and the deck-count byte renamed
        unk_e78 -> unk_e90."""
        fields['gender'] = R.char_gender(char, self._session_account(session))
        rows = list(fields.pop(f'repeat[{questmod.MAX_ACTIVE}]', []))
        rows += [{'active_quest_id': 0, 'active_quest_progress': 0}] * (5 - len(rows))
        fields['repeat[5]'] = rows[:5]
        fields['unk_e90'] = fields.pop('unk_e78', 0)

    def _player_record(self, session, char, *, remote=False, pos=None):
        """The per-player row for S2C 0x07 / 0x04 / 0x05, built from the store and this
        session by records.player_record (world-player-record, lc-spawn-fields).

        Everything the old hand-packed `_build_en_opcode_07` invented comes from the store
        now: uid = account uid, karma = account manner, level from exp, gender, the 14 look
        words, real STR/DEX/INT/SPR, the equip grid with its option words, live cur_hp /
        cur_mp and the idle motion block (no IP octets at +0x8B3, no 32/1/501, S2-26/S2-27).
        The wire layout is the 0x07 grammar, so the record stays the live-proven 368 B."""
        account = self._session_account(session)
        return R.player_record(session, char, account, remote=remote, pos=pos,
                               client_build=self.client_build)

    def _build_player_spawn(self, session, char, pos=None):
        """The S2C 0x07 payload with this session's own record: the packet that registers
        the local player (gate 0x4221A2 needs uid == scene+0x220). Built, not sent: it is
        one of the packets MapTransfer prepares before it commits anything, so a record that
        will not encode cannot leave the client half-way through a map load."""
        return P.build('0x07', R.player_list(self._player_record(session, char, pos=pos)),
                       client_build=self.client_build, receiver_uid=P.session_uid(session))

    # S2C 0x1C CreateCharacterResult codes, with the client message each one shows
    # (live-verified, login_character#06). Anything else only closes the waiting modal.
    CREATE_OK = 1              # closes window 0x16; the client builds the entity itself
    CREATE_NAME_IN_USE = 2     # "The name is already being used. Please try another name."
    CREATE_REFUSED = 3         # "Server process is running. Please try again ... (DB)"
    CREATE_BAD_NAME = 4        # "Invalid name. Please try another name."

    def _validate_new_character(self, account, rec):
        """(result, reason) for a C2S 0x0E record: CREATE_OK, or the 0x1C refusal and why
        (login_character.md F3 step 2, validation order a -> b -> c)."""
        name = names.clean(rec.get('name', ''))
        # a. name rules first (the client checked them too; a packet that skips them is
        #    either tampered or a non-EN client).
        bad = names.check(name)
        if bad is not None:
            return self.CREATE_BAD_NAME, f'invalid name: {bad}'
        # b. globally unique, compared case-insensitively (F3; every name-addressed packet
        #    resolves through this index).
        owner = self.store.name_owner(name)
        if owner is not None:
            return self.CREATE_NAME_IN_USE, f'name already used on account {owner!r}'
        # c. everything the client itself enforces: the 5-slot cap, the stat pool spent
        #    exactly (stat_total(1) = 9) and looks inside the gender's ranges.
        chars = account.get('characters') or []
        if len(chars) >= storemod.MAX_CHARACTERS:
            return self.CREATE_REFUSED, f'account already has {len(chars)} characters'
        stats = [int(rec.get(k, 0)) for k in ('str', 'dex', 'int', 'tol')]
        want = progression.stat_total(1)
        if any(v < 0 for v in stats) or sum(stats) != want:
            return self.CREATE_REFUSED, f'stats {stats} sum {sum(stats)} != stat_total(1) = {want}'
        gender = self._create_gender(account, rec)
        base = 0x64 if gender else 0                      # 1..4 / 2..5, or the 101-based set
        ranges = {'look_slot10': range(base + 1, base + 5), 'look_slot1': range(base + 1, base + 5),
                  'look_slot6': range(base + 2, base + 6), 'look_slot5': range(base + 2, base + 6),
                  'look_slot9': range(2, 6)}              # s9 is 2..5 for both genders
        for field, allowed in ranges.items():
            value = int(rec.get(field, 0))
            if value not in allowed:
                return self.CREATE_REFUSED, (f'{field} {value} outside {allowed.start}..{allowed.stop - 1} '
                                             f'for gender {gender}')
        return self.CREATE_OK, ''

    def _create_gender(self, account, rec):
        """The gender a C2S 0x0E creates: 2008 = the account's flag (scene+0x130 from S2C
        0x02); 2009 = the packet's own bool (spec_2009 0x44B961/0x0E `gender`, creation
        window ctrls 0x1E -> 1 / 0x1F -> 0), which picks the look set exactly like the 2008
        flag did (1 = the 101-based set)."""
        if self.client_build == cfgmod.BUILD_2009:
            return 1 if int(rec.get('gender', 0)) else 0
        return 1 if account.get('gender') else 0

    def _handle_create_character(self, sock, session, rec, no_enc=False):
        """
        Create character (C2S 0x0E, spec 0x449384/0x0E, 35 B):
          u16 look_slot10 (body appearance; the old server stored it as `class`, B7),
          u16 look_slot1/6/5/9, str[17] name, u16 str, dex, int, tol(spr)
        2009 (spec_2009 0x44B961/0x0E, 36 B): + bool gender after the name. It is stored as
        the character's own `gender` (the 2009 0x02/0x07 records carry it per character)
        and decides the look ranges (_create_gender).

        Flow F3. Validation order and replies (S2C 0x1C, all live-verified):
          4 invalid name, 2 name in use, 3 refused (cap / stat sum / look range), 1 OK.
        The record written is the lc-data-model schema (store.new_character): a Novice
        (class 0, job2 0, exp 0) with look [0,s1,0,0,s10,s5,s6,0,0,s9,s10,0,0,s10] at the
        configured start point (map 101 (1411,714), not map 0 (100,100)), saved at once.

        On result 1 the client builds the select entity from its own creation state, so no
        0x02 follows: live verification (login_character#03/#05) showed a second success
        0x02 at character select appends duplicate entities (uids restart at 30000000) and
        leaves window 0x13 open. `CREATE_REPLY_0x02` restores the old behaviour.
        """
        name = names.clean(rec.get('name', ''))
        log.info(f'[CREATE] name="{name}" look s10={rec.get("look_slot10")} s1={rec.get("look_slot1")} '
                 f's6={rec.get("look_slot6")} s5={rec.get("look_slot5")} s9={rec.get("look_slot9")} '
                 f'stats={[rec.get(k) for k in ("str", "dex", "int", "tol")]}'
                 + (f' gender={rec.get("gender")}' if 'gender' in rec else ''))

        username = session.get('username')
        with self.store.lock:
            account = self.store.account(username)
            if account is None:
                # Pre-login 0x0E: the policy's own refusal is suppressed before login, so
                # the client (which cannot have sent this) is left alone.
                log.warning('[CREATE] no logged-in account on this session; ignored')
                return
            result, reason = self._validate_new_character(account, rec)
            if result == self.CREATE_OK:
                # `look` is the whole appearance now: lc-charlist and lc-spawn-fields send
                # it verbatim, so the legacy face/top/bottom/shoes keys the old builders
                # rendered from are no longer written (migrated records keep theirs).
                char = self.store.new_character(
                    name, s10=rec['look_slot10'], s1=rec['look_slot1'], s6=rec['look_slot6'],
                    s5=rec['look_slot5'], s9=rec['look_slot9'],
                    stats=[rec['str'], rec['dex'], rec['int'], rec['tol']],
                    gender=self._create_gender(account, rec))
                # Starter kit: the design gives a Novice no items (F3 2d), so this is 0 by
                # default. A configured id is WORN from the start: it goes into its grid slot
                # (the 0x07 equip ids) and the look is composed as a 0x1D would compose it,
                # so the hand, the grid and the Equipment window agree (it used to write the
                # item id into `weapon`, which the old record sent as a sprite number).
                starter = int(self.config.STARTER_WEAPON)
                if starter and self._grantable_item(starter, 'STARTER_WEAPON'):
                    self._wear_starter(char, account, starter)
                # cs-skill-learn "starting skills": none by default. EN hands Dash, Double
                # Jump, Mining, ... out as quest rewards (quests 26/28/155/156, open question
                # Q11); STARTING_SKILLS lets a server give them at creation instead.
                for sid in self.config.STARTING_SKILLS:
                    res = SK.learn(char, sid, check=False)
                    if not res.ok:
                        log.warning(f'[CREATE] STARTING_SKILLS {sid} not learned: {res.why}')
                # Saved immediately (F4), once store.lock is let go (_save_store_now).
                self.store.add_character(username, char, save=False)
                log.info(f'[CREATE] "{name}" created for {username!r}: Novice at map '
                         f'{char["map"]} ({char["x"]}, {char["y"]}), select slot '
                         f'{len(account["characters"]) - 1}')
            else:
                log.warning(f'[CREATE] "{name}" refused with 0x1C result {result}: {reason}')
        if result == self.CREATE_OK:
            self._save_store_now(f'create "{name}"')

        P.send(self, sock, session, '0x1C', {'result': result})
        if result == self.CREATE_OK and self.config.CREATE_REPLY_0x02:
            resp = self._build_login_success(session, self.store.account(username))
            self._send_encrypted(sock, session, 0x02, resp, use_by_array=no_enc)

    def _handle_delete_character(self, sock, session, rec, no_enc=False):
        """
        Delete character (C2S 0x12, spec 0x44ABA4/0x12, 38 B: str[17] char_name +
        str[21] confirm_password), flow F4.

        The client reaches this through the password dialog: 0x201 OK sends C2S 0x51 with
        target_window_id 0x235, the server answers S2C 0x80 {1, 0x235}, the confirm window
        0x235 opens and its OK sends this packet plus the modal (live login_character#07).

        S2C 0x1F results (live-verified): 1 removes the entity, 2 "try again (DB)" for an
        unknown name, 3 "created within 24 hours", 12 "Invalid password". Every 0x12 gets
        one, or the modal never closes (B11) - the registry's must-reply policy is the
        backstop if this raises.
        """
        name = names.clean(rec.get('char_name', ''))
        username = session.get('username')
        min_age = float(self.config.DELETE_MIN_AGE_HOURS) * 3600.0
        with self.store.lock:
            account = self.store.account(username)
            if account is None:
                log.warning('[DELETE] no logged-in account on this session; ignored')
                return
            char = self.store.find_character(username, name)
            if char is None:
                # Not on this account (wrong name, or another account's character): DB-busy,
                # which leaves the client's list untouched.
                result, reason = 2, 'not a character of this account'
            elif not auth.verify(account.get('password'), rec.get('confirm_password', '')):
                result, reason = 12, 'wrong password'
            elif min_age and time.time() - float(char.get('created_at', 0) or 0) < min_age:
                result, reason = 3, f'created less than {self.config.DELETE_MIN_AGE_HOURS} h ago'
            else:
                # Order is kept for the rest: the client drops the selected entity and
                # shifts its neighbours itself, so the remaining slots must still line up.
                # Saved at once, after store.lock is let go (_save_store_now below).
                self.store.remove_character(username, char['name'], save=False)
                result, reason = 1, f'deleted, {len(account["characters"])} left'
        if result == 1:
            self._save_store_now(f'delete "{name}"')
        if result == 1 and session.get('char_name') == name:
            # This session had the character selected (Back from the world keeps it): drop
            # the refs so nothing (combat driver, exp) writes to a record that is gone, and
            # free the name index for the next character that takes it.
            session['char'] = session['char_name'] = None
            self._leave_world(session, 'character deleted')
            self.world.leave(session)
            # Off its map's shared monsters too, so none of their packets reaches this
            # session at character select (cs-monster-death, world-shared-monsters).
            self._clear_map_monsters(session)
        log.info(f'[DELETE] {username!r} "{name}" -> 0x1F result {result} ({reason})')
        P.send(self, sock, session, '0x1F', {'result': result})

    # ========================================================================
    # Password gate (C2S 0x51 -> S2C 0x80) and the bank (shop_storage-password-gate,
    # premium_cash-password-gate, shop_storage-bank-model/-gold/-items/-fallback;
    # shop_storage.md 1.5, F3-F7, premium_cash.md F6/F18).
    #
    # How each client build reaches the bank window 0x1A7:
    #   2008  NPC 97 "Mikomakisho" (hni UI 423) -> FUN_00464480: if scene+0x218 == 0 the box
    #         "no SS# in this ID..." and nothing is sent (then use `!bank` / `/bank`), else
    #         password dialog 0x201 -> C2S 0x51 {password, 0x1A7} -> S2C 0x65, 0x80 {1, 0x1A7},
    #         0x65. The grid is drawn only from the block 0x65 fills (C32: any 0x65 before the
    #         0x80 is enough; the second one resets the tab, harmless).
    #   2009  the 0x5A handler FORCES scene+0x21E (the 2008 +0x218 flag) to 0 at 0x451C8E, so
    #         FUN_00446000 skips the password dialog and opens 0x1A7 directly with no packet
    #         at all (and window 0x235 the same way, FUN_00448730 case 2). The grid therefore
    #         shows whatever 0x65 the client last got: _hook_bank_contents sends one at every
    #         enter world of a 2009 client. The client never asks for 0x65 in either build.
    # Every later bank change is applied by the client itself on 0x66 / 0x67 / 0x68 / 0x69
    # with its own tab algorithms, which bank_tabs.py ports; a refusal re-sends 0x65 (items)
    # or 0x68/0x69 {0, bank_gold, gold} (gold), because 0x66/0x67 carry no failure code.
    # ========================================================================
    # C2S 0x51 target windows the server may unlock (F3 step 4 / premium_cash F6 step 2). The
    # client opens WHATEVER id 0x80 names, so an id outside this list is refused, never
    # echoed. 2009's new special cases 0x1FA / 0x4B8 / 0x4CF are cash-shop windows
    # (FUN_00464890 / FUN_00464400 / FUN_00465890) the premium_cash phase (P8) owns; the 2009
    # client never sends 0x51 anyway (scene+0x21E is forced to 0).
    PASSWORD_WINDOW_BANK = EC.UI_BANK                 # 0x1A7
    PASSWORD_WINDOW_GIFT = 0x1F9                      # cash-shop gift / purchase confirm
    PASSWORD_WINDOW_DELETE_CONFIRM = 0x235
    PASSWORD_WINDOWS = frozenset((PASSWORD_WINDOW_BANK, PASSWORD_WINDOW_GIFT,
                                  PASSWORD_WINDOW_DELETE_CONFIRM))

    def _handle_password_verify(self, sock, session, rec, no_enc=False):
        """C2S 0x51 PasswordVerifyForWindow {str[21] password, u32 target_window_id} ->
        S2C 0x80 {result, target_window_id when result == 1}.

        The password is read to its first NUL (the client copies it into an uninitialised
        stack buffer, so the rest of the 21 bytes is garbage) and compared with the account's
        `second_password` when one is set, else the login password (auth.verify: hashed or a
        hand-edited plaintext record). Wrong password -> 0x80 {0} ("Invalid password.").
        Right password:
          0x235  -> 0x80 {1, 0x235}: the delete-confirm window, whose OK sends C2S 0x12 (the
                    step that made lc-delete reachable, live login_character#07).
          0x1A7  -> 0x65, 0x80 {1, 0x1A7}, 0x65 (the bank; needs a character in world).
          0x1F9  -> 0x80 {1, 0x1F9}: the cash-shop flow continues client-side (it re-checks
                    the cash balance; premium_cash owns the rest).
          other  -> 0x80 {0}.
        session['pw_ok'][window] records the unlock time for the handlers behind it."""
        target = int(rec.get('target_window_id', 0))
        if not session.get('username'):
            # Same rule as the must-reply policy: a pre-login socket cannot have opened a
            # password dialog, so it gets nothing to work with.
            log.warning('[PASSWORD] 0x51 before login; ignored')
            return
        account = self._session_account(session)
        stored = (account or {}).get('second_password') or (account or {}).get('password')
        ok = bool(account) and auth.verify(stored, rec.get('password', ''))
        who = session.get('username')
        if not ok:
            log.info(f'[PASSWORD] window 0x{target:X} for {who!r}: wrong password -> 0x80 {{0}}')
            P.send(self, sock, session, '0x80', {'result': 0})
            return
        if target not in self.PASSWORD_WINDOWS:
            log.warning(f'[PASSWORD] window 0x{target:X} for {who!r} is not unlockable '
                        f'(allowlist {sorted(hex(w) for w in self.PASSWORD_WINDOWS)}); refused')
            P.send(self, sock, session, '0x80', {'result': 0})
            return
        if target == self.PASSWORD_WINDOW_BANK:
            if not self._open_bank(sock, session, 'password'):
                P.send(self, sock, session, '0x80', {'result': 0})
            return
        session.setdefault('pw_ok', {})[target] = time.monotonic()
        log.info(f'[PASSWORD] window 0x{target:X} unlocked for {who!r}')
        P.send(self, sock, session, '0x80', {'result': 1, 'target_window_id': target})

    # ---- the bank ----
    def _bank(self, session):
        """The bank_tabs.Bank of this session's character (the client build picks the etc-tab
        rule, bank_tabs.etc_change), or None outside a character."""
        char = self._session_char(session)
        return None if char is None else BT.Bank(char, build=self.client_build)

    def _send_bank_contents(self, sock, session):
        """S2C 0x65 BankContents: the whole bank block (capacities, three slot lists, bank
        gold) from the persisted model. It is also the resync of every refused item move."""
        char = self._session_char(session)
        if char is None:
            return False
        P.send(self, sock, session, '0x65', BT.fields_for_65(char, self.client_build))
        return True

    def _open_bank(self, sock, session, why):
        """0x65, S2C 0x80 {1, 0x1A7}, 0x65: open window 0x1A7 in bank mode with the grid drawn
        from the model (F3 step 4 and the F3b fallback). 0x80 has no state gate client-side,
        so this works whether or not a password dialog was ever shown. In world only: the
        bank window belongs to the map UI and the model to the selected character."""
        if not session.get('in_world') or self._session_char(session) is None:
            log.info(f'[BANK] {why}: {session.get("username")!r} has no character in world; not opened')
            return False
        self._send_bank_contents(sock, session)
        P.send(self, sock, session, '0x80', {'result': 1, 'target_window_id': self.PASSWORD_WINDOW_BANK})
        self._send_bank_contents(sock, session)
        now = time.monotonic()
        session['bank_open'] = now
        session.setdefault('pw_ok', {})[self.PASSWORD_WINDOW_BANK] = now
        bank = self._bank(session)
        log.info(f'[BANK] opened for {session.get("char_name")!r} ({why}): caps {bank.caps}, '
                 f'gold {bank.gold}, {sum(1 for t in BT.TABS for e in bank.data[t] if e)} slot(s)')
        return True

    def _hook_bank_contents(self, server, session, map_code=None, reason=None, **_):
        """`on_enter_world` hook: the 2009 client opens the bank with no packet (scene+0x21E
        forced to 0, FUN_00446000), so its bank block must already hold this character's
        bank - one S2C 0x65 per world entry (a map change keeps the block: only 0x65 writes
        scene+0x998.., and the 0x03 handler redraws the grid from it). The 2008 client gets
        its 0x65 from the password gate instead, so its enter world is unchanged."""
        if self.client_build != cfgmod.BUILD_2009 or reason != 'enter_world':
            return
        sock = session.get('sock')
        if sock is not None and self._send_bank_contents(sock, session):
            log.info(f'[BANK] 0x65 at enter world for {session.get("char_name")!r} '
                     f'(2009: no bank request exists)')

    def _bank_request_ok(self, session, what):
        """Common gate of C2S 0x3C..0x3F: a character in world. The bank window's own state
        is a soft gate (the client never reports closing it, F4 step 2): logged, not
        enforced - and a 2009 client opens the window without any packet at all."""
        if not session.get('in_world') or self._session_char(session) is None:
            log.warning(f'[BANK] {what} from {session.get("username")!r} with no character in world; dropped')
            return False
        if self.client_build != cfgmod.BUILD_2009 and not session.get('bank_open'):
            log.info(f'[BANK] {what} from {session.get("char_name")!r} without a bank opened '
                     f'by this session (soft gate: accepted)')
        return True

    def _bank_item_refusal(self, sock, session, what, why, text):
        """A refused item move: the full 0x65 (the client changed nothing yet - it waits for
        0x66/0x67 - so this only re-asserts the truth) plus a 0x15 line saying why."""
        log.info(f'[BANK] {what} refused: {why}')
        self._send_bank_contents(sock, session)
        self._notice(sock, session, text, 'warn')

    def _handle_bank_deposit_item(self, sock, session, rec, no_enc=False):
        """C2S 0x3C BankDepositItem {id, qty, n, opts, extra} -> S2C 0x66 {gold after the fee,
        id, qty, the request's descriptor} (shop_storage-bank-items, F4).

        Checks in the client's order: descriptor (n <= 5), a bank-able Type 0/1/2 that is no
        Cash item, quantity 1..999 / 1..99 / 1, ownership (exact block for equipment), the
        manner fee 0/25/50 against gold, then the client's OWN add simulated on a copy - its
        pre-check FUN_00427B70 is looser than the add (998 + 10 in two slots with no empty
        slot passes the check and fails the add), and a 0x66 the add rejects would leave the
        client's bank and bag disagreeing. Commit: fee, bag remove, bank add, persist."""
        item, qty, n, opts, extra = P.item_descriptor(rec)
        what = f'deposit {item} x{qty}'
        if not self._bank_request_ok(session, what):
            return

        def refuse(why, text="That item can't be stored."):
            self._bank_item_refusal(sock, session, what, why, text)

        if n > P.OPTION_LIST_MAX:
            return refuse(f'{n} option words (max {P.OPTION_LIST_MAX})')
        info = en_item(item)
        if info is None or info.type not in BT.TAB_OF_TYPE:
            return refuse(f'Type {getattr(info, "type", None)} is not a bank item')
        if info.is_cash:
            return refuse('cash item (hii Cash, itemdef+0x1F0 != 0)')
        limit = SH.qty_limit(info.type)
        if not 1 <= qty <= limit:
            return refuse(f'qty {qty} outside 1..{limit}')
        words = invmod.block_from_wire(opts, extra) if info.type == EC.TYPE_EQUIPMENT else None
        if not self._inv_has(session, item, qty, words):
            return refuse('not owned in that quantity / block', "You don't have that many of that item.")
        escrow = self._escrow_refusal(session, item, qty, words)
        if escrow is not None:
            return refuse(*escrow)                                   # trade-escrow-guards
        wallet = self._wallet_of(session)
        bank = self._bank(session)
        fee = SH.bank_fee(self.store.manner(session.get('username')))
        why = self.trade.gold_refusal(session, fee)
        if why is not None:
            return refuse(why, trademod.GOLD_IN_TRADE_TEXT)
        with self._combat_lock(session):
            if wallet.gold < fee:
                return refuse(f'fee {fee} > gold {wallet.gold}', 'You are short of gold.')
            if not bank.fits(item, qty, words):
                return refuse(f'the client add fails (caps {bank.caps}; pre-check '
                              f'{"passes" if bank.space_ok(item, qty) else "fails"})',
                              'There are no empty space in Bank.')
            wallet.pay(fee)
            self._inv_remove(session, item, qty, words)
            bank.add(item, qty, words)
            gold, _victy = self._wallet_commit(session, wallet, 'bank deposit')
        log.info(f'[BANK] {EC.item_name(item)} {item} x{qty} deposited (fee {fee}) -> gold {gold}')
        # The descriptor is echoed VERBATIM: the client builds the bank record from it and
        # then removes the bag item by an exact 12-byte compare against the same bytes.
        P.send(self, sock, session, '0x66', dict(
            invmod.item_fields('0x66', item, block=invmod.echo_block_fields('0x66', opts, extra)),
            qty=qty, gold=gold))

    def _handle_bank_withdraw_item(self, sock, session, rec, no_enc=False):
        """C2S 0x3D {id, qty, n, opts, extra} -> S2C 0x67 {id, qty, the STORED descriptor}
        (shop_storage-bank-items, F5). Two client send paths share the wire format: (a)
        equipment (double-click / drag) sends qty 1 and the full block, (b) the amount dialog
        0x1A9 for stackables sends n=0, extra=0. Equipment picks the bank slot by exact
        block, else - for an all-zero request - the first slot with that id; the 0x67 then
        carries that slot's own block, because the client memcmps it against its slot."""
        item, qty, n, opts, extra = P.item_descriptor(rec)
        what = f'withdraw {item} x{qty}'
        if not self._bank_request_ok(session, what):
            return

        def refuse(why, text="That item can't be withdrawn."):
            self._bank_item_refusal(sock, session, what, why, text)

        if n > P.OPTION_LIST_MAX:
            return refuse(f'{n} option words (max {P.OPTION_LIST_MAX})')
        info = en_item(item)
        if info is None or info.type not in BT.TAB_OF_TYPE:
            return refuse(f'Type {getattr(info, "type", None)} is not a bank item')
        bank = self._bank(session)
        words = None
        if info.type == EC.TYPE_EQUIPMENT:
            qty = 1                                  # path (a) hard-wires 1 (spec 0x3D)
            words = bank.pick_equip(item, invmod.block_from_wire(opts, extra))
            if words is None:
                return refuse('no bank slot holds that id / block', "The bank doesn't hold that item.")
        else:
            limit = SH.qty_limit(info.type)
            if not 1 <= qty <= limit:
                return refuse(f'qty {qty} outside 1..{limit}')
            if bank.total(item) < qty:
                return refuse(f'bank holds {bank.total(item)}', "The bank doesn't hold that many.")
        # The space check, the bank remove and the bag add form one step under the combat
        # lock: a kill's loot (added under the same lock by the driver thread) must not take
        # the last slot between the check and the add, or the item would leave the bank for
        # nowhere.
        with self._combat_lock(session):
            why = self._bag(session).fits(item, qty, words)
            if why is not None:
                return refuse(why, "There isn't empty space in the inventory.")
            if not bank.remove(item, qty, words):
                return refuse('the client remove fails', "The bank doesn't hold that many.")
            if self._inv_add(session, item, qty, 'bank withdraw', words) is None:
                bank.add(item, qty, words)           # put it back: nothing is lost
                return refuse('the bag add failed', "There isn't empty space in the inventory.")
            self.store.mark_dirty(f'bank withdraw {item}')
        log.info(f'[BANK] {EC.item_name(item)} {item} x{qty} withdrawn')
        P.send(self, sock, session, '0x67', dict(invmod.item_fields('0x67', item, words), qty=qty))

    @staticmethod
    def _bank_gold_amount(rec):
        """The u64 of C2S 0x3E/0x3F. The client reads it with _atol and sign-extends: a
        negative entry arrives as >= 2^63 and is refused like 0 (F6 step 3)."""
        amount = int(rec.get('gold', 0))
        return 0 if amount >= 1 << 63 else amount

    def _handle_bank_deposit_gold(self, sock, session, rec, no_enc=False):
        """C2S 0x3E BankDepositGold {u64 gold} -> S2C 0x68 (shop_storage-bank-gold, F6)."""
        self._bank_gold_move(sock, session, rec, deposit=True)

    def _handle_bank_withdraw_gold(self, sock, session, rec, no_enc=False):
        """C2S 0x3F BankWithdrawGold {u64 gold} -> S2C 0x69 (shop_storage-bank-gold, F7)."""
        self._bank_gold_move(sock, session, rec, deposit=False)

    def _bank_gold_move(self, sock, session, rec, *, deposit):
        """Move gold between the wallet and the bank. S2C 0x68 / 0x69 {amount, storage_gold,
        gold} both run the same client code (absolute bank and carried gold; the amount is
        not read); 0x68 answers a deposit and 0x69 a withdraw (our convention, Q1). A refusal
        (0, a negative entry, more than the source holds, a u64 overflow) is the same packet
        with amount 0: a no-op resync of both labels."""
        what = 'gold deposit' if deposit else 'gold withdraw'
        key = '0x68' if deposit else '0x69'
        if not self._bank_request_ok(session, what):
            return
        amount = self._bank_gold_amount(rec)
        bank, wallet = self._bank(session), self._wallet_of(session)
        # trade-escrow-guards: gold locked into a trade (C2S 0x23) cannot be deposited.
        escrow = self.trade.gold_refusal(session, amount) if deposit and amount else None
        with self._combat_lock(session):
            src = wallet.gold if deposit else bank.gold
            dst = bank.gold if deposit else wallet.gold
            ok = 0 < amount <= src and dst + amount <= BT.GOLD_MAX and escrow is None
            if ok:
                if deposit:
                    wallet.gold, bank.gold = wallet.gold - amount, bank.gold + amount
                else:
                    wallet.gold, bank.gold = wallet.gold + amount, bank.gold - amount
                self._wallet_commit(session, wallet, f'bank {what}')
            fields = {'amount': amount if ok else 0, 'storage_gold': bank.gold, 'gold': wallet.gold}
        if not ok:
            log.info(f'[BANK] {what} of {int(rec.get("gold", 0))} refused (gold {wallet.gold}, '
                     f'bank {bank.gold})')
            P.send(self, sock, session, key, fields)
            if escrow is not None:
                log.info(f'[BANK] {what}: {escrow}')
            self._notice(sock, session, trademod.GOLD_IN_TRADE_TEXT if escrow is not None
                         else 'You entered more gold than you have.' if amount
                         else 'Enter the amount of the gold.', 'warn')
            return
        log.info(f'[BANK] {what} {amount} -> gold {wallet.gold}, bank {bank.gold}')
        P.send(self, sock, session, key, fields)

    def _handle_id_transfer(self, sock, session, rec, no_enc=False):
        """
        C2S 0x65 / 0x66 AccountTransferRequest {str[33] target_id} -> S2C 0x8C {result 6}.

        Legacy Yahoo-ID migration. The server always sends transfer_status 3 in S2C 0x02,
        which hides select-screen controls 5/6/7, so this is unreachable from the stock UI
        (live login_character#23); a packet that arrives anyway is logged and refused with
        "No id information...". Result 2 is never sent: it carries a str[33] new_id that
        the client sprintf_s'es into a 64-byte buffer (lc-id-transfer-stub, F11).

        2009 (client-2009-world): the client has no S2C 0x8C handler any more (s2c_format_diff
        gone_in_2009) and its 0x65/0x66 reply is "not traced", so nothing is sent; the
        controls stay hidden by the same transfer_status 3 (spec_2009 0x02).
        """
        target = names.clean(rec.get("target_id", ""))
        if self.client_build == cfgmod.BUILD_2009:
            log.warning(f'[ID-TRANSFER] {session.get("username")!r} sent a legacy transfer request for '
                        f'{target!r}; the 2009 client has no S2C 0x8C: dropped')
            return
        log.warning(f'[ID-TRANSFER] {session.get("username")!r} sent a legacy transfer request for '
                    f'{target!r}; refused with 0x8C result 6')
        P.send(self, sock, session, '0x8C', {'result': 6})

    # S2C 0x02 failure codes that differ per build (spec_2009 0x02 `result`, jump table
    # 0x45E124). 2008 answers an unknown account and a wrong password alike with 0x11 ("Enter
    # the correct password"), so nobody can enumerate account names; the 2009 client reads
    # 0x11 as "ID does not exist" and has its own 0x14 "Invalid password.", so a 2009 server
    # uses both (the spec's result table; it does reveal which account names exist).
    LOGIN_UNKNOWN_ID = {cfgmod.BUILD_2008: 0x11, cfgmod.BUILD_2009: 0x11}
    LOGIN_BAD_PASSWORD = {cfgmod.BUILD_2008: 0x11, cfgmod.BUILD_2009: 0x14}
    LOGIN_CORRUPTED = 0x0B     # "Login process was corrupted (RETURN)"
    LOGIN_BLANK = 0x0F         # "blank ID/password": a 2009 SSO field without a password
    # spec_2009 C2S 0x01 (live-verified layout): str[131] sso_account + u32 session_key.
    LOGIN_KEY_2009 = '0x451CE5/0x01'

    def _login_credentials(self, sock, session, payload):
        """(account, password, session_key) of a C2S 0x01 in the active build's layout, or
        None after the malformed login was answered (the client closes its socket on any
        result != 1).

        2008 (spec 0x44D8BF/0x01, 62 B): str[41] account_id + str[21] password from the
        login dialog, each cut at its first NUL; there is no session key (0).

        2009 (spec 0x451CE5/0x01, 135 B, live-verified): the first launcher (SSO) argument,
        131 raw bytes of scene+0x48, then the u32 scene+0x154 (0 on the first login, the key
        of the last S2C 0x02 on a relogin). The 2009 client has no ID/password form and
        never sends another argument, so the launcher's field 1 carries both (design
        decision, client-2009-login): WindSlayer_patched.exe -<account> <password> -x -x
        (play_2009.bat). The field is cut at its first NUL, stripped (the argument's trailing
        space survives on the wire) and split once on whitespace; a field without a password
        is a blank login (0x0F)."""
        if self.client_build != cfgmod.BUILD_2009:
            if len(payload) < 62:
                log.warning(f'[LOGIN] Too short: {len(payload)}B (need 62)')
                log.warning(f'[LOGIN] Raw:\n{hexdump(payload)}')
                # 0x0B "Login process was corrupted (RETURN)"; a 0x02 with result != 1 is
                # one byte (the grammar stops there) and the client closes the socket itself.
                P.send(self, sock, session, '0x02', {'result': self.LOGIN_CORRUPTED})
                return None
            username = payload[0:41].split(b'\x00')[0].decode('ascii', errors='replace')
            password = payload[41:62].split(b'\x00')[0].decode('ascii', errors='replace')
            return username, password, 0
        try:
            rec = P.parse(self.LOGIN_KEY_2009, payload, client_build=self.client_build)
        except P.ParseError:
            log.warning(f'[LOGIN] 2009 login is {len(payload)}B, not the 135B of '
                        f'{self.LOGIN_KEY_2009}; refused with 0x02 result 0x0B')
            P.send(self, sock, session, '0x02', {'result': self.LOGIN_CORRUPTED})
            return None
        field = P.to_bytes(rec.get('sso_account')).split(b'\x00', 1)[0].decode('latin-1')
        parts = field.strip().split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            # Never log the field itself: with one token it may be a bare password.
            log.info(f'[LOGIN] 2009 SSO field has no "<account> <password>" pair '
                     f'({len(parts)} token(s)); refused with 0x02 result 0x{self.LOGIN_BLANK:02X}')
            P.send(self, sock, session, '0x02', {'result': self.LOGIN_BLANK})
            return None
        return parts[0], parts[1].strip(), int(rec.get('session_key', 0)) & 0xFFFFFFFF

    def _new_session_key(self, uid):
        """A fresh non-zero u32 for the 2009 S2C 0x02 session_key (spec_2009 0x02, scene+0x154):
        the client echoes it in its next C2S 0x01, which is how a relogin is recognised.
        Differs from the account's previous key, so a stale client can never match."""
        old = self.session_keys.get(uid)
        key = 0
        while key == 0 or key == old:
            key = secrets.randbits(32)
        self.session_keys[uid] = key
        return key

    def _handle_login(self, sock, session, payload, no_enc=False):
        """
        Login request (C2S 0x01). 2008: spec 0x44D8BF/0x01, 62 B: str[41] account_id +
        str[21] password, each cut at its first NUL. 2009: spec 0x451CE5/0x01, 135 B:
        str[131] SSO field "<account> <password>" + u32 session_key (_login_credentials).

        S2C 0x02 result codes (login_character.md F2; lc-login-errors). Every failure
        closes the client socket itself, so the reply is one byte (the grammar stops after
        a result != 1):
          0x0B corrupted (RETURN)   payload shorter than the client ever sends
          0x0E maintenance          config MAINTENANCE (same flag as version_code 0xEA61)
          0x06 server full          logged-in accounts >= config MAX_ONLINE
          0x11 wrong password       unknown account OR wrong password - one code for both,
                                    so a stranger cannot enumerate account names (2008).
                                    2009: 0x11 "ID does not exist", 0x14 "Invalid password."
          0x0F blank (2009)         SSO field without a password
          0x05 blocked              account.banned
          0x13 account deleted      account.deleted
          0x04 already connected    a second login of an online account (the old session is
                                    closed first, F2 2f)
        Passwords are compared through the store (auth.verify: pbkdf2 hash at rest, a
        hand-edited plaintext record still works) and are never logged.

        2009 relogin (spec_2009 C2S 0x01 / S2C 0x02 session_key, 0x452133/0x2B): a login
        whose u32 key is non-zero and equals the account's live key (self.session_keys,
        issued by its last password login) is the client reconnecting by itself - after a
        channel change it reads only the 0x02 result byte and sends C2S 0x2B at once. The
        password is still verified (the SSO field is unchanged). An old session of that
        account is closed and REPLACED (no result 4) and the key stays the same, because on
        that path the client keeps scene+0x154 from the previous 0x02. The full list is sent
        anyway: gs+0x408 ignores it, the back-to-select path (gs+0x404) needs it. Any other
        key (0, stale, another account's) is a fresh login and gets a new key.
        """
        # If we're already logged in, this is a spurious mid-flow re-login —
        # likely triggered by the binary patch at 0x43ED30[0] falling through
        # to login flow. Responding with 0x02 would RESET the client to
        # character select, causing oscillation. Ignore instead.
        if session.get('username'):
            log.info(f'[LOGIN] Ignoring mid-flow 0x01 — already logged in as "{session["username"]}"')
            return

        creds = self._login_credentials(sock, session, payload)
        if creds is None:
            return
        username, password, key = creds
        log.info(f'[LOGIN] user="{username}" ({len(password)} password bytes)'
                 + (f' client {self.client_build} session_key=0x{key:08X}'
                    if self.client_build == cfgmod.BUILD_2009 else ''))

        if self.config.MAINTENANCE:
            log.info(f'[LOGIN] "{username}" refused: MAINTENANCE (0x02 result 0x0E)')
            P.send(self, sock, session, '0x02', {'result': 0x0E})
            return

        registered = False
        with self.store.lock:
            account = self.store.account(username)
            if account is None and self.config.AUTO_REGISTER and names.is_valid(username):
                # F2 2d exception: the account is created and the login continues. The name
                # rules are the character ones (ASCII, <= 16 B): enough to keep a login id
                # out of the str[41] field's ugly corners, and it becomes a JSON key.
                account = self.store.create_account(username, password, save=False)
                registered = True
                log.info(f'[LOGIN] AUTO_REGISTER created account "{username}" uid={account["uid"]}')
            known = account is not None
            ok = known and auth.verify(account.get('password'), password)
            uid = account.get('uid') if ok else None
            banned = bool(account.get('banned')) if ok else False
            deleted = bool(account.get('deleted')) if ok else False
        if registered:
            self._save_store_now(f'AUTO_REGISTER {username!r}')
        if not ok:
            # 2008: 0x11 "Enter the correct password" for an unknown account as well as a
            # wrong password (no account enumeration). 2009: 0x11 "ID does not exist" /
            # 0x14 "Invalid password." (LOGIN_UNKNOWN_ID / LOGIN_BAD_PASSWORD).
            table = self.LOGIN_BAD_PASSWORD if known else self.LOGIN_UNKNOWN_ID
            result = table.get(self.client_build, 0x11)
            log.info(f'[LOGIN] FAILED for "{username}" ({"wrong password" if known else "unknown account"}; '
                     f'0x02 result 0x{result:02X})')
            P.send(self, sock, session, '0x02', {'result': result})
            return
        if banned or deleted:
            result = 0x05 if banned else 0x13
            log.info(f'[LOGIN] "{username}" refused: {"banned" if banned else "deleted"} '
                     f'(0x02 result 0x{result:02X})')
            P.send(self, sock, session, '0x02', {'result': result})
            return
        online = len(self.world.by_uid)
        if self.world.session(uid) is None and online >= self.config.MAX_ONLINE:
            # A duplicate login of an account that is already counted replaces its session
            # instead of adding one, so it is never refused as "full".
            log.info(f'[LOGIN] "{username}" refused: {online} online >= MAX_ONLINE '
                     f'{self.config.MAX_ONLINE} (0x02 result 6)')
            P.send(self, sock, session, '0x02', {'result': 6})
            return

        # lc-uid-online (F3; login_character.md F2 step 2f/2g). The uid is the account's
        # persistent store uid: the registration gate at 0x4221A2 requires every own spawn
        # uid (entity+0x84) == scene+0x220, which the login-success handler (0x44DBB2)
        # writes from this account_id. It used to be 1 for every login, so a second client
        # registered the first one's records as its own local player (S1-04).
        # One session per account: a duplicate login closes the old session and refuses
        # this one with result 4 ("Connection already exists. Disconnecting existing
        # connection"); the client closes its socket and the user logs in again.
        #
        # 2009 relogin (see the docstring): the live key replaces the old session instead.
        with self.world_lock:
            relogin = (self.client_build == cfgmod.BUILD_2009 and key != 0
                       and self.session_keys.get(uid) == key)
            old = self.world.claim(session, uid, username, replace=relogin)
            if self.client_build == cfgmod.BUILD_2009 and (old is None or relogin):
                session['session_key'] = key if relogin else self._new_session_key(uid)
        if old is not None and relogin:
            self._kick(old, f'2009 relogin of {username!r} (uid {uid}) with its live session key '
                            f'from {session.get("addr")}')
        elif old is not None:
            self._kick(old, f'duplicate login of {username!r} (uid {uid}) from {session.get("addr")}')
            P.send(self, sock, session, '0x02', {'result': 4})
            log.info(f'[LOGIN] "{username}" is already online (uid {uid}): old session '
                     f'{old.get("addr")} closed, new login refused with 0x02 result 4')
            return
        if relogin:
            log.info(f'[LOGIN] "{username}" uid={uid} relogin with the live session key '
                     f'0x{key:08X}' + (f' (old session {old.get("addr")} replaced)' if old else ''))
        elif key and self.client_build == cfgmod.BUILD_2009:
            log.info(f'[LOGIN] "{username}" sent session key 0x{key:08X}, not the live one: fresh login')

        log.info(f'[LOGIN] SUCCESS for "{username}" uid={uid}')
        resp = self._build_login_success(session, account)
        self._send_encrypted(sock, session, 0x02, resp, use_by_array=no_enc)

    def _kick(self, session, reason):
        """Close a session's connection from another thread (duplicate login). It leaves the
        online indexes at once, its connection loop stops dispatching buffered requests,
        and the normal disconnect path (the _handle_fireway finally) cleans up and saves."""
        session['kicked'] = reason
        self.world.drop(session)
        if session.get('username'):
            self.store.mark_dirty(f'kick {session["username"]}')
        # The outbox closes before the socket: a tick still walking self.sessions (regen
        # 0x28/0x44, buffs) then gets the OSError it already handles at put(), instead of
        # queueing a packet whose write fails on the writer thread and logs the
        # dead-connection WARNING for what is a deliberate kick (world.Outbox._fail).
        outbox = session.get('outbox')
        if outbox is not None:
            outbox.close()
        sock = session.get('sock')
        if sock is not None:
            for close in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
                try:
                    close()
                except OSError:
                    pass
        log.info(f'[KICK] {session.get("username")!r} uid={session.get("uid")} at {session.get("addr")}: {reason}')

    def _compose_looks(self, account):
        """Migrate the stored look of every character of `account` to the composed one
        (inventory.Inventory.compose_all): a record written before the server composed the
        look still holds the creation outfit under its worn gear. Idempotent - a composed
        look replays to itself - so it runs before every 0x02, the first record of a session
        (the select screen, then the 0x07 at enter world). The caller holds the store lock."""
        changed = []
        for char in list((account or {}).get('characters') or [])[:R.MAX_CHARACTERS]:
            if not isinstance(char, dict):
                continue
            before = list(char.get('look') or [])
            gender = R.record_gender(char, account, self.client_build)
            if invmod.Inventory(char).compose_all(gender):
                changed.append(char.get('name'))
                log.info(f'[LOOK] {char.get("name")!r}: stored look {before} -> {char["look"]}'
                         f'{" + " + str(char["look_ext"]) if char.get("look_ext") else ""} '
                         f'(composed from the worn items, gender {gender})')
        if changed:
            self.store.mark_dirty(f'look composed for {", ".join(map(str, changed))}')
        return changed

    def _wear_starter(self, char, account, item):
        """STARTER_WEAPON: put `item` in its grid slot with a zero block and compose the look
        as the S2C 0x1D handler would (a new character has nothing to displace)."""
        bag = invmod.Inventory(char)
        slot, _displaced = bag.equip(item, invmod.ZERO_BLOCK)
        if slot is None:
            log.warning(f'[CREATE] STARTER_WEAPON {item} ("{EC.item_name(item)}") has no equip '
                        f'slot (Kind {getattr(en_item(item), "kind", None)}); not given')
            return None
        bag.recompose(item, en_item(item).spr_num, R.record_gender(char, account, self.client_build))
        # Born at the maxima (hpmp.new_character_vitals) - now with the weapon's bonuses.
        char['hp'], char['mp'] = hpmp.new_character_vitals(char)
        return slot

    def _build_login_success(self, session, account):
        """S2C 0x02 LoginResultCharacterList, result 1 (lc-charlist).

        Built from the store through records.character_list + the 0x02 grammar. What the
        hand-packed version got wrong (S2-23, S2-46, B5/B6/B8/B15):
          - `job_branch` (+0x111) held the LEVEL, so the select screen labelled a level-2
            character with the tier-2 class name and read past the 21-entry name table
            from level 3; it is the job tier, and the level is derived by the client from
            `total_exp` (which was always 0, hence "Lv.1" for everyone);
          - the appearance array was mis-slotted (constant 123 in slot 1, face/top/bottom/
            shoes in 2/4/5/6) instead of `char['look']` - the "renders unclothed" bug;
          - the three account flags were sent as 0: they are the cash first-purchase flag,
            the account gender (which gates every "(M)"/"(F)" item) and the manner i32;
          - 16 trailing bytes were appended after transfer_status; the client reads none.

        The client never frees the select-screen entities it already has (live verify
        login_character#03), so a second success 0x02 at character select duplicates them:
        only the login reply and the create reply (CREATE_REPLY_0x02, lc-create) may send
        this, plus the unknown-name 0x2B refusal, which reloads the select screen anyway.
        """
        uid = P.session_uid(session) or (account or {}).get('uid') or 0
        # shop_storage F14.5: a stall escrow left in a record (the server stopped while that
        # character was selling, or a full bag kept part of it) goes back into the bag before
        # the first record of the session is built (Market.lock before store.lock).
        market = getattr(self, 'market', None)
        if market is not None:
            market.recover(account)
        with self.store.lock:
            self._compose_looks(account)
            stored = len((account or {}).get('characters') or [])
            # 2009: 82-byte records (per-character gender, 17 look words) and the header's
            # session_key (spec_2009 0x02), which the client echoes on its next login.
            fields = R.character_list(uid, account, client_build=self.client_build,
                                      session_key=session.get('session_key', 0))
        if stored > fields['char_count']:
            # The select screen has 5 slots (window 0x12 refuses a 6th); an older file can
            # hold more. lc-create enforces the cap on the way in.
            log.warning(f'[LOGIN] account uid {uid} has {stored} characters: only the first '
                        f'{fields["char_count"]} are sent')
        body = P.build('0x02', fields, client_build=self.client_build)
        # The first-purchase popup this 0x02 owes: the next 0x6A shows it and clears it
        # client-side, and Mall.enter then clears the account's flag (premium_cash.md 1.1).
        session['first_purchase_popup_owed'] = bool(fields.get('cash_first_purchase_flag'))
        gender = fields.get('account_gender_flag', fields.get('account_gender_byte'))
        log.info(f'[LOGIN] 0x02 character list: {len(body)}B, {fields["char_count"]} char(s), '
                 f'account_id={uid} gender={gender} manner={fields["manner_points"]}'
                 + (f' session_key=0x{fields["session_key"]:08X}' if 'session_key' in fields else ''))
        return body


# ============================================================================
# Main
# ============================================================================

def main():
    setup_logging()
    cfg = cfgmod.load()
    log.info('=' * 60)
    log.info(f'  WindSlayer Private Server ({cfg.WORLD_NAME})')
    log.info('=' * 60)
    log.info(f'  Client build:   {cfg.CLIENT_BUILD} (spec {os.path.basename(P.spec_path(cfg.CLIENT_BUILD))}, '
             f'client data {os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg.client_dir()))})')
    log.info(f'  Config:         {cfg.path}')
    log.info(f'  Version Server: {cfg.BIND_HOST}:{cfg.VERSION_PORT} (game IP {cfg.PUBLIC_IP})')
    log.info(f'  Game Server:    {cfg.BIND_HOST}:{cfg.GAME_PORT} (Fireway) '
             + ('[client connects to 7022]' if cfg.CLIENT_BUILD == cfgmod.BUILD_2008
                else '[2009 client connects to the channel entry port = GAME_PORT]'))
    log.info(f'  Admin port:     127.0.0.1:{cfg.ADMIN_PORT}')
    log.info(f'  Accounts:       {cfg.accounts_path}')
    log.info('=' * 60)

    # NOTE: UDP port 42907 is the CLIENT's own local bind (CSNSocket::Create).
    # We must NOT bind it here or the client's socket init fails.

    gs = GameServer(host=cfg.BIND_HOST, port=cfg.GAME_PORT, config=cfg)

    # Port 7022 is HARDCODED in the English client's ConnectToGameServer function
    # at VA 0x44080E (push 0x1B6E = 7022). The version response carries only the IP.
    # The game server is built first so the channel table can report its live user count
    # (lc-version-config); it binds nothing until gs.start() below.
    vs = VersionServer(host=cfg.BIND_HOST, port=cfg.VERSION_PORT, config=cfg,
                       user_counts=gs.channel_user_counts)
    threading.Thread(target=vs.start, daemon=True).start()

    try:
        gs.start()
    except KeyboardInterrupt:
        gs.store.flush()
        gs.bosses.flush()
        log.info('Server stopped.')

if __name__ == '__main__':
    main()
