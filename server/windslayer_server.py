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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from cencmsg import CEncMsg

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
                    {'name': 'TestHero', 'level': 1, 'class': 0, 'map': 0,
                     'x': 100, 'y': 100, 'hp': 100, 'mp': 50}
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
                    log.info(f'[FIREWAY] Timeout waiting for client data')
                    break
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
        """Echo 0x0D heartbeat back with MT encryption every time."""
        self._send_encrypted(sock, session, 0x0D, bytes(payload), use_by_array=False)

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

        time.sleep(0.05)
        resp_07 = self._build_pyslayer_opcode_07(session, char)
        log.info(f'[ENTER_WORLD] Sending 0x07 spawn: {len(resp_07)}B')
        self._send_encrypted(sock, session, 0x07, resp_07, use_by_array=no_enc)

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

    def _handle_buy_item(self, sock, session, payload, no_enc):
        """0x0B BUY → respond with 0x18 (got item)."""
        if len(payload) < 4: return
        item = struct.unpack('<H', payload[0:2])[0]
        count = struct.unpack('<H', payload[2:4])[0]
        log.info(f'[BUY] item={item} count={count}')
        # opcode_18: item(2) + count(2)
        body = struct.pack('<H', item) + struct.pack('<H', count)
        self._send_encrypted(sock, session, 0x18, body, use_by_array=no_enc)

    def _handle_sell_item(self, sock, session, payload, no_enc):
        """0x0C SELL → respond with 0x19 (lost item)."""
        if len(payload) < 4: return
        item = struct.unpack('<H', payload[0:2])[0]
        count = struct.unpack('<H', payload[2:4])[0]
        log.info(f'[SELL] item={item} count={count}')
        body = struct.pack('<H', item) + struct.pack('<H', count)
        self._send_encrypted(sock, session, 0x19, body, use_by_array=no_enc)

    def _handle_use_item(self, sock, session, payload, no_enc):
        """0x15 USE ITEM/SKILL → set HP/MP via 0x28/0x44."""
        if len(payload) < 2: return
        item = struct.unpack('<H', payload[0:2])[0]
        log.info(f'[USE] item={item} → applying default heal')
        # Restore HP via opcode_28
        hp = 100
        body_hp = struct.pack('<H', hp)
        self._send_encrypted(sock, session, 0x28, body_hp, use_by_array=no_enc)
        # Restore MP via opcode_44
        body_mp = struct.pack('<H', 50)
        self._send_encrypted(sock, session, 0x44, body_mp, use_by_array=no_enc)

    def _handle_change_map(self, sock, session, payload, no_enc):
        """0x7E CHANGE MAP via portal → respond with 0x08 (and 0x07 spawn at new pos)."""
        if len(payload) < 4: return
        portal_code = struct.unpack('<I', payload[0:4])[0]
        cur_map = session.get('current_map', 101)
        # Look up portal in our table
        portals = self._get_portals()
        key = f'{cur_map}_{portal_code}'
        if key in portals:
            next_map, xpos, ypos = portals[key]
        else:
            log.warning(f'[CHANGE_MAP] unknown portal {portal_code} from map {cur_map}, defaulting to stage01_01')
            next_map, xpos, ypos = 101, 1411.0, 714.0
        session['current_map'] = next_map
        log.info(f'[CHANGE_MAP] map {cur_map} portal {portal_code} → map {next_map} at ({xpos}, {ypos})')
        # Send 0x08 — note no flag byte, mapcode LE
        uid = session.get('account_id', 1) & 0xFFFFFFFF
        body_08 = struct.pack('<H', next_map) + struct.pack('<I', uid) + struct.pack('<I', 0)
        self._send_encrypted(sock, session, 0x08, body_08, use_by_array=no_enc)

    def _get_portals(self):
        """Lazy-load the portal table from JSON."""
        if not hasattr(self, '_portals_cache'):
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'portals.json'), 'r') as f:
                    self._portals_cache = json.load(f)
            except FileNotFoundError:
                self._portals_cache = {}
        return self._portals_cache

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
        body.append(0)                                       # bool
        body.extend(struct.pack('<I', 1000))                 # x again?
        body.extend(b'Mentor'.ljust(17, b'\x00'))            # mentor name
        body.append(0)                                       # var_y
        body.append(10)
        for i in range(5):
            body.extend(struct.pack('<H', 10 + i))           # quest ids
        for i in range(5):
            body.append(10 + i)                              # quest counts
        body.append(35)                                      # equip slots
        body.append(35)                                      # consume slots
        body.append(35)                                      # other slots
        body.append(15)                                      # end-quest count
        for i in range(15):
            body.extend(struct.pack('<H', i))                # quest id
            body.append(1)                                   # complete count
        body.append(0)                                       # equipment lists (empty)
        body.append(0)                                       # 2nd list (empty)
        body.append(0)                                       # 3rd list (empty)
        body.extend(struct.pack('<I', 0))                    # event time
        return bytes(body)

    def _build_pyslayer_opcode_07(self, session, char):
        """
        opcode 0x07 — spawn packet. Ported from PySlayer.
        Player list visible in the current map (just self in single-player).
        """
        body = bytearray()
        body.append(1)                                       # 1 player visible
        # ---- self ----
        name = char.get('name', 'Hero').encode('ascii', errors='replace')[:16]
        body.extend(name + b'\x00' * (17 - len(name)))       # name CHAR[17]
        body.extend(struct.pack('<I', session.get('account_id', 1) & 0xFFFFFFFF))  # uid
        body.extend(struct.pack('<I', 1))                    # ?
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

        # 2026-04-25: EN client's 0x07 handler reads ~1500 more bytes than
        # PySlayer's KR format provides. Pad with zeros to satisfy the parser.
        # Max packet body = 2047 - 8 (header) - 1 (opcode) = 2038. Body so far
        # is ~405; cap padding at ~1600 to stay safely under the limit.
        target_size = 2030  # leaves room for opcode + header
        if len(body) < target_size:
            body.extend(b'\x00' * (target_size - len(body)))
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
        session['account_id'] = abs(hash(username)) & 0x7FFFFFFF

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
