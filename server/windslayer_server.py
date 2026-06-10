"""
WindSlayer Private Server
========================
Reverse-engineered server for WindSlayer English client (~2008).

Architecture:
  - Version Server (TCP 7011): Send version response + channel list, close.
    After close, client shows launcher. User clicks Start → connects to 7012.

  - Game Server (TCP 7012): Full game protocol (Fireway).
    1. Server sends key exchange (opcode 0x5A, seq=1, NoEncode flag)
    2. Client calls SetCodeKey(seed) - all further packets encrypted
    3. Client sends login (opcode 0x01, encrypted)
    4. Server sends login response (opcode 0x02, encrypted)
    5. Character select, enter game, etc.
"""

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
import sqlite3
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from cencmsg import CEncMsg

# Reference data tables built from KR Yahoo client dumps. Lazy-loaded; safe
# to import even if a JSON is missing (will raise on first use only).
from data import items as item_table, npcs as npc_table, map_codes, map_filename


# ============================================================================
# Content DB (gamedef.sqlite3): items.json lacks Kind/Spr_Num/Buy/Sell which
# equip + shop pricing need; source them here. Lazy connection; cached.
# ============================================================================
_GAMEDEF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gamedef.sqlite3')
if not os.path.exists(_GAMEDEF_PATH):
    _GAMEDEF_PATH = r'C:\Users\ohdri\Desktop\PySlayer\gamedef.sqlite3'
_gamedef_conn = None
_gamedef_cache = {}

def _gamedef_item(idx):
    """items columns: Type,Kind,Sprite(icon),Spr_Num(body sprite),HP,MP,Buy,Sell,W_Att,Def,name. None if missing."""
    global _gamedef_conn
    if idx in _gamedef_cache:
        return _gamedef_cache[idx]
    try:
        if _gamedef_conn is None:
            _gamedef_conn = sqlite3.connect(_GAMEDEF_PATH, check_same_thread=False)
            _gamedef_conn.row_factory = sqlite3.Row
        row = _gamedef_conn.execute(
            'SELECT Type,Kind,Sprite,Spr_Num,HP,MP,Buy,Sell,W_Att,Def,name FROM items WHERE idx=?',
            (int(idx),)).fetchone()
        info = dict(row) if row else None
    except Exception as e:
        info = None
    _gamedef_cache[idx] = info
    return info

# gamedef Kind -> char-dict appearance key that _build_en_opcode_07 renders.
_KIND_TO_APPARENCE = {11: 'weapon', 6: 'top', 5: 'bottom', 9: 'shoes', 15: 'head', 16: 'head', 10: 'head'}


# ============================================================================
# PHASE 1 COMBAT — server-authoritative monster model + content tables.
# The client only renders what 0x1A (spawn) / 0x14 (hp) / 0x29 (death) tell it.
# ============================================================================
@dataclass
class Monster:
    uid: int                 # entity uid; MOB_UID_BASE+index (never collides with player uid=1)
    npccode: int             # -> entity +0xe60 anim index (must be a client-loaded anim record to render)
    name: str
    level: int
    hp: int
    max_hp: int
    body_atk: int            # monster melee attack (for when mobs hit back, later)
    defense: int             # subtracted from incoming player damage
    exp: int                 # exp granted to killer
    x: float
    y: float
    spawn_x: float = 0.0
    spawn_y: float = 0.0
    alive: bool = True
    aggro_uid: int = 0
    drop_items: list = field(default_factory=list)
    gold: int = 0
    respawn_at: float = 0.0

# Popola-region monster stats (gamedef.sqlite3 npcs, type=3 = AI monster).
# npccode 1 (Seeyo) is the proven-rendering choice placed in map 102.
MONSTER_DB = {
    1:  dict(name='Seeyo',      level=1, hp=5,   atk=3,  defense=2,  exp=10,
             drops=[3,4,5,18,19,20,21,47,48,2030,4356], gold=7),
    2:  dict(name='Coring',     level=2, hp=10,  atk=4,  defense=5,  exp=14,
             drops=[3,4,8,23,30,33,38,40,47,48,2031,4356], gold=10),
    3:  dict(name='Oroling',    level=3, hp=15,  atk=6,  defense=8,  exp=20,
             drops=[4,8,14,19,25,27,29,32,34,38,40,41,47,48,2032,4356], gold=14),
    4:  dict(name='Bbonyang',   level=5, hp=30,  atk=9,  defense=13, exp=32,
             drops=[2,4,5,8,39,47,103,210,2033,4356], gold=20),
    5:  dict(name='Poko',       level=6, hp=50,  atk=11, defense=18, exp=40,
             drops=[2,8,13,39,47,48,103,211,2034,4356], gold=25),
    6:  dict(name='Kamikaji',   level=5, hp=30,  atk=9,  defense=13, exp=32,
             drops=[2,3,5,7,39,47,51,209,2035,1570,4356], gold=20),
    12: dict(name='Shiryosoni', level=8, hp=174, atk=18, defense=31, exp=112,
             drops=[2036,1241,5,51], gold=60),
}

# Spawn layout from the client map files. 101 (town) = none; 102 (field) = 8 Seeyo.
MAP_SPAWNS = {
    101: [],
    102: [
        (1, 1239.0, 411.0), (1, 1194.0, 702.0), (1, 1494.0, 702.0), (1, 700.0, 900.0),
        (1, 1300.0, 900.0), (1, 2100.0, 800.0), (1, 100.0, 485.0), (1, 1100.0, 530.0),
    ],
}
MOB_UID_BASE = 0x000F0000
MOB_RESPAWN_SECS = 15.0

# Quests (content from quest_defs/gamedef.sqlite3). Client active log = 3 slots.
from quest_defs import load_quests
MAX_ACTIVE_QUESTS = 3


logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('server_live.log', mode='w', encoding='utf-8')
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
    After close, launcher shows start button. Clicking Start connects to 7012.
    """

    def __init__(self, host='0.0.0.0', port=7011, game_port=7012):
        self.host = host
        self.port = port
        self.game_port = game_port

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

    def _build_version_body(self):
        """
        Version response body.

        The ULONG field at [this+0x129C] is the IP address used by
        ConnectToGameServer (VA 0x440805-0x440814). Port is hardcoded to 7022.
        So we send 127.0.0.1 as inet_addr (network byte order in the DWORD).
        """
        body = bytearray()
        body.append(0x01)                           # opcode = version response
        body.extend(struct.pack('<H', 3))            # result_code = 3

        message = b'Press start button to start the game.\n\n\xA9 2009 OUTSPARK.com. All rights reserved.'
        body.extend(struct.pack('<H', len(message)))
        body.extend(message)

        # Channel data (parsed by 0x4404D0)
        num_channels = 1
        body.append(0x01)          # server count
        body.append(0x03)          # server status
        body.append(num_channels)  # channel limit
        body.append(num_channels)  # channel count

        # IP is passed through htonl() by Fireway Connect (VA 0x10001CA0).
        # So the stored ULONG must be in HOST byte order.
        # 127.0.0.1: a=127, b=0, c=0, d=1
        # In host order: 0x7F000001 -> little-endian bytes: 01 00 00 7F
        game_ip_host_order = 0x7F000001  # 127.0.0.1 in host order
        game_ip_bytes = struct.pack('<I', game_ip_host_order)

        for i in range(num_channels):
            body.append(i + 1)                       # channel number
            body.extend(struct.pack('<H', 0))        # player count
            body.extend(game_ip_bytes)               # IP (ULONG in host order; client htonl's it)
        return bytes(body)

    def _handle(self, sock, addr):
        try:
            body = self._build_version_body()
            pkt = make_raw_packet(body, seq=1)
            log.info(f'[VERSION] Sending version response ({len(pkt)} bytes)')
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
# Game Server (port 7012) - Fireway protocol + HTTP fallback
# ============================================================================

class GameServer:
    """
    Port 7012. Full game protocol (Fireway).

    Flow:
      1. Server sends key exchange (opcode 0x5A, seq=1, NoEncode flag)
         Payload: INT32 encryption seed
      2. Client calls SetCodeKey(seed) - all further packets encrypted
      3. Client sends login (opcode 0x01, encrypted)
         Payload: CHAR[41] username + CHAR[21] password
      4. Server sends login response (opcode 0x02, encrypted)
      5. Character select, enter game, etc.
    """

    def __init__(self, host='0.0.0.0', port=7012):
        self.host = host
        self.port = port
        self.sessions = {}

        self.db_file = 'accounts.json'
        self.accounts = self._load_accounts()

    def _load_accounts(self):
        if os.path.exists(self.db_file):
            with open(self.db_file, 'r') as f:
                return json.load(f)
        default = {
            'test': {
                'password': 'test',
                'characters': [
                    {'name': 'TestHero', 'level': 1, 'class': 1, 'map': 0,
                     'x': 100, 'y': 100, 'hp': 100, 'mp': 50,
                     'hair': 1, 'face': 1, 'uid': 1}
                ]
            },
            'admin': {
                'password': 'admin',
                'characters': []
            }
        }
        self._save_accounts(default)
        return default

    def _save_accounts(self, accounts=None):
        if accounts is None:
            accounts = self.accounts
        with open(self.db_file, 'w') as f:
            json.dump(accounts, f, indent=2)

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(10)
        log.info(f'[GAME] Listening on {self.host}:{self.port}')

        while True:
            try:
                client, addr = srv.accept()
                log.info(f'[GAME] Connection from {addr}')
                threading.Thread(target=self._handle, args=(client, addr), daemon=True).start()
            except Exception as e:
                log.error(f'[GAME] Accept error: {e}')

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

    def _handle_fireway(self, sock, addr):
        """Handle a Fireway game connection."""
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
                'account_id': 0,
            }
            self.sessions[addr] = session

            # Step 3: Read encrypted client packets - also log any raw bytes received
            sock.settimeout(120.0)
            sock_buffer = bytearray()
            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    # Keep-alive: don't drop an idle in-world client. (Was break;
                    # the 120s drop kept closing the client mid-debug.)
                    continue
                except Exception as e:
                    log.info(f'[FIREWAY] recv error: {e}')
                    break

                if not chunk:
                    log.info(f'[FIREWAY] {addr} closed connection (after {len(sock_buffer)}B recv)')
                    if sock_buffer:
                        log.info(f'[FIREWAY] Bytes before close:\n{hexdump(bytes(sock_buffer))}')
                    break

                sock_buffer.extend(chunk)
                log.info(f'[FIREWAY] Got {len(chunk)}B from client (total {len(sock_buffer)}B):\n{hexdump(bytes(chunk))}')

                # Try to parse any complete packets
                while len(sock_buffer) >= HEADER_SIZE:
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
                    log.info(f'[FIREWAY] Pkt: opcode=0x{opcode:02X} size={ps} seq={seq} no_enc={no_enc} payload={len(payload)}B')
                    self._dispatch(sock, session, opcode, payload, no_enc)

        except Exception as e:
            log.error(f'[FIREWAY] Error for {addr}: {e}')
            traceback.print_exc()
        finally:
            if addr in self.sessions:
                del self.sessions[addr]
            sock.close()

    def _send_encrypted(self, sock, session, opcode, payload=b'', use_by_array=False):
        """Send a packet. Use EncodebyArray if use_by_array=True (for pre-login packets)."""
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
            sock.sendall(bytes(pkt))

    def _dispatch(self, sock, session, opcode, payload, no_enc=False):
        if opcode == 0x01:
            self._handle_login(sock, session, payload, no_enc)
        elif opcode == 0x0E:
            self._handle_create_character(sock, session, payload, no_enc)
        elif opcode == 0x0D:
            self._handle_world_sync(sock, session, payload, no_enc)
        elif opcode == 0x2B:
            self._handle_enter_world(sock, session, payload, no_enc)
        elif opcode == 0x63:
            self._send_encrypted(sock, session, 0x63, b'', use_by_array=no_enc)
        elif opcode == 0x03:
            # CHAT — echo it as a server announcement (opcode 0x16)
            self._handle_chat(sock, session, payload, no_enc)
        elif opcode == 0x04:
            # SET STATS — increase a stat
            self._handle_set_stats(sock, session, payload, no_enc)
        elif opcode == 0x0B:
            # BUY ITEM — respond with 0x18 (got item)
            self._handle_buy_item(sock, session, payload, no_enc)
        elif opcode == 0x0C:
            # SELL ITEM — respond with 0x19 (lost item)
            self._handle_sell_item(sock, session, payload, no_enc)
        elif opcode == 0x15:
            # USE ITEM/SKILL — respond with HP/MP delta
            self._handle_use_item(sock, session, payload, no_enc)
        elif opcode == 0x7E:
            # CHANGE MAP via portal
            self._handle_change_map(sock, session, payload, no_enc)
        elif opcode == 0x2F:
            log.info(f'[0x2F] arena query — ignoring')
        elif opcode == 0x05:
            # client heartbeat / periodic keepalive (~4 min). Consume, NO reply
            # (replying with the 0x05 init packet resets in-game state -> kick).
            log.info('[0x05] client heartbeat - consumed')
        elif opcode == 0x0F:
            self._handle_equip_item(sock, session, payload, no_enc)
        elif opcode == 0x16:
            # ACCEPT QUEST (inbound). 0x16 is ALSO the outbound chat opcode.
            self._handle_accept_quest(sock, session, payload, no_enc)
        elif opcode == 0x1A:
            # mob/target query (inbound). Outbound 0x1A is the mob-spawn builder.
            log.info('[0x1A] mob query - consumed')
        elif opcode == 0x25:
            # UseSkill / basic-attack (W key): client-authoritative hit list ->
            # server resolves damage, broadcasts HP/death/exp/drop.
            self._handle_attack(sock, session, payload, no_enc)
        elif opcode == 0x2C:
            self._handle_room_query(sock, session, payload, no_enc)
        elif opcode == 0x2D:
            # scene-finalize / map-ready ack (inbound). Consume (replying froze
            # the client in a prior experiment).
            log.info('[0x2D] scene-finalize ack - consumed')
        else:
            log.info(f'[FIREWAY] Unhandled opcode 0x{opcode:02X}')
            try:
                text = payload.decode('ascii', errors='replace')
                readable = ''.join(c if c.isprintable() else '.' for c in text)
                if any(c.isalpha() for c in readable):
                    log.info(f'[FIREWAY] Text: {readable}')
            except:
                pass

    def _handle_world_sync(self, sock, session, payload, no_enc=False):
        """0x0D = local-player MOVEMENT (sent on every arrow-key step), NOT a
        heartbeat. On a real server it is rebroadcast to OTHER players so they
        see you move; the SENDER must NOT receive it back. The previous code
        echoed it (MT-encrypted), which desynced the client's cipher stream into
        garbage that the client parsed as random opcodes -> spurious menus,
        'death', and loading-screen/map-warp. With no other players in the world
        we simply consume it. (To support multiplayer later: broadcast this
        payload to every OTHER session in the same map, not to `session`.)"""
        # Optionally track the position server-side here. No response to sender.
        return

    def _handle_enter_world(self, sock, session, payload, no_enc=False):
        """
        Enter-world request (opcode 0x2B from client).
        Client payload (42 bytes after opcode):
          [0-4]   zeros (5 bytes)
          [5-13]  "10.5.0.2\0" (9 bytes)
          [14-20] zeros (7 bytes)
          [21-22] 0xA79B magic (2 bytes)
          [23-24] zeros (2 bytes)
          [25-41] character name (17 bytes)

        Response format (derived from handler at VA 0x4503E1 - see
        enter_world_fields.md). Total 316 bytes after opcode.
        """
        log.info(f'[ENTER_WORLD] Raw payload ({len(payload)}B):\n{hexdump(payload)}')
        char_name = b''
        if len(payload) >= 42:
            char_name = payload[25:42].split(b'\x00')[0]
        char_name_str = char_name.decode('ascii', errors='replace')
        log.info(f'[ENTER_WORLD] Character "{char_name_str}"')

        username = session.get('username')
        account = self.accounts.get(username, {})
        char = None
        for c in account.get('characters', []):
            if c.get('name') == char_name_str:
                char = c
                break

        if not char:
            log.warning(f'[ENTER_WORLD] Character "{char_name_str}" not found')
            return

        # Persist identity so portal/map-change (0x7E) can find the character
        # and use the correct source map for portal lookups.
        session['char_name'] = char_name_str
        session['current_map'] = char.get('map', 101) or 101

        # CRITICAL: respond with opcode 0x2E (not 0x2B)! Opcodes 0x2B and 0x2E
        # share the handler at VA 0x44F762, but the post-read check at
        # VA 0x44FCB1 (`cmp byte [esp+0x43], 0x2E`) only triggers the scene
        # transition helpers (0x442EE0, 0x443280, 0x443690, 0x443780) when the
        # received opcode was 0x2E. The sub_opcode byte at payload[0] is the
        # LOOP COUNT (must be > 0). Count=1 means one character state follows.
        # 2026-04-25: PySlayer flow — send 0x03 (in-game state) + 0x07 (spawn)
        # in response to client's 0x2B. This is what the real Korean Yahoo
        # server does. Replaces our previous 0x2E + 0x2B combo.
        resp_03 = self._build_pyslayer_opcode_03(session, char)
        log.info(f'[ENTER_WORLD] Sending 0x03 in-game state: {len(resp_03)}B')
        self._send_encrypted(sock, session, 0x03, resp_03, use_by_array=no_enc)

        # 2026-04-26 Phase 4 step 6 (EN deep-RE workflow output):
        # Use _build_en_opcode_07_packet — a BYTE-PERFECT spawn packet matching
        # the EN handler 0x44EF72's exact read sequence. Critical fix: positions
        # are i32 (not f64), apparences is 14 (not 17), equipment is 16 (not
        # 15), extra_equip is 9 u16 (not 10×3 i16), and marriage/pvp flags are
        # u16 (not u8). The i32-vs-f64 alone caused 16 bytes of misalignment
        # that walked HP off into garbage.
        # ALSO: removed the 0x2B follow-up. Both 0x44F762 (0x2B) and 0x44EF72
        # (0x07) write to the SAME per-player struct — the padded 2000-byte
        # 0x2B was scribbling ~1900 zero bytes through the alive flag, HP, MP,
        # and apparences that 0x07 had just set correctly. The new 0x07 builder
        # sets the active/alive bit inline (last action bool = 1).
        time.sleep(0.05)
        body_07 = self._build_en_opcode_07_packet(session, char)
        log.info(f'[ENTER_WORLD] Sending 0x07 EN player-spawn (byte-perfect): {len(body_07)}B')
        self._send_encrypted(sock, session, 0x07, body_07, use_by_array=no_enc)

        # 2026-06-09 Phase 4 step 7 REVERTED: tried adding 0x08 CHANGE_MAP here
        # to commit the spawn into render/input pipeline. INSTEAD it caused the
        # client to NOT TRANSITION TO IN-GAME at all (no heartbeats arrived
        # after 0x2F+0x63 and client closed the connection in 16s). Memory was
        # right: "PySlayer doesn't send 0x08 on enter-world; only on client-
        # initiated change. The 0x03's mapcode handles initial map load."
        # Sending 0x08 here triggers a re-map-load loop on the EN client.

        # 2026-04-25: PySlayer follows up 0x07 with a welcome chat (0x0A) —
        # this may be the trigger that finalizes the in-game transition.
        time.sleep(0.05)
        chat = b'Welcome to WindSlayer!'
        sender = b'Server'
        body_0A = struct.pack('<B', 1) \
                + sender + b'\x00' * (17 - len(sender)) \
                + struct.pack('<B', len(chat)) \
                + chat
        log.info(f'[ENTER_WORLD] Sending 0x0A welcome chat: {len(body_0A)}B')
        self._send_encrypted(sock, session, 0x0A, body_0A, use_by_array=no_enc)

        # Phase 1 combat: populate the map with monsters once in-world.
        time.sleep(0.05)
        self._spawn_map_monsters(sock, session, session.get('current_map', 101) or 101, no_enc=no_enc)

        # 2026-06-09 Phase 4 step 8 — RENDER + INPUT ACTIVATION (opcode 0x6F).
        # Four-way RE investigation converged on this as the missing trigger:
        #
        # 1) Keyboard handler 0x00487DB0 drops input when [ebx+0x5A0] == 0.
        #    The ONLY writer of [+0x5A0]=1 is fn 0x0045AF00, called from
        #    EXACTLY ONE site (0x00461F29) at the end of opcode 0x6F's case
        #    in dispatcher 0x461890. → without 0x6F, arrow keys produce zero
        #    client→server packets (matches observed symptom).
        # 2) Sprite renderer sub_0x4305B0 returns if [esi+0x970] == 0. The
        #    only writer of [char+0x970] (sub_0x444100) requires [+0x98]==4,
        #    but our 0x07 handler writes 3. 0x6F's tail (call 0x45C470) is
        #    the sprite-render activation handshake.
        # 3) The 35-second loading-screen transition is TIMER_RECEIVELOADED
        #    re-arming because the client never reaches "in-game ready".
        #    0x6F completes that state.
        #
        # Body: 28 zero bytes — the case_0x6F at 0x004618EC does a fixed
        # `rep movsd 7 dwords` (= 28 B) into [ebx+0x58..0x73] (a scratch
        # slot), then [ebx+0x5B8]=0, then calls 0x45AF00 (activation) +
        # 0x45C470 (final commit). The activation runs UNCONDITIONALLY after
        # the copy regardless of body content.
        # 2026-06-09 DISABLED 0x6F/0x19: these were built on the (now-disproven)
        # theory that our 0x07 entity just needed an input/sprite "activation".
        # Full RE shows 0x07 hardcodes alive=3 (remote player) and never sets
        # +0x11dc; the local player is a separate spawn (0x1A). 0x6F/0x19 act on
        # scene+0x970 (the local player) which doesn't exist yet, so they no-op
        # or destabilise. Re-test corrected 0x07 (valid appearance + f64 pos)
        # for VISIBILITY first; local-player/control handled separately.
        if False:
            time.sleep(0.05)
            body_6F = b'\x00' * 28
            log.info(f'[ENTER_WORLD] Sending 0x6F render+input activation: {len(body_6F)}B')
            self._send_encrypted(sock, session, 0x6F, body_6F, use_by_array=no_enc)

        # 2026-06-09 Phase 4 step 9 — SPRITE ACTIVATION (opcode 0x19).
        # After step 8, input was bound (arrow keys send packets) but sprite
        # still invisible — the partial-success path the workflow predicted.
        # Per find_render_activation: handler 0x451D7E (case 0x19) reaches
        # 0x451E24 which writes `[entity+0x98] = 4` DIRECTLY. The renderer's
        # local-player pointer setter (sub_0x444100) requires `[+0x98]==4`
        # AND `[+0x84]==session uid` before it sets `[char+0x970] = entity`,
        # which the renderer dereferences for the draw call. Our 0x07 only
        # writes 3 to [+0x98] (hardcoded in 0x44EF72), so the renderer's
        # entity-lookup fails. Sending 0x19 with body matching the atlas's
        # read pattern (u8 + u32 + u32 = 9 bytes) bumps [+0x98] to 4.
        if False:
            time.sleep(0.05)
            uid = session.get('account_id', 1) & 0xFFFFFFFF
            body_19 = struct.pack('<B', 0) + struct.pack('<I', uid) + struct.pack('<I', 0)
            log.info(f'[ENTER_WORLD] Sending 0x19 sprite activation (state=4): {len(body_19)}B')
            self._send_encrypted(sock, session, 0x19, body_19, use_by_array=no_enc)

        # 2026-04-24: UDP map-server simulator (20 packets opcode 0x11 to
        # 127.0.0.1:42907) caused cascading UI errors — client parsed each
        # packet as a different event (first-purchase bonus, name-taken,
        # etc.). Reverted. Opcode 0x11 is NOT the map-server response.
        # Left _fake_map_server method below for future experiments.

        # 2026-04-23: tried sending opcode 0x2C(map_id=1) after 0x2E to trigger
        # the "finalize scene" path (handler VA 0x4507EE → 0x4305B0). Client
        # accepted the packet but went silent — heartbeat count dropped from
        # ~5-7 to 1 within 30s, then client froze. Reverted. The validation
        # path probably hit some state that put the client in a dead wait.

        # NOTE: tried sending 0x03 to trigger KillTimer(hwnd, 2) at VA 0x44E3CB
        # but it froze the client. Better to rely on the binary patch (jump
        # table entry 0x43ED30[0] → 0x43E9DB) to neutralize the "No response"
        # code path.

        # No periodic keepalive — the 0x02 handler RESETS game state (creates
        # dialog manager entries, etc), so sending it in the world kicks
        # player back to login. Just rely on the initial 0x02 after 0x2E.

    def _fake_map_server(self, client_ip, account_id):
        """
        Send speculative UDP map-server response packets to 127.0.0.1:42907.
        Client broadcasts 0x11+account_id after receiving our 0x2B and waits
        for a map-server response. We fake that response here.

        Format guess: standard Fireway packet (8-byte header + body),
        NoEncode+EncodebyArray encoding (same as the TCP pre-login packets).
        Body = opcode + account_id echo + (unknown map data, try zeros).
        """
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.settimeout(1.0)

            # Build packet: opcode 0x11 + account_id echo + 64 bytes of zeros
            body = struct.pack('<B', 0x11) + struct.pack('<I', account_id) + b'\x00' * 64
            pkt = make_raw_packet(body, seq=1, no_encode=True)
            enc = CEncMsg()
            enc.encode_by_array(pkt)

            log.info(f'[UDP_FAKE] Map-server response ({len(pkt)}B) -> {client_ip}:42907:')
            log.debug(f'[UDP_FAKE] bytes:\n{hexdump(bytes(pkt))}')

            # Send 20 times over 10 seconds to cover timing window
            for i in range(20):
                try:
                    udp.sendto(bytes(pkt), (client_ip, 42907))
                except Exception as e:
                    log.warning(f'[UDP_FAKE] sendto failed: {e}')
                    break
                time.sleep(0.5)
            log.info('[UDP_FAKE] Done sending.')
        except Exception as e:
            log.error(f'[UDP_FAKE] error: {e}')
        finally:
            try: udp.close()
            except: pass

    def _build_map_enter_packet(self, char):
        """
        Opcode 0x2E (Map/Channel Enter) payload.
        Handler reads 14 fields before creating 0x47E entry at 0x450F7E.
        Field [char+0xf70] (offset 29 in payload) MUST be 0-3 to reach the creation.

        Wire layout (47 bytes):
          UCHAR   -> char+0xf36  (map flag?)
          UCHAR   -> char+0xf37
          USHORT  -> local       (unknown)
          CHAR[16]-> char+0xf38  (map name / resource name?)
          USHORT  -> char+0xf48
          UCHAR   -> char+0xf5b
          UCHAR   -> char+0xf5c
          UCHAR   -> char+0xf5d
          UCHAR   -> char+0xf5e
          UCHAR   -> char+0xf5f
          UCHAR   -> char+0xf72
          UCHAR   -> char+0xf71
          UCHAR   -> char+0xf70  <- MUST be 0-3
          CHAR[17]-> char+0xf4a  (character name)
        """
        body = bytearray()
        body.append(0)                              # 0xf36
        body.append(0)                              # 0xf37
        body.extend(struct.pack('<H', 0))           # local USHORT
        map_name = b'town\x00' + b'\x00' * 11        # 16 bytes
        body.extend(map_name)                       # 0xf38
        body.extend(struct.pack('<H', 0))           # 0xf48
        body.append(0)                              # 0xf5b
        body.append(0)                              # 0xf5c
        body.append(0)                              # 0xf5d
        body.append(0)                              # 0xf5e
        body.append(0)                              # 0xf5f
        body.append(0)                              # 0xf72
        body.append(0)                              # 0xf71
        body.append(0)                              # 0xf70 - CRITICAL: 0-3
        name = char.get('name', 'Hero').encode('ascii', errors='replace')[:16]
        body.extend(name + b'\x00' * (17 - len(name)))  # 0xf4a CHAR[17]
        return bytes(body)

    # =================================================================
    # In-game packet handlers (ported from PySlayer game_server.py)
    # =================================================================

    def _handle_chat(self, sock, session, payload, no_enc):
        """0x03 CHAT from client → echo as 0x16 announcement."""
        if len(payload) < 1: return
        text = payload[1:].split(b'\x00')[0].decode('ascii', errors='replace')
        username = session.get('username', 'Player')
        log.info(f'[CHAT] {username}: {text}')
        # Build opcode_16 (chat broadcast) per PySlayer: name(17) + len + text
        name_bytes = username.encode('ascii', 'replace')[:16].ljust(17, b'\x00')
        msg_bytes = text.encode('ascii', 'replace')[:255]
        body = struct.pack('<B', 1) + name_bytes + struct.pack('<B', len(msg_bytes)) + msg_bytes
        self._send_encrypted(sock, session, 0x16, body, use_by_array=no_enc)

    def _handle_set_stats(self, sock, session, payload, no_enc):
        """0x04 client increased a stat → respond with 0x14 stat update."""
        if len(payload) < 1: return
        stat_type = payload[0]
        # opcode_14: uid(4) + stat_type(1) + value(2)
        uid = session.get('account_id', 1) & 0xFFFFFFFF
        # Just echo a +1 value for now (real server would track)
        body = struct.pack('<I', uid) + struct.pack('<B', stat_type) + struct.pack('<H', 4)
        log.info(f'[STATS] type={stat_type} +1')
        self._send_encrypted(sock, session, 0x14, body, use_by_array=no_enc)

    # =================================================================
    # SHOP / ECONOMY (opcodes 0x0B,0x0C inbound; 0x18,0x19,0x80 outbound)
    #
    # Wire formats verified against the EN client handlers reached via
    # dispatcher 0x004582B3, cross-checked with PySlayer:
    #
    #   0x0B BuyItem  (client->server)  : u16 item, u16 count, u8 undef
    #       (client SEND builder; the client's *receive* handler 0x0047091D is
    #        the shop-inventory listing — a separate, deeper feature, TODO.)
    #   0x0C SellItem (client->server)  : u16 item, u8 count, u16 undef
    #       (PySlayer client_packets/parse_0x0C.py: count is a U8, not U16.)
    #   0x18 GetItem/BuyResult (server->client), handler 0x0046DE3E:
    #       u64 gold, u32 winnie(cash), u16 item, u16 count   (16 B payload)
    #   0x19 SellResult        (server->client), handler 0x0046DBEE:
    #       u64 gold, u16 item, u16 count, u8 loopN, loopN*u16, u16 trailer
    #       (we send loopN=0 -> 15 B payload, matching PySlayer opcode_0x19)
    #   0x80 CashShopBalance   (server->client), handler 0x0044E2FE:
    #       u8 flag(=1), u32 value   (5 B payload; flag!=1 pops an error box)
    #
    # The economy is a protocol-correct stub: the server has no persistent
    # gold/inventory yet, so it always reports a large balance and grants/
    # removes the requested item so the client UI stays in sync. Hook real
    # gold/inventory accounting where marked TODO.
    # =================================================================

    # Default wallet values reported to the client until real accounting lands.
    _DEFAULT_GOLD = 999999
    _DEFAULT_WINNIE = 999999   # cash/premium currency

    def _wallet(self, session):
        """Return (gold, winnie) for this session. TODO: persist per account."""
        return (session.get('gold', self._DEFAULT_GOLD),
                session.get('winnie', self._DEFAULT_WINNIE))

    def _handle_buy_item(self, sock, session, payload, no_enc):
        """0x0B BUY (client->server) → respond 0x18 (item granted + wallet).

        Payload: u16 item, u16 count, u8 undef (5 B). Mirrors EN client
        builder 0x0047... and PySlayer parse_0B.
        """
        if len(payload) < 4:
            log.warning(f'[BUY] short payload {len(payload)}B')
            return
        item = struct.unpack_from('<H', payload, 0)[0]
        count = struct.unpack_from('<H', payload, 2)[0] or 1
        info = _gamedef_item(item)
        if info is None:
            log.warning(f'[BUY] unknown item {item}'); return
        gold, winnie = self._wallet(session)
        cost = int(info.get('Buy') or 0) * count
        if gold < cost:
            log.info(f'[BUY] item={item} x{count} cost={cost} > gold={gold}; refusing')
            self._send_chat_line(sock, session, 'Server', 'Not enough gold.', no_enc)
            return
        gold -= cost
        session['gold'] = gold
        self._inv_add(session, item, count)
        log.info(f"[BUY] {info.get('name')} item={item} x{count} cost={cost} -> gold={gold}")
        self._send_encrypted(sock, session, 0x18,
                             self._build_opcode_18(gold, winnie, item, count), use_by_array=no_enc)

    def _handle_sell_item(self, sock, session, payload, no_enc):
        """0x0C SELL (client->server) → respond 0x19 (item removed + wallet).

        Payload: u16 item, u8 count, u16 undef (5 B). PySlayer parse_0C shows
        count is a U8 here (unlike buy).
        """
        if len(payload) < 3:
            log.warning(f'[SELL] short payload {len(payload)}B')
            return
        item = struct.unpack_from('<H', payload, 0)[0]
        count = payload[2] or 1   # u8 (not u16)
        if not self._inv_has(session, item, count):
            log.info(f'[SELL] item={item} x{count} not owned; refusing')
            return
        info = _gamedef_item(item)
        self._inv_remove(session, item, count)
        gold, _ = self._wallet(session)
        refund = int((info.get('Sell') if info else 0) or 0) * count
        gold += refund
        session['gold'] = gold
        log.info(f'[SELL] item={item} x{count} refund={refund} -> gold={gold}')
        self._send_encrypted(sock, session, 0x19,
                             self._build_opcode_19(gold, item, count), use_by_array=no_enc)

    # ---- outbound shop/economy builders ----

    def _build_opcode_18(self, gold, winnie, item, count):
        """0x18 GetItem / BuyResult body (16 B). Handler 0x0046DE3E reads:
        GetU64 gold, GetU32 winnie, GetU16 item, GetU16 count."""
        return (struct.pack('<Q', gold & 0xFFFFFFFFFFFFFFFF)
                + struct.pack('<I', winnie & 0xFFFFFFFF)
                + struct.pack('<H', item & 0xFFFF)
                + struct.pack('<H', count & 0xFFFF))

    def _build_opcode_19(self, gold, item, count):
        """0x19 SellResult body (15 B with empty list). Handler 0x0046DBEE reads:
        GetU64 gold, GetU16 item, GetU16 count, GetU8 loopN, loopN*GetU16,
        GetU16 trailer. We emit loopN=0 (so no loop entries) + a 0 trailer."""
        return (struct.pack('<Q', gold & 0xFFFFFFFFFFFFFFFF)
                + struct.pack('<H', item & 0xFFFF)
                + struct.pack('<H', count & 0xFFFF)
                + struct.pack('<B', 0)            # loopN = 0 (no extra u16 list)
                + struct.pack('<H', 0))           # trailing u16

    def _build_opcode_80(self, value, flag=1):
        """0x80 CashShopBalance / CurrencyUpdate body (5 B). Handler 0x0044E2FE:
        GetU8 flag (MUST be 1, else the client shows an error message box),
        then GetU32K value. The 'K' GET variant is a cipher-layer detail; on
        the wire it is a plain 4-byte LE u32 (PySlayer sends p32u)."""
        return struct.pack('<B', flag & 0xFF) + struct.pack('<I', value & 0xFFFFFFFF)

    def _send_buy_result(self, sock, session, item, count, no_enc=False):
        gold, winnie = self._wallet(session)
        body = self._build_opcode_18(gold, winnie, item, count)
        self._send_encrypted(sock, session, 0x18, body, use_by_array=no_enc)

    def _send_sell_result(self, sock, session, item, count, no_enc=False):
        gold, _ = self._wallet(session)
        body = self._build_opcode_19(gold, item, count)
        self._send_encrypted(sock, session, 0x19, body, use_by_array=no_enc)

    def _send_cash_balance(self, sock, session, value=None, no_enc=False):
        """Push a 0x80 currency/cash-shop balance to the client. Call after
        enter-world (or whenever the cash balance changes) to populate the
        cash-shop UI. value defaults to the session's winnie balance."""
        if value is None:
            value = self._wallet(session)[1]
        body = self._build_opcode_80(value, flag=1)
        log.info(f'[CASH] balance update value={value}')
        self._send_encrypted(sock, session, 0x80, body, use_by_array=no_enc)

    # ---- session inventory (point 4: persist drops/buys/uses for a session) ----
    # Stored on the session as {item_idx:int -> count:int}. A separate, simple
    # backend so the same model serves USE / EQUIP / SHOP / DROP-PICKUP.
    def _inventory(self, session):
        return session.setdefault('inventory', {})

    def _inv_add(self, session, item, count=1):
        inv = self._inventory(session)
        inv[item] = inv.get(item, 0) + count
        return inv[item]

    def _inv_remove(self, session, item, count=1):
        """Decrement; remove the key at <=0. Returns the remaining count (>=0)."""
        inv = self._inventory(session)
        have = inv.get(item, 0)
        left = have - count
        if left > 0:
            inv[item] = left
        else:
            inv.pop(item, None)
            left = 0
        return left

    def _inv_has(self, session, item, count=1):
        return self._inventory(session).get(item, 0) >= count

    def _handle_use_item(self, sock, session, payload, no_enc):
        """0x15 UseItemOrSkill (client->server) → apply a consumable's HP/MP.

        Wire format (opcode byte already stripped by parse_packet):
            u16 item_idx          # PySlayer parse_15: up16u(payload[1:3])

        We look the idx up in the item DB (server/data/items.json, same data as
        PySlayer gamedef.sqlite3 `items`). Columns used: `type` (0=consumable,
        1=equip, 2=etc), `hp` (HP restored), `mp` (MP restored). Only type==0
        is consumed here; equip/etc are ignored (equip arrives via 0x0F).

        HP/MP are tracked as ABSOLUTE current values on the session and clamped
        to the per-char max (the value the client was told at enter-world). The
        outbound 0x28 (setHP) / 0x44 (setMP) carry the new ABSOLUTE value as a
        single u16 — verified vs EN receive handlers and PySlayer opcode_28/44
        (which send self.hp / self.mp, i.e. the post-heal absolute, not a delta).
        """
        if len(payload) < 2:
            log.warning(f'[USE] short payload {len(payload)}B')
            return
        item = struct.unpack_from('<H', payload, 0)[0]

        info = item_table.get(item)
        if info is None:
            log.info(f'[USE] item={item} unknown in item DB - ignoring')
            return

        itype = info.get('type', -1)
        if itype != 0:
            # Not a consumable; nothing to apply (equip = 0x0F, etc = 2).
            log.info(f'[USE] item={item} type={itype} not consumable - ignoring')
            return

        # Optional inventory gate: only consume if we know the player owns it.
        # (Inventory may be empty early on; don't hard-block so test heals work.)
        inv = self._inventory(session)
        if item in inv and inv[item] <= 0:
            log.info(f'[USE] item={item} none left in inventory - ignoring')
            return

        hp_restore = int(info.get('hp', 0) or 0)
        mp_restore = int(info.get('mp', 0) or 0)
        if hp_restore <= 0 and mp_restore <= 0:
            log.info(f'[USE] item={item} ("{info.get("title","?")}") has no hp/mp effect')

        char = self._session_char(session) or {}
        # max = what the client believes (sent at enter-world). Default to the
        # spawn values so we never clamp above what the UI bar can show.
        max_hp = int(session.get('max_hp', char.get('hp', 100)))
        max_mp = int(session.get('max_mp', char.get('mp', 50)))

        cur_hp = int(session.get('hp', char.get('hp', max_hp)))
        cur_mp = int(session.get('mp', char.get('mp', max_mp)))

        new_hp = min(cur_hp + hp_restore, max_hp)
        new_mp = min(cur_mp + mp_restore, max_mp)
        session['hp'] = new_hp
        session['mp'] = new_mp

        # Consume one from the session inventory (no-op if untracked).
        if item in inv:
            self._inv_remove(session, item, 1)

        log.info(f'[USE] item={item} ("{info.get("title","?")}") '
                 f'hp {cur_hp}->{new_hp}/{max_hp} mp {cur_mp}->{new_mp}/{max_mp}')

        # Push absolute HP/MP only when they actually changed (avoid redundant
        # UI churn). 0x28 setHP / 0x44 setMP, single u16 each.
        if new_hp != cur_hp:
            self._send_hp(sock, session, new_hp, no_enc=no_enc)
        if new_mp != cur_mp:
            self._send_mp(sock, session, new_mp, no_enc=no_enc)

    def _session_char(self, session):
        """Resolve the character dict for this session (mirrors enter-world)."""
        account = self.accounts.get(session.get('username'), {})
        chars = account.get('characters', [])
        name = session.get('char_name')
        if name:
            for c in chars:
                if c.get('name') == name:
                    return c
        return chars[0] if chars else None

    def _handle_change_map(self, sock, session, payload, no_enc):
        """0x7E portal → 0x08 (change map) THEN 0x07 (re-spawn).

        0x08 alone (handler 0x450867) only rebuilds the map container and sets
        the transition state; the player entity is created ONLY by the 0x07
        handler (0x44EF72). Without a following 0x07 the new map loads but no
        player exists → infinite loading screen. So we replay the same
        byte-perfect 0x07 spawn the enter-world flow uses, at the portal's
        destination x/y. (0x03 is enter-world only; not resent on map change.)

        EN 0x08 body has NO leading flag byte: [u16 mapcode LE][u32 uid][u32 0]
        (the cmp cl,0x5e/0xa3/0xa4 at 0x450867 compares the OPCODE, not payload).
        """
        if len(payload) < 4:
            return
        portal_code = struct.unpack('<I', payload[0:4])[0]
        cur_map = session.get('current_map', 101)
        portals = self._get_portals()
        key = f'{cur_map}_{portal_code}'
        if key not in portals:
            # Unknown portal: DO NOTHING. The old code re-loaded the SAME map and
            # re-spawned in place (0x08 same-mapcode + 0x07), which re-creates the
            # player entity on an already-loaded scene -> duplicate/orphan entity
            # -> character invisible/stuck + flashing HP (observed). Far safer to
            # ignore the portal (player stays put) until it's mapped in portals.json.
            log.warning(f'[CHANGE_MAP] unknown portal {portal_code} from map {cur_map} - ignoring (no re-spawn)')
            return
        next_map, xpos, ypos = portals[key]
        if next_map == cur_map:
            # Same-map portal: also skip the re-load+re-spawn (same duplicate risk).
            log.info(f'[CHANGE_MAP] portal {portal_code} targets same map {cur_map} - ignoring')
            return
        session['current_map'] = next_map
        log.info(f'[CHANGE_MAP] map {cur_map} ({map_filename(cur_map)}) portal {portal_code} → '
                 f'map {next_map} ({map_filename(next_map)}) at ({xpos}, {ypos})')

        uid = session.get('account_id', 1) & 0xFFFFFFFF
        # 1) 0x08 change-map (body unchanged) — loads + renders the new map.
        body_08 = struct.pack('<H', next_map) + struct.pack('<I', uid) + struct.pack('<I', 0)
        self._send_encrypted(sock, session, 0x08, body_08, use_by_array=no_enc)

        # 2) Re-establish the FULL in-game state for the new map by mirroring the
        #    PROVEN enter-world sequence (0x03 in-game-state + 0x07 spawn + 0x0A).
        #    0x08 alone loads the map but does NOT re-create/finalize the player:
        #    the body stayed invisible and the client never sent its post-spawn
        #    handshake (0x2F/0x63) — only 0x0D. Replaying 0x03+0x07+0x0A (the same
        #    thing that works on enter-world) re-establishes the local player.
        char = self._session_char(session)
        if char is not None:
            char = dict(char)
            char['x'] = float(xpos)
            char['y'] = float(ypos)
            char['uid'] = uid
            time.sleep(0.05)
            resp_03 = self._build_pyslayer_opcode_03(session, char, current_map=next_map)
            log.info(f'[CHANGE_MAP] Sending 0x03 in-game state (map {next_map}): {len(resp_03)}B')
            self._send_encrypted(sock, session, 0x03, resp_03, use_by_array=no_enc)
            time.sleep(0.05)
            body_07 = self._build_en_opcode_07_packet(session, char)
            log.info(f'[CHANGE_MAP] Sending 0x07 re-spawn: {len(body_07)}B')
            self._send_encrypted(sock, session, 0x07, body_07, use_by_array=no_enc)
            time.sleep(0.05)
            chat, sender = b'Welcome!', b'Server'
            body_0A = (struct.pack('<B', 1) + sender + b'\x00' * (17 - len(sender))
                       + struct.pack('<B', len(chat)) + chat)
            self._send_encrypted(sock, session, 0x0A, body_0A, use_by_array=no_enc)
            # Phase 1 combat: seed the new map's monsters (none in town 101).
            time.sleep(0.05)
            self._spawn_map_monsters(sock, session, next_map, no_enc=no_enc)
        else:
            log.warning('[CHANGE_MAP] no character in session; cannot re-spawn')

    def _get_portals(self):
        """Lazy-load the portal table from JSON."""
        if not hasattr(self, '_portals_cache'):
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'portals.json'), 'r') as f:
                    self._portals_cache = json.load(f)
            except FileNotFoundError:
                self._portals_cache = {}
        return self._portals_cache

    # ========================================================================
    # In-game opcode handlers (generated + verified by the opcode workflow).
    # Inbound handlers consume/ack so the client never hangs waiting on a reply.
    # ========================================================================
    def _handle_accept_quest(self, sock, session, payload, no_enc):
        """0x16 inbound: ACCEPT (at start NPC) OR TURN-IN (at end NPC) — one opcode
        for both; the server decides from current quest state. Live quest-log via
        the 0x26 grant cluster (NOT a 0x03 reset). u16 quest_id (client builder 0x477330)."""
        if len(payload) < 2:
            log.warning(f'[QUEST] short 0x16 payload {len(payload)}B')
            return
        quest_id = struct.unpack_from('<H', payload, 0)[0]
        q = load_quests().get(quest_id)
        if q is None:
            log.warning(f'[QUEST] unknown quest_id={quest_id}')
            self._send_chat_line(sock, session, 'Server', 'Unknown quest.', no_enc)
            return
        active = session.setdefault('quests', [])
        done = session.setdefault('quests_done', [])
        prog = session.setdefault('quest_progress', {})

        if quest_id not in active and quest_id not in done:
            # ----- ACCEPT -----
            if len(active) >= MAX_ACTIVE_QUESTS:
                self._send_chat_line(sock, session, 'Server', 'Quest log full (3).', no_enc)
                return
            self._quest_assign_slot(session, quest_id)
            active.append(quest_id)
            prog.setdefault(quest_id, {})
            self._send_quest_grant(sock, session, quest_id, no_enc)
            for item, count in q['send']:           # items the NPC hands you on accept
                self._inv_add(session, item, count)
                self._send_drop(sock, session, item=item, count=count, no_enc=no_enc)
            if q['demand']:
                self._send_quest_progress(sock, session, quest_id, 0, no_enc)
            self._send_chat_line(sock, session, 'Server', f'Quest {quest_id} accepted.', no_enc)
            return

        if quest_id in active:
            # ----- TURN-IN -----
            if not self._quest_demand_met(session, q):
                have = sum(prog.get(quest_id, {}).get(i, 0) for i, _ in q['demand'])
                need = sum(n for _, n in q['demand'])
                self._send_chat_line(sock, session, 'Server',
                                     f'Quest {quest_id}: not complete ({have}/{need}).', no_enc)
                return
            self._complete_quest(sock, session, q, no_enc)

    # ---- quest helpers (live log via 0x26 grant / 0x59 progress / 0x27 complete) ----
    def _quest_assign_slot(self, session, quest_id):
        slots = session.setdefault('quest_slot', {})
        if quest_id in slots:
            return slots[quest_id]
        used = set(slots.values())
        for s in (1, 2, 3):
            if s not in used:
                slots[quest_id] = s
                return s
        return None

    def _send_quest_grant(self, sock, session, quest_id, no_enc=False):
        """0x26 GRANT — add quest to the client's active log live (no 0x03 reset)."""
        log.info(f'[QUEST] -> 0x26 GRANT quest_id={quest_id}')
        self._send_encrypted(sock, session, 0x26, struct.pack('<H', quest_id & 0xFFFF), use_by_array=no_enc)

    def _send_quest_progress(self, sock, session, quest_id, current, no_enc=False):
        """0x59 PROGRESS — [u8 slot][u8 count] update the slot's on-screen counter."""
        slot = session.get('quest_slot', {}).get(quest_id)
        if slot is None:
            return
        log.info(f'[QUEST] -> 0x59 PROGRESS quest_id={quest_id} slot={slot} count={current}')
        self._send_encrypted(sock, session, 0x59, struct.pack('<BB', slot & 0xFF, current & 0xFF), use_by_array=no_enc)

    def _send_quest_complete(self, sock, session, quest_id, no_enc=False):
        """0x27 COMPLETE — remove from active log + credit quest Money client-side."""
        log.info(f'[QUEST] -> 0x27 COMPLETE quest_id={quest_id}')
        self._send_encrypted(sock, session, 0x27, struct.pack('<H', quest_id & 0xFFFF), use_by_array=no_enc)

    def _quest_demand_met(self, session, q):
        prog = session.get('quest_progress', {}).get(q['idx'], {})
        return all(prog.get(item, 0) >= need for item, need in q['demand'])

    def _complete_quest(self, sock, session, q, no_enc):
        qid = q['idx']
        for item, need in q['demand']:
            self._inv_remove(session, item, need)        # consume collected items
        self._send_quest_complete(sock, session, qid, no_enc)
        for item, count in q['reward']:                  # reward items via 0x18
            self._inv_add(session, item, count)
            self._send_drop(sock, session, item=item, count=count,
                            gold_gain=(q['money'] if (item, count) == q['reward'][0] else 0), no_enc=no_enc)
        if not q['reward'] and q['money']:
            self._send_drop(sock, session, gold_gain=q['money'], no_enc=no_enc)
        if q['exp']:
            self._send_exp(sock, session, q['exp'], no_enc=no_enc)
        session['quests'].remove(qid)
        session.get('quest_slot', {}).pop(qid, None)
        session.setdefault('quests_done', []).append(qid)
        self._send_chat_line(sock, session, 'Server',
                             f'Quest {qid} complete! +{q["exp"]} exp, +{q["money"]} gold.', no_enc)

    def _quest_credit_item(self, sock, session, item_id, qty, no_enc):
        """Credit a collected/dropped item toward any active quest's demand."""
        if qty <= 0:
            return
        prog = session.setdefault('quest_progress', {})
        for qid in list(session.get('quests', [])):
            q = load_quests().get(qid)
            if not q:
                continue
            for d_item, need in q['demand']:
                if d_item == item_id:
                    cur = prog.setdefault(qid, {})
                    cur[item_id] = min(need, cur.get(item_id, 0) + qty)
                    self._send_quest_progress(sock, session, qid, cur[item_id], no_enc)

    def _handle_use_skill(self, sock, session, payload, no_enc):
        """0x25 inbound = UseSkill (u16 skill). Echo the cast so the animation
        plays (EN handler 0x452D69 first read is an unconditional u16)."""
        if len(payload) < 2:
            return
        skill = struct.unpack_from('<H', payload, 0)[0]
        log.info(f'[SKILL] cast skill={skill}')
        self._send_encrypted(sock, session, 0x25, struct.pack('<H', skill & 0xFFFF), use_by_array=no_enc)

    def _handle_equip_item(self, sock, session, payload, no_enc):
        """0x0F EQUIP. Update the char's appearance state from items.Spr_Num
        (BODY sprite, not the icon) so the gear shows on the next spawn/map-change.
        NOTE: instant appearance refresh (re-sending 0x07) is DEFERRED — re-sending
        0x07 without a preceding map-clear risks a duplicate player entity; needs
        a safe appearance-update opcode (future RE)."""
        if len(payload) < 2:
            return
        item = struct.unpack_from('<H', payload, 0)[0]
        info = _gamedef_item(item)
        if info is None or info.get('Type') != 1:
            log.info(f'[EQUIP] item={item} not equippable (Type!=1) - ignoring')
            return
        char_key = _KIND_TO_APPARENCE.get(info.get('Kind'))
        equipped = session.setdefault('equipped', {})
        if char_key is None:
            equipped[f"kind{info.get('Kind')}"] = item
            log.info(f"[EQUIP] {info.get('name')} Kind={info.get('Kind')} - no visible slot (state only)")
            return
        char = self._session_char(session)
        if char is None:
            return
        char[char_key] = int(info.get('Spr_Num') or 0) & 0xFFFF
        equipped[char_key] = item
        # Instant in-place appearance refresh via 0x1D (finds entity by uid,
        # re-composes the skin; never allocates -> no duplicate player).
        self._send_encrypted(sock, session, 0x1D,
                             self._build_en_opcode_1D(char), use_by_array=no_enc)
        log.info(f"[EQUIP] {info.get('name')} item={item} -> {char_key}=Spr_Num {char[char_key]} "
                 f"(instant 0x1D refresh, uid={char.get('uid', 1)})")

    def _handle_room_query(self, sock, session, payload, no_enc):
        """0x2C inbound = room/arena list query (u8 code), sent once at spawn.
        Consume; answering with 0x33 unprompted opens the arena UI."""
        code = payload[0] if payload else 0
        log.info(f'[0x2C] room query code={code} - consumed')

    def _send_chat_line(self, sock, session, sender, text, no_enc=False):
        """Outbound chat broadcast (opcode 0x16): u8 count=1, CHAR[17] sender,
        u8 len, text. (EN receive-handler 0x451AF0 reads a leading entry count.)"""
        name_bytes = sender.encode('ascii', 'replace')[:16].ljust(17, b'\x00')
        msg_bytes = text.encode('ascii', 'replace')[:255]
        body = struct.pack('<B', 1) + name_bytes + struct.pack('<B', len(msg_bytes)) + msg_bytes
        self._send_encrypted(sock, session, 0x16, body, use_by_array=no_enc)

    def _build_opcode_1A(self, npccode, mob_uid, x, y, anim_idx=None):
        """Outbound monster spawn (0x1A) — byte-exact per EN handler 0x451D8D.

        Re-derived field-by-field from the handler's Get* read order (workflow
        ws-bug-triage). The PREVIOUS body diverged after field 4 (wrong order +
        treated GetU32K as variable) which skewed the per-field cipher stream,
        landing the monster off-world (garbage pos) so it was never finalized.

        +0xe60 is the 1-based ANIM-CONTAINER index (1..count, ~180), NOT the
        npccode — the handler at 0x451E47 does a container lookup (0x409770)
        and if it fails the render record is never bound (entity stays invisible
        even with a correct body). GetU32K is 4 bytes on the wire (same as the
        working 0x07 builder)."""
        if anim_idx is None:
            anim_idx = npccode            # caller SHOULD pass a real anim index
        b = bytearray()
        def u8(v):  b.append(v & 0xFF)
        def u16(v): b.extend(struct.pack('<H', v & 0xFFFF))
        def u32(v): b.extend(struct.pack('<I', v & 0xFFFFFFFF))
        def i32(v): b.extend(struct.pack('<i', int(v)))
        def f64(v): b.extend(struct.pack('<d', float(v)))
        ix, iy = int(x), int(y)
        u8(1)                 # 451D9F count >=1
        u32(anim_idx)         # 451E00 +0xe60 anim-container index (must resolve!)
        u32(mob_uid)          # 451E1A +0x84  uid
        u8(0)                 # 451F9D inner-loop count = 0 (no sub-entries)
        u8(0)                 # 45204C +0x8bb
        u8(0)                 # 452066 +0x8bc
        u32(0)                # 452080 +0xdbc (K)
        u32(0)                # 45209A +0xdb8 (K)
        u32(ix)               # 4520B5 +0x1318 x (K, -> float)
        u32(iy)               # 4520EE +0x1320 y (K, -> float)
        u32(0)                # 452126 +0xe5c (K)
        u32(32)               # 452140 +0x954 (K)  [match 0x07]
        u8(0)                 # 45215A +0x8d9 bool
        u8(1)                 # 452174 +0x8cf      [match 0x07]
        i32(0)                # 45218E +0x904
        u32(501)              # 4521A8 +0xe00 (K)  [match 0x07]
        u8(0)                 # 4521C2 +0x8bd
        f64(x)                # 4521DC +0x11f8 posX
        f64(y)                # 4521F6 +0x1288 posY
        i32(0)                # 452210 +0xe50
        u8(127); u8(0); u8(0); u8(1)        # 45222A.. +0x8b3..8b6 ip
        u8(0); u8(0); u8(0); u8(0); u8(0)   # 452292.. +0x8da/8df/8dc/8dd/8de bools
        u16(0)                # 452315 +0x9c
        u32(0)                # 45233D +0xd94 (K)
        u8(0)                 # 452357 +0x8e4 bool = 0 -> SKIP +0xe48, block ends
        return bytes(b)

    def _spawn_test_monster(self, sock, session, npccode=1001, x=None, y=None, no_enc=False):
        """Debug: spawn one monster near the player (combat bring-up). NOT called
        automatically (avoids duplicate-uid mobs on re-entry)."""
        char = self._session_char(session) or {}
        if x is None: x = char.get('x', 1411.0)
        if y is None: y = char.get('y', 714.0)
        mob_uid = session.get('next_mob_uid', 0x1000)
        session['next_mob_uid'] = mob_uid + 1
        body = self._build_opcode_1A(npccode, mob_uid, x, y)
        log.info(f'[MOB] spawn npc={npccode} uid={mob_uid} at ({x},{y}) {len(body)}B')
        self._send_encrypted(sock, session, 0x1A, body, use_by_array=no_enc)
        return mob_uid

    def _send_hp(self, sock, session, hp, no_enc=False):
        """Outbound 0x28 setHP (single u16)."""
        self._send_encrypted(sock, session, 0x28, struct.pack('<H', hp & 0xFFFF), use_by_array=no_enc)

    def _send_mp(self, sock, session, mp, no_enc=False):
        """Outbound 0x44 setMP (single u16)."""
        self._send_encrypted(sock, session, 0x44, struct.pack('<H', mp & 0xFFFF), use_by_array=no_enc)

    # ========================================================================
    # PHASE 1 COMBAT — spawn monsters per map; resolve attack -> hp -> death ->
    # exp/drop. Monster HP uses 0x14 (per-uid); 0x28/0x44 are LOCAL-player only.
    # ========================================================================
    def _spawn_map_monsters(self, sock, session, map_id, no_enc=True):
        """Spawn all monsters for map_id (0x1A each) and track them in
        session['monsters']={uid:Monster}. Rebuilt fresh on every map enter."""
        spawns = MAP_SPAWNS.get(map_id, [])
        session['monsters'] = {}
        if not spawns:
            log.info(f'[MOB] map {map_id} has no monster spawns (town/none)')
            return
        for i, (npccode, x, y) in enumerate(spawns):
            stat = MONSTER_DB.get(npccode)
            if stat is None:
                log.warning(f'[MOB] no stats for npccode {npccode}, skipping')
                continue
            uid = MOB_UID_BASE + i
            mob = Monster(uid=uid, npccode=npccode, name=stat['name'], level=stat['level'],
                          hp=stat['hp'], max_hp=stat['hp'], body_atk=stat['atk'],
                          defense=stat['defense'], exp=stat['exp'], x=float(x), y=float(y),
                          spawn_x=float(x), spawn_y=float(y),
                          drop_items=stat.get('drops', []), gold=stat.get('gold', 0))
            session['monsters'][uid] = mob
            body = self._build_opcode_1A(npccode, uid, x, y, anim_idx=stat.get('anim', npccode))
            log.info(f"[MOB] spawn {mob.name} npc={npccode} uid={uid:#x} anim={stat.get('anim', npccode)} at ({x},{y}) hp={mob.hp}")
            self._send_encrypted(sock, session, 0x1A, body, use_by_array=no_enc)
        log.info(f"[MOB] spawned {len(session['monsters'])} monsters on map {map_id}")

    def _handle_attack(self, sock, session, payload, no_enc=True):
        """Inbound 0x25. Parse the client's hit-target uid list and resolve each.
        Format (lenient): u64 skillId | u8 count | count*{u16 uid,u16,u8 sub,sub*u16,u16}"""
        self._tick_respawns(sock, session, no_enc)
        p = payload
        if len(p) < 9:
            log.info(f'[ATK] short 0x25 ({len(p)}B) - echo only')
            return
        off = 0
        skill_id = struct.unpack_from('<Q', p, off)[0]; off += 8
        targets = []

        def parse_group(off):
            if off >= len(p):
                return off
            count = p[off]; off += 1
            for _ in range(count):
                if off + 5 > len(p):
                    break
                uid = struct.unpack_from('<H', p, off)[0]; off += 2
                off += 2
                sub = p[off]; off += 1
                for _s in range(sub):
                    if off + 2 > len(p):
                        break
                    off += 2
                if off + 2 <= len(p):
                    off += 2
                targets.append(uid)
            return off

        off = parse_group(off)
        if off + 8 <= len(p):
            off += 8
            off = parse_group(off)
        log.info(f'[ATK] skill={skill_id} targets={[hex(t) for t in targets]}')
        for uid in targets:
            self._resolve_hit(sock, session, skill_id, uid, no_enc=no_enc)

    def _compute_damage(self, session, skill_id, mob):
        char = self._session_char(session) or {}
        base = int(char.get('attack', char.get('str', 10)) or 10)
        raw = int(base * (1.5 if skill_id else 1.0))
        return max(1, raw - mob.defense)

    def _resolve_hit(self, sock, session, skill_id, mob_uid, no_enc=True):
        mob = session.get('monsters', {}).get(mob_uid)
        # client sends the FULL uid (MOB_UID_BASE+i); it may arrive truncated to
        # the low 16 bits, so match on either form.
        if mob is None:
            for m in session.get('monsters', {}).values():
                if (m.uid & 0xFFFF) == (mob_uid & 0xFFFF):
                    mob = m; break
        if mob is None or not mob.alive:
            return
        dmg = self._compute_damage(session, skill_id, mob)
        mob.hp = max(0, mob.hp - dmg)
        log.info(f'[ATK] hit {mob.name} uid={mob.uid:#x} dmg={dmg} -> hp={mob.hp}/{mob.max_hp}')
        self._send_entity_hp(sock, session, mob.uid, mob.hp, no_enc=no_enc)
        if mob.hp <= 0:
            self._kill_monster(sock, session, mob, no_enc=no_enc)

    def _kill_monster(self, sock, session, mob, no_enc=True):
        mob.alive = False
        log.info(f'[KILL] {mob.name} uid={mob.uid:#x} dead; exp={mob.exp} gold={mob.gold}')
        self._send_death(sock, session, mob.uid, no_enc=no_enc)
        if mob.exp:
            self._send_exp(sock, session, mob.exp, no_enc=no_enc)
        item, count = 0, 0
        if mob.drop_items and random.random() < 0.6:
            item, count = random.choice(mob.drop_items), 1
        if item:
            self._inv_add(session, item, count)   # grant the drop into the inventory
            self._quest_credit_item(sock, session, item, count, no_enc)  # quest collect/kill progress
        self._send_drop(sock, session, item=item, count=count, gold_gain=mob.gold, no_enc=no_enc)
        mob.respawn_at = time.monotonic() + MOB_RESPAWN_SECS

    def _build_opcode_14(self, uid, stat_type, value):
        return struct.pack('<I', uid & 0xFFFFFFFF) + struct.pack('<B', stat_type & 0xFF) + struct.pack('<H', value & 0xFFFF)

    def _send_entity_hp(self, sock, session, uid, hp, no_enc=False):
        # 0x14 UpdateStats type=2 -> per-uid overhead HP (NOT 0x28/0x44 which are local-only)
        self._send_encrypted(sock, session, 0x14, self._build_opcode_14(uid, 2, hp), use_by_array=no_enc)

    def _build_opcode_29(self, uid, anim=0, dx=0, dz=0):
        return (struct.pack('<I', uid & 0xFFFFFFFF) + struct.pack('<I', anim & 0xFFFFFFFF)
                + struct.pack('<I', dx & 0xFFFFFFFF) + struct.pack('<I', dz & 0xFFFFFFFF))

    def _send_death(self, sock, session, uid, anim=0, dx=0, dz=0, no_enc=False):
        log.info(f'[COMBAT] death uid={uid:#x}')
        self._send_encrypted(sock, session, 0x29, self._build_opcode_29(uid, anim, dx, dz), use_by_array=no_enc)

    def _send_exp(self, sock, session, exp_delta, no_enc=False):
        session['exp'] = session.get('exp', 0) + exp_delta
        log.info(f"[COMBAT] +{exp_delta} exp (session total={session['exp']})")
        self._send_encrypted(sock, session, 0x21, struct.pack('<i', exp_delta), use_by_array=no_enc)

    def _send_drop(self, sock, session, item=0, count=0, gold_gain=0, winnie_gain=0, no_enc=False):
        gold, winnie = self._wallet(session)
        gold = (gold + gold_gain) & 0xFFFFFFFFFFFFFFFF
        winnie = (winnie + winnie_gain) & 0xFFFFFFFF
        session['gold'], session['winnie'] = gold, winnie
        log.info(f'[DROP] item={item}x{count} gold+={gold_gain} -> gold={gold}')
        self._send_encrypted(sock, session, 0x18, self._build_opcode_18(gold, winnie, item, count), use_by_array=no_enc)

    def _tick_respawns(self, sock, session, no_enc=True):
        now = time.monotonic()
        for mob in list(session.get('monsters', {}).values()):
            if not mob.alive and mob.respawn_at and now >= mob.respawn_at:
                mob.hp, mob.alive, mob.respawn_at = mob.max_hp, True, 0.0
                mob.x, mob.y = mob.spawn_x, mob.spawn_y
                log.info(f'[MOB] respawn {mob.name} uid={mob.uid:#x}')
                self._send_encrypted(sock, session, 0x1A,
                                     self._build_opcode_1A(mob.npccode, mob.uid, mob.x, mob.y),
                                     use_by_array=no_enc)

    def _build_pyslayer_opcode_03(self, session, char, current_map=101):
        """
        opcode 0x03 — in-game state. Ported from PySlayer.
        Sent in response to client's 0x2B enter-game request.
        """
        body = bytearray()
        body.append(1)                                       # must be >= 1
        body.extend(struct.pack('<H', current_map))          # map xml_mapcode
        body.extend(struct.pack('<I', 1000))                 # x ?
        body.extend(struct.pack('<Q', 100000))               # gold
        body.extend(struct.pack('<I', 30000))                # fame related
        body.extend(struct.pack('<I', 0))                    # 명성
        body.extend(struct.pack('<I', 0))                    # winnie
        body.extend(struct.pack('<I', 0))                    # battle wins
        body.extend(struct.pack('<I', 0))                    # battle losses
        body.extend(struct.pack('<I', 0))                    # battle KO
        body.extend(struct.pack('<I', 0))                    # battle Down
        # --- mentor/spouse gate (U32) — handler 0x44E54C: if NONZERO it then
        # reads GetStr(17)+GetU8 (spouse/mentor name sub-block); 0 SKIPS it.
        # The OLD code emitted bool(0)+U32(1000)+'Mentor'[17]+byte, which the
        # client read as a NONZERO gate (0x0003E800) → over-read name+byte →
        # desynced EVERY field after it (incl. the quest log) by ~18 bytes,
        # making the 3-slot active-quest log read FULL → "only 3 at a time".
        # Send exactly ONE zero U32. (Verified: handler 0x44E38F.)
        body.extend(struct.pack('<I', 0))                    # mentor gate = 0
        body.append(0)                                        # +0xe78 flag
        # --- ACTIVE quest log: EXACTLY 3 slots (handler 0x44E650 reads 3 u16
        # ids then 0x44E690 reads 3 u8 counts). Empty so the client lets you
        # accept new quests. ---
        for _ in range(3):
            body.extend(struct.pack('<H', 0))                # active quest id (empty)
        for _ in range(3):
            body.append(0)                                    # active quest count (empty)
        # --- 3 INVENTORY SLOT CAPACITIES (+0x3a9/+0x3aa/+0x3ab): equip /
        # consume / other tab sizes. NOT value-irrelevant — zeroing these gave
        # "There isn't empty space in the inventory" on quest-accept. 35 each. ---
        body.append(35)                                      # equip slots
        body.append(35)                                      # consume slots
        body.append(35)                                      # other slots
        # --- 4 count-prefixed lists (completed quests etc.); count=0 each
        # (handler skips each list when count==0). ---
        body.append(0)                                        # list A (completed quests) count
        body.append(0)                                        # list B count
        body.append(0)                                        # list C count
        body.append(0)                                        # list D count
        # NOTE: no trailing event-time U32 — handler reads nothing more here.
        return bytes(body)

    def _build_pyslayer_opcode_07(self, session, char, pad=True):
        """
        opcode 0x07 — spawn packet. Ported from PySlayer.
        Player list visible in the current map (just self in single-player).

        pad=True (legacy): zero-pad to 2030 bytes — what we did before; may
        cause the EN client's read-loop to overrun into bogus fields.
        pad=False: end the packet at the natural PySlayer length (~400 bytes
        for solo). PySlayer does not pad.
        """
        body = bytearray()
        body.append(1)                                       # 1 player visible
        # ---- self ----
        name = char.get('name', 'Hero').encode('ascii', errors='replace')[:16]
        body.extend(name + b'\x00' * (17 - len(name)))       # name CHAR[17]
        body.extend(struct.pack('<I', session.get('account_id', 1) & 0xFFFFFFFF))  # uid
        # 2026-04-25: PySlayer KR sends p32u(1) here. EN client reads the FIRST
        # byte of this as buf[304] and if non-zero triggers extra reads
        # (uint16 + uint16 + string). Send 0 to skip that branch.
        body.extend(struct.pack('<I', 0))                    # was 1; now 0 to skip EN conditional
        body.extend(struct.pack('<H', 0))                    # chat target flag
        body.extend(struct.pack('<H', 0))                    # guild flag (0 = none)
        body.append(0)                                       # marriage flag
        body.append(char.get('class', 0) & 0xFF)             # job1
        body.append(0)                                       # job2
        body.append(char.get('level', 1) & 0xFF)             # level
        body.append(20)                                      # rank
        body.append(0)                                       # ?
        # Apparences: 17 USHORTs. Values come from PySlayer's known-good dummy
        # so the character actually renders. [head, hair, face, ?, top, bottom,
        # shoes, ?, gloves, helm, ?, weapon, ?, eff, eff, eff, eff]
        apparences = [
            0,                                # head
            123,                              # hair
            char.get('face', 1) & 0xFFFF,     # face
            0,                                # ?
            char.get('top', 100) & 0xFFFF,    # top (clothes)
            char.get('bottom', 200) & 0xFFFF, # bottom (pants)
            char.get('shoes', 300) & 0xFFFF,  # shoes
            0,                                # ?
            116,                              # gloves
            131,                              # helm
            0,                                # ?
            10,                               # weapon
            0,                                # ?
            701, 701, 701, 701,               # effects
        ]
        for v in apparences:
            body.extend(struct.pack('<H', v))
        body.extend(struct.pack('<H', char.get('str', 3)))
        body.extend(struct.pack('<H', char.get('dex', 3)))
        body.extend(struct.pack('<H', char.get('int', 1)))
        body.extend(struct.pack('<H', char.get('spr', 2)))   # tol
        for _ in range(15):                                  # equips
            body.extend(struct.pack('<H', 0))
            for _ in range(6):
                body.extend(struct.pack('<H', 0))
        for _ in range(10):                                  # cash equips
            body.extend(struct.pack('<h', 0))
            body.extend(struct.pack('<h', 0))
            body.extend(struct.pack('<h', 0))
        body.append(0)                                       # buffs flag
        body.append(0)                                       # bool
        # Spawn position from PySlayer's gamedef.sqlite3: portal_code 26 from
        # stage01_02 → stage01_01 lands at (1411, 714) — verified valid on
        # this map's playable area. Earlier (500, 500) put us off-map.
        body.extend(struct.pack('<d', 1411.0))               # x position
        body.extend(struct.pack('<d', 714.0))                # y position
        body.extend(struct.pack('<I', 32))                   # ?
        body.append(0)                                       # bool
        body.append(1)                                       # ?
        body.extend(struct.pack('<I', 501))
        body.extend(struct.pack('<I', 502))
        body.append(0)
        body.extend(struct.pack('<I', 503))
        # IP bytes in reverse order (PySlayer convention). For 127.0.0.1 we send
        # [ip[3], ip[2], ip[1], ip[0]] = [1, 0, 0, 127]. The last octet was 127
        # before — should have been 1.
        body.append(1)                                       # ip[3] = last octet
        body.append(0)                                       # ip[2]
        body.append(0)                                       # ip[1]
        body.append(127)                                     # ip[0] = first octet
        body.append(0)                                       # action flag
        body.append(0)
        body.append(0)
        body.append(0)
        body.append(0)
        body.append(0)
        body.append(1)
        body.extend(struct.pack('<H', char.get('hp', 100)))
        body.extend(struct.pack('<H', char.get('mp', 50)))
        body.extend(struct.pack('<I', 1))
        body.append(1)
        # is_my_connection==True branch in PySlayer doesn't add the trailing
        # bool/string fields (those are for OTHER players)

        # 2026-04-26 Phase 4 step 5: padding is now optional. The padding to
        # 2030 bytes was added when we believed the EN client read past the
        # PySlayer block (and zero-pad would be safer than random heap bytes).
        # Deep RE of handler 0x44EF72 shows ~140-400 bytes are read on the
        # main path with conditional reads — well within PySlayer's natural
        # block size. Padding may cause overflow reads of zeros into HP/MP
        # slots. Default to no padding now.
        if pad:
            target_size = 2030  # leaves room for opcode + header
            if len(body) < target_size:
                body.extend(b'\x00' * (target_size - len(body)))
        return bytes(body)

    def _build_en_opcode_1D(self, char):
        """opcode 0x1D — in-place appearance/equip refresh for an EXISTING entity
        (handler 0x0045298C). Finds the entity by uid (call 0x004189F0) and
        re-composes its sprite skin; it NEVER allocates, so it cannot duplicate
        the player (unlike 0x07/0x04 which always allocate via 0x444390).
        Wire: u32 uid | u16 skinSelector | u8 N | N*u16 apparence | u16 weapon.
        The 14-slot apparence layout mirrors _build_en_opcode_07 (idx11=weapon)."""
        apparences = [
            0,                                  # 0  head
            char.get('hair', 1) & 0xFFFF,       # 1  hair
            char.get('face', 1) & 0xFFFF,       # 2  face
            0,                                  # 3
            char.get('top', 0) & 0xFFFF,        # 4  top
            char.get('bottom', 0) & 0xFFFF,     # 5  bottom
            char.get('shoes', 0) & 0xFFFF,      # 6  shoes
            0, 0, 0, 0,                         # 7..10
            char.get('weapon', 0) & 0xFFFF,     # 11 weapon
            0, 0,                              # 12,13
        ]
        body = bytearray()
        body += struct.pack('<I', int(char.get('uid', 1)) & 0xFFFFFFFF)   # uid
        body += struct.pack('<H', (char.get('class', 1) or 1) & 0xFFFF)   # skinSelector (1-based class)
        body.append(len(apparences) & 0xFF)                              # N = 14
        for v in apparences:
            body += struct.pack('<H', v & 0xFFFF)
        body += struct.pack('<H', char.get('weapon', 0) & 0xFFFF)        # trailing weapon
        return bytes(body)

    def _build_en_opcode_07(self, session, char):
        """
        opcode 0x07 — EN player-spawn block, byte-exact per the handler at
        VA 0x44EF72. Returns ONE per-player block (no leading count); the
        caller prepends the u8 player-count byte.

        This layout was re-derived field-by-field from a full trace of the
        handler's Get* sequence (see trace_handler.py output). The PREVIOUS
        version was misaligned: it omitted the +0xE1 byte, the 17-byte string
        at +0xD0, and the +0x12/+0x14 fields, and treated POSITION as i32 at
        +0x904/+0xE50. The real position is two f64 at +0x11F8/+0x1288. That
        skew left the entity with garbage position (rendered off-world) and
        scrambled appearance — i.e. the invisible character.

        Field order / wire types (entity offsets in comments):
          name CHAR[17] @+0x00 | uid u32 @+0x84 | i32 @+0x15D8 |
          u16 @+0xE2 | u8 @+0xE1 | string CHAR[17] @+0xD0 | u16 @+0x12 |
          u8 @+0x14 | u8 @+0x110 | u8 @+0x111 | u8 @+0x99 (level) | u8 @+0x9A |
          u8 @+0x113 | apparences u16[14] @+0x120 | u16 @+0xE6/E8/EA/EC |
          equipment 16×(u16 + 6×u16) @+0x13C/+0x16E | extra u16[9] @+0x15C |
          N1 u8=0 | N2 u8=0 | u32 @+0x954 | u8 @+0x8D9 | u8 @+0x8CF |
          i32 @+0x904 | u32 @+0xE00 | u8 @+0x8BD |
          posX f64 @+0x11F8 | posY f64 @+0x1288 | i32 @+0xE50 |
          ip u8×4 @+0x8B3.. | 5×bool @+0x8DA/DF/DC/DD/DE |
          u16 @+0x9C | u16 @+0xA0 | u32 @+0xD94 | u8 @+0x8EC |
          bool @+0x14F4 = 0  (=0 ends the block; nonzero would add u16+CHAR[25])
        """
        def u8(v):  body.append(v & 0xFF)
        def u16(v): body.extend(struct.pack('<H', v & 0xFFFF))
        def u32(v): body.extend(struct.pack('<I', v & 0xFFFFFFFF))
        def i32(v): body.extend(struct.pack('<i', int(v)))
        def f64(v): body.extend(struct.pack('<d', float(v)))
        def cstr(s, n):
            b = s.encode('ascii', errors='replace')[:n - 1]
            body.extend(b + b'\x00' * (n - len(b)))

        body = bytearray()

        cstr(char.get('name', 'Hero'), 17)          # +0x00 name
        u32(char.get('uid', 1))                      # +0x84 uid (network id)
        i32(1)                                       # +0x15D8

        # +0xE2 marriage u16. Handler GATE @0x44F03C: if marriage != 0 it then
        # reads u8 @+0xE1 and CHAR[17] @+0xD0 (spouse name). marriage == 0 SKIPS
        # both. We send 0, so we must NOT emit them.
        marriage = 0
        u16(marriage)
        if marriage != 0:
            u8(0)                                    # +0xE1 (only if married)
            cstr(char.get('spouse', ''), 17)         # +0xD0 spouse name

        # +0x12 u16. Handler GATE @0x44F093: reads bool @+0x14 ONLY if +0x12 == 1.
        field12 = 0
        u16(field12)
        if field12 == 1:
            u8(0)                                    # +0x14 (only if +0x12 == 1)

        u8(char.get('class', 1))                      # +0x110 job1 (1=warrior)
        u8(0)                                         # +0x111 job2
        u8(min(char.get('level', 1), 99))            # +0x99 level
        u8(0)                                         # +0x9A
        u8(0)                                         # +0x113

        # apparences u16[14] @+0x120 — body/cosmetic part ids
        apparences = [
            0,                                  # 0  head
            char.get('hair', 1) & 0xFFFF,       # 1  hair
            char.get('face', 1) & 0xFFFF,       # 2  face
            0,                                  # 3
            char.get('top', 0) & 0xFFFF,        # 4  top
            char.get('bottom', 0) & 0xFFFF,     # 5  bottom
            char.get('shoes', 0) & 0xFFFF,      # 6  shoes
            0, 0, 0, 0,                         # 7..10
            char.get('weapon', 0) & 0xFFFF,     # 11 weapon
            0, 0,                              # 12,13
        ]
        assert len(apparences) == 14
        for v in apparences:
            u16(v)

        # 4× u16 @+0xE6/E8/EA/EC (equip-appearance / dye ids)
        u16(0); u16(0); u16(0); u16(0)

        # equipment: 16 × {u16 item_id, 6×u16 enchant} @+0x13C / +0x16E
        for _ in range(16):
            u16(0)
            for _ in range(6):
                u16(0)

        # extra u16[9] @+0x15C
        for _ in range(9):
            u16(0)

        u8(0)                                         # N1 inventory count = 0
        u8(0)                                         # N2 skill count = 0

        u32(32)                                       # +0x954
        u8(0)                                         # +0x8D9
        u8(1)                                         # +0x8CF
        i32(0)                                        # +0x904
        u32(501)                                      # +0xE00
        u8(0)                                         # +0x8BD

        f64(char.get('x', 2000.0))                    # +0x11F8 posX  (f64!)
        f64(char.get('y', 2000.0))                    # +0x1288 posY  (f64!)
        i32(0)                                         # +0xE50

        # ip bytes @+0x8B3..+0x8B6
        client_ip = session.get('client_ip', '127.0.0.1')
        try:
            octets = [int(x) for x in client_ip.split('.')]
            if len(octets) != 4:
                raise ValueError
        except ValueError:
            octets = [127, 0, 0, 1]
        for o in octets:
            u8(o)

        u8(0); u8(0); u8(0); u8(0); u8(0)            # 5 bools @+0x8DA/DF/DC/DD/DE

        u16(0)                                         # +0x9C
        u16(0)                                         # +0xA0
        u32(0)                                         # +0xD94
        u8(0)                                          # +0x8EC
        u8(0)                                          # +0x14F4 = 0 -> block ends

        return bytes(body)

    def _build_en_opcode_07_packet(self, session, char):
        """
        Top-level wrapper for opcode 0x07: u8 player_count = 1, then ONE
        per-player block from _build_en_opcode_07. Returns the packet body
        (without the leading 0x07 opcode byte — _send_encrypted adds that).
        """
        body = bytearray()
        body.append(1)  # player_count — must be >= 1
        body.extend(self._build_en_opcode_07(session, char))
        return bytes(body)

    def _build_pyslayer_enter_world(self, session, char):
        """
        Format ported from PySlayer's server_packets/opcode_0x2E.py.
        Returns a payload (excluding the leading opcode byte — caller should
        send this with opcode=0x2B).
        """
        body = bytearray()

        # v802 = 1 (character count)
        body.append(1)

        # ---- per-character ----
        name = char.get('name', 'Hero').encode('ascii', errors='replace')[:16]
        body.extend(name + b'\x00' * (17 - len(name)))   # name CHAR[17]
        body.extend(struct.pack('<I', 10))                # uint32 = 10
        body.extend(struct.pack('<I', 30))                # uint32 = 30
        body.extend(struct.pack('<H', 0))                 # uint16 = 0 (chat target?)
        body.append(0)                                     # guild flag = 0 (no guild)

        body.append(char.get('class', 0) & 0xFF)           # job1
        body.append(0)                                     # job2 (no second job)
        body.append(char.get('level', 1) & 0xFF)           # level
        body.append(20)                                    # rank
        body.append(1)                                     # bool

        # apparences: 17 uint16s — appearance/equipment IDs
        for _ in range(17):
            body.extend(struct.pack('<H', 0))

        # stats — str/dex/int/tol + 2 trailers
        body.extend(struct.pack('<H', char.get('str', 3)))
        body.extend(struct.pack('<H', char.get('dex', 3)))
        body.extend(struct.pack('<H', char.get('int', 1)))
        body.extend(struct.pack('<H', char.get('spr', 2)))   # 'tol' in PySlayer
        body.extend(struct.pack('<H', 100))
        body.extend(struct.pack('<H', 101))

        # equips: 15 entries × (1 uint16 + 6 enchant uint16) = 15*14 = 210 bytes
        for _ in range(15):
            body.extend(struct.pack('<H', 0))               # equip id
            for _ in range(6):
                body.extend(struct.pack('<H', 0))           # enchant

        # cash equip: 10 entries × 3 uint16 = 60 bytes
        for _ in range(10):
            body.extend(struct.pack('<h', 100))             # signed in PySlayer
            body.extend(struct.pack('<h', 1))
            body.extend(struct.pack('<h', 1))

        body.append(0)                                      # buffs flag
        body.append(0)                                      # something
        body.append(1)                                      # ?
        body.append(2)                                      # ?
        body.append(3)                                      # ?
        body.extend(struct.pack('<I', 100))                 # uint32

        body.append(0)                                      # else-method bool
        body.extend(struct.pack('<b', 1))                   # signed int8
        body.extend(b'test'.ljust(13, b'\x00'))             # string padded to 13

        return bytes(body)

    def _build_enter_world_response(self, session, char):
        """
        Build the enter-world response body (after opcode 0x2B).

        Wire order (from VA 0x4503E1 handler analysis):
          1. CHAR[17]  name
          2. UINT32    char_id
          3. INT32     money
          4. USHORT    level
          5. UCHAR     class
          6. UCHAR     gender
          7. UCHAR     hair
          8. UCHAR     face
          9. UINT32    flags
          10. USHORT[14] stats
          11. USHORT   hp
          12. USHORT   mp
          13. USHORT   max_hp
          14. USHORT   max_mp
          15-16. (loop 16x): USHORT slot_id, USHORT[6] slot_attrs
          17. USHORT[9] skills
          18. UCHAR    extra_count (N)
          19. USHORT[N] extras
          20. UCHAR    flag_14EB (1 => status=3 alive, 0 => status=1)
          21. UCHAR    last_byte
        """
        resp = bytearray()

        # 0. Sub-opcode byte (REAL handler 0x44F762 reads this FIRST via
        #    GetDataFromPacket(UCHAR&) at VA 0x44F774, then checks
        #    `cmp byte [esp+0x56], 0; jbe 0x44FCB1` — must be > 0).
        resp.append(0x01)

        # 1. name CHAR[17]
        name = char.get('name', 'Hero').encode('ascii', errors='replace')[:16]
        resp.extend(name + b'\x00' * (17 - len(name)))

        # 2. char_id UINT32
        resp.extend(struct.pack('<I', session['account_id']))

        # 3. money INT32
        resp.extend(struct.pack('<i', 1000))

        # 4. level USHORT
        resp.extend(struct.pack('<H', char.get('level', 1)))

        # 5. class UCHAR
        resp.append(char.get('class', 0) & 0xFF)

        # 6. gender UCHAR
        resp.append(0)

        # 7. hair UCHAR, 8. face UCHAR
        resp.append(char.get('top', 0) & 0xFF)
        resp.append(char.get('face', 0) & 0xFF)

        # 9. flags UINT32
        resp.extend(struct.pack('<I', 0))

        # 10. stats USHORT[14] - STR, DEX, INT, SPR + 10 more (bonuses/resistances?)
        stats = [
            char.get('str', 3), char.get('dex', 3),
            char.get('int', 1), char.get('spr', 2),
        ] + [0] * 10
        for s in stats:
            resp.extend(struct.pack('<H', s & 0xFFFF))

        # 11-14. hp, mp, max_hp, max_mp
        resp.extend(struct.pack('<H', char.get('hp', 100)))
        resp.extend(struct.pack('<H', char.get('mp', 50)))
        resp.extend(struct.pack('<H', char.get('hp', 100)))
        resp.extend(struct.pack('<H', char.get('mp', 50)))

        # 15-16. 16 slots: USHORT slot_id, USHORT[6] slot_attrs per slot
        for slot_i in range(16):
            resp.extend(struct.pack('<H', 0))        # slot_id
            for j in range(6):
                resp.extend(struct.pack('<H', 0))    # slot_attr

        # 17. skills USHORT[9]
        for _ in range(9):
            resp.extend(struct.pack('<H', 0))

        # 18. extra_count UCHAR (0 = no extras)
        resp.append(0)
        # 19. no extras, loop doesn't iterate

        # 20. flag UCHAR (1 makes buf[0x98]=3 "alive")
        resp.append(1)

        # 21. last byte
        resp.append(0)

        # Pad to 2000 bytes. This was the working state where HUD loaded and
        # the char rendered. Trimming to 316 caused faster crashes, not slower.
        while len(resp) < 2000:
            resp.append(0)

        return bytes(resp)

    def _handle_create_character(self, sock, session, payload, no_enc=False):
        """
        Create character request (opcode 0x0E).
        Payload (35 bytes):
          WORD class_id
          WORD face, top, bottom, shoes
          CHAR[17] name
          WORD str, dex, int, spr
        """
        if len(payload) < 35:
            log.warning(f'[CREATE] Payload too short: {len(payload)}B')
            return

        cls = struct.unpack_from('<H', payload, 0)[0]
        face = struct.unpack_from('<H', payload, 2)[0]
        top = struct.unpack_from('<H', payload, 4)[0]
        bottom = struct.unpack_from('<H', payload, 6)[0]
        shoes = struct.unpack_from('<H', payload, 8)[0]
        name = payload[10:27].split(b'\x00')[0].decode('ascii', errors='replace')
        stats = struct.unpack_from('<HHHH', payload, 27)

        log.info(f'[CREATE] name="{name}" class={cls} face={face} top={top} bottom={bottom} shoes={shoes} stats={stats}')

        username = session.get('username')
        if not username or username not in self.accounts:
            log.warning(f'[CREATE] No session user')
            return

        # Add new character
        new_char = {
            'name': name,
            'class': cls,
            'level': 1,
            'map': 0,
            'x': 100, 'y': 100,
            'hp': 100, 'mp': 50,
            'face': face, 'top': top, 'bottom': bottom, 'shoes': shoes,
            'str': stats[0], 'dex': stats[1], 'int': stats[2], 'spr': stats[3],
        }
        self.accounts[username]['characters'].append(new_char)
        self._save_accounts()

        # Respond with updated character list (opcode 0x02 login response format)
        account = self.accounts[username]
        resp = self._build_login_success(session, account)
        self._send_encrypted(sock, session, 0x02, resp, use_by_array=no_enc)

    def _handle_login(self, sock, session, payload, no_enc=False):
        """
        Login request (opcode 0x01 from client).
        Payload: CHAR[41] username + CHAR[21] password = 62 bytes
        If client used NoEncode+EncodebyArray, respond in same mode.
        """
        # If we're already logged in, this is a spurious mid-flow re-login —
        # likely triggered by the binary patch at 0x43ED30[0] falling through
        # to login flow. Responding with 0x02 would RESET the client to
        # character select, causing oscillation. Ignore instead.
        if session.get('username'):
            log.info(f'[LOGIN] Ignoring mid-flow 0x01 — already logged in as "{session["username"]}"')
            return

        if len(payload) < 62:
            log.warning(f'[LOGIN] Too short: {len(payload)}B (need 62)')
            log.warning(f'[LOGIN] Raw:\n{hexdump(payload)}')
            self._send_encrypted(sock, session, 0x02, bytes([0x0B]), use_by_array=no_enc)
            return

        username = payload[0:41].split(b'\x00')[0].decode('ascii', errors='replace')
        password = payload[41:62].split(b'\x00')[0].decode('ascii', errors='replace')
        log.info(f'[LOGIN] user="{username}" pass="{password}"')

        account = self.accounts.get(username)
        if not account or account['password'] != password:
            log.info(f'[LOGIN] FAILED for "{username}"')
            self._send_encrypted(sock, session, 0x02, bytes([0x11]), use_by_array=no_enc)
            return

        log.info(f'[LOGIN] SUCCESS for "{username}"')
        session['username'] = username
        # DETERMINISTIC uid (was abs(hash(username)) — random per process due to
        # Python hash randomization). The registration gate at 0x4221A2 requires
        # spawn entity+0x84 (uid) == scene+0x220, and scene+0x220 is written from
        # THIS account_id by the login-success handler (0x44DBB2). The spawn uid
        # must use the SAME value. Use 1 for single-player bring-up.
        session['account_id'] = 1

        resp = self._build_login_success(session, account)
        self._send_encrypted(sock, session, 0x02, resp, use_by_array=no_enc)

    def _build_login_success(self, session, account):
        """
        Login success payload (from disassembly at 0x44D8CA):
          BYTE sub_opcode = 0x01
          UINT32 account_id
          BYTE unknown_1, BYTE has_premium, INT32 premium_flags
          BYTE num_characters
          Per character: CHAR[17] name, BYTE class, BYTE level,
            UINT32 look, UINT32[6] fields, USHORT[14] equip_slots
          BYTE account_status (3=normal)
          BYTE[16] peer_data
        """
        resp = bytearray()
        resp.append(0x01)
        resp.extend(struct.pack('<I', session['account_id']))
        resp.append(0x00)
        resp.append(0x00)
        resp.extend(struct.pack('<i', 0))

        chars = account.get('characters', [])
        resp.append(len(chars))

        for char in chars:
            name = char.get('name', 'Hero').encode('ascii', errors='replace')[:16]
            resp.extend(name + b'\x00' * (17 - len(name)))
            resp.append(char.get('class', 0) & 0xFF)
            resp.append(char.get('level', 1) & 0xFF)
            resp.extend(struct.pack('<I', 0))
            for _ in range(6):
                resp.extend(struct.pack('<I', 0))
            # Appearance fields (14 USHORTs). Zero values render an invisible
            # character. Use non-zero ID for default body+clothes so the
            # character appears on the select screen. Pattern from PySlayer.
            apparences = [
                0,                              # head/chunk
                123,                            # hair
                char.get('face', 1) & 0xFFFF,   # face
                0,                              # ?
                char.get('top', 100) & 0xFFFF,  # top (clothes)
                char.get('bottom', 200) & 0xFFFF,  # bottom (pants)
                char.get('shoes', 300) & 0xFFFF,   # shoes
                0,                              # gloves?
                0,                              # helm
                0,                              # weapon
                0,                              # ?
                0,                              # ?
                0,                              # ?
                0,                              # ?
            ]
            for v in apparences:
                resp.extend(struct.pack('<H', v))

        resp.append(0x03)
        resp.extend(b'\x00' * 16)

        log.info(f'[LOGIN] Response: {len(resp)}B, {len(chars)} char(s)')
        return bytes(resp)


# ============================================================================
# UDP Map Server (port 42907)
# ============================================================================

class UDPMapServer:
    """
    Listens for UDP broadcasts from the client. After processing the enter-world
    response (opcode 0x2B), the client broadcasts opcode 0x11 + account_id UINT
    to 255.255.255.255:42907 via SetBroadCast+SendTo from the tertiary socket.
    """

    def __init__(self, host='0.0.0.0', port=42907):
        self.host = host
        self.port = port

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as e:
            log.warning(f'[UDP] Could not bind {self.host}:{self.port}: {e}')
            return
        log.info(f'[UDP] Listening on {self.host}:{self.port}')

        while True:
            try:
                data, addr = sock.recvfrom(2048)
                log.info(f'[UDP] Got {len(data)}B from {addr}:\n{hexdump(data)}')
                # Try to decode if it's a Fireway packet with NoEncode flag
                if len(data) >= HEADER_SIZE:
                    dw0 = struct.unpack_from('<I', data, 0)[0]
                    no_enc = bool(dw0 & NO_ENCODE_FLAG)
                    if no_enc:
                        buf = bytearray(data)
                        enc = CEncMsg()
                        if enc.decode_by_array(buf):
                            log.info(f'[UDP] Decoded:\n{hexdump(bytes(buf))}')
            except Exception as e:
                log.error(f'[UDP] recv error: {e}')


# ============================================================================
# Main
# ============================================================================

def main():
    log.info('=' * 60)
    log.info('  WindSlayer Private Server')
    log.info('=' * 60)
    log.info('  Version Server: 0.0.0.0:7011')
    log.info('  Game Server:    0.0.0.0:7022 (Fireway) [HARDCODED in client!]')
    log.info('')
    log.info('  Accounts: test/test, admin/admin')
    log.info('=' * 60)

    # Port 7022 is HARDCODED in the English client's ConnectToGameServer function
    # at VA 0x44080E (push 0x1B6E = 7022). The port field in version response
    # channel data is ignored.
    vs = VersionServer(port=7011, game_port=7022)
    threading.Thread(target=vs.start, daemon=True).start()

    # NOTE: UDP port 42907 is the CLIENT's own local bind (CSNSocket::Create).
    # We must NOT bind it here or the client's socket init fails.

    gs = GameServer(port=7022)
    try:
        gs.start()
    except KeyboardInterrupt:
        log.info('Server stopped.')

if __name__ == '__main__':
    main()
