"""
Game server probe - try different initial packets to see what the client accepts.
Also runs a version server that works.
"""
import socket
import struct
import threading
import time
import sys
import os

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'game_probe.log')

def log(msg):
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')

def hexdump(data):
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        h = ' '.join(f'{b:02X}' for b in chunk)
        a = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'  {i:04X}: {h:<48s}  {a}')
    return '\n'.join(lines)

def make_packet(opcode, data=b'', seq=0):
    total = 8 + len(data)
    dword0 = (total & 0x7FF) | ((seq & 0xFF) << 12)
    return struct.pack('<II', dword0, opcode) + data

def read_packet(sock, timeout=10.0):
    sock.settimeout(timeout)
    buf = bytearray()
    while len(buf) < 8:
        try:
            chunk = sock.recv(8 - len(buf))
        except:
            return None
        if not chunk:
            return None
        buf.extend(chunk)

    dw0 = struct.unpack_from('<I', buf, 0)[0]
    opcode = struct.unpack_from('<I', buf, 4)[0]
    size = dw0 & 0x7FF

    if size < 8 or size > 0x7FF:
        return ('invalid', bytes(buf), dw0, opcode)

    while len(buf) < size:
        try:
            chunk = sock.recv(size - len(buf))
        except:
            return None
        if not chunk:
            return None
        buf.extend(chunk)

    return ('ok', bytes(buf), size, opcode, bytes(buf[8:]))

# ============================================================================
# Version Server (known working)
# ============================================================================

def build_version_response(game_port=7012):
    payload = bytearray()
    payload.append(0x01)  # result
    payload.extend(struct.pack('<H', 301))  # build
    message = b'Press start button to start the game.\n\n\xA9 2009 OUTSPARK.com. All rights reserved.'
    payload.extend(struct.pack('<H', len(message)))
    payload.extend(message)
    payload.append(0x01)
    payload.append(0x03)
    payload.append(0x01)  # 1 channel
    payload.append(0x01)
    # Channel entry
    payload.append(0x01)  # channel 1
    payload.extend(struct.pack('<H', 0))  # 0 players
    payload.extend(b'Ch1\x00')  # name
    payload.extend(struct.pack('<I', game_port))  # port
    return bytes(payload)

def handle_version(sock, addr):
    log(f'[VERSION] Connection from {addr}')
    data = build_version_response(7012)
    pkt = make_packet(0x0000, data, seq=1)
    sock.sendall(pkt)
    log(f'[VERSION] Sent version response')
    time.sleep(10)
    sock.close()

# ============================================================================
# Game Server - try different approaches
# ============================================================================

attempt_counter = [0]

def handle_game(sock, addr):
    attempt = attempt_counter[0]
    attempt_counter[0] += 1

    log(f'[GAME] Connection from {addr} (attempt {attempt})')

    # Strategy list - try a different one each connection
    strategies = [
        # 0: Raw 4-byte seed (no framing)
        ('raw_seed', lambda: struct.pack('<I', 0x12345678)),
        # 1: Fireway packet opcode 0 with seed
        ('pkt_op0_seed', lambda: make_packet(0x0000, struct.pack('<I', 0x12345678), seq=1)),
        # 2: Fireway packet opcode 0x0111
        ('pkt_op0111', lambda: make_packet(0x0111, struct.pack('<I', 0), seq=1)),
        # 3: Fireway packet opcode 0x0202
        ('pkt_op0202', lambda: make_packet(0x0202, b'', seq=1)),
        # 4: Don't send anything, wait 30 seconds for client
        ('wait_for_client', None),
        # 5: Fireway packet opcode 0 with empty data
        ('pkt_op0_empty', lambda: make_packet(0x0000, b'', seq=1)),
        # 6: Fireway packet opcode 0 with seed and seq=0
        ('pkt_op0_seed_seq0', lambda: make_packet(0x0000, struct.pack('<I', 0x12345678), seq=0)),
        # 7: Two packets: first empty, then seed
        ('two_packets', None),
        # 8: Fireway opcode 1 with seed
        ('pkt_op1_seed', lambda: make_packet(0x0001, struct.pack('<I', 0x12345678), seq=1)),
        # 9: Large packet with server info (like version server)
        ('server_info', None),
    ]

    idx = attempt % len(strategies)
    name, builder = strategies[idx]
    log(f'[GAME] Trying strategy {idx}: {name}')

    sock.settimeout(2.0)

    if name == 'wait_for_client':
        # Don't send anything, just wait
        log(f'[GAME] Waiting for client to send first...')
        start = time.time()
        while time.time() - start < 15.0:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    log(f'[GAME] Client disconnected (empty recv)')
                    sock.close()
                    return
                log(f'[GAME] Received {len(chunk)} bytes:')
                log(hexdump(chunk))
            except socket.timeout:
                continue
        log(f'[GAME] No data from client in 15 seconds')
        sock.close()
        return

    elif name == 'two_packets':
        # Send empty packet, then seed
        pkt1 = make_packet(0x0000, b'', seq=0)
        log(f'[GAME] Sending empty packet:')
        log(hexdump(pkt1))
        sock.sendall(pkt1)
        time.sleep(0.5)
        pkt2 = make_packet(0x0000, struct.pack('<I', 0x12345678), seq=1)
        log(f'[GAME] Sending seed packet:')
        log(hexdump(pkt2))
        sock.sendall(pkt2)

    elif name == 'server_info':
        # Send a server-info-like packet
        payload = bytearray()
        payload.append(0x01)  # some result
        payload.extend(struct.pack('<I', 0x12345678))  # seed
        payload.extend(struct.pack('<I', 0))  # flags
        pkt = make_packet(0x0000, bytes(payload), seq=1)
        log(f'[GAME] Sending server info:')
        log(hexdump(pkt))
        sock.sendall(pkt)

    else:
        payload = builder()
        log(f'[GAME] Sending ({len(payload)} bytes):')
        log(hexdump(payload))
        sock.sendall(payload)

    # Wait for response
    log(f'[GAME] Waiting for client response...')
    all_data = bytearray()
    for _ in range(10):
        try:
            chunk = sock.recv(4096)
            if not chunk:
                log(f'[GAME] Client disconnected')
                break
            all_data.extend(chunk)
            log(f'[GAME] Response! {len(chunk)} bytes:')
            log(hexdump(chunk))
            # If we got data, keep reading
        except socket.timeout:
            continue
        except Exception as e:
            log(f'[GAME] Error: {e}')
            break

    if all_data:
        log(f'[GAME] Total response: {len(all_data)} bytes')
        log(hexdump(bytes(all_data)))
    else:
        log(f'[GAME] No response from client')

    sock.close()
    log(f'[GAME] Done with strategy {name}')

def run_server(port, handler, name):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', port))
    srv.listen(5)
    log(f'[{name}] Listening on port {port}')
    while True:
        client, addr = srv.accept()
        threading.Thread(target=handler, args=(client, addr), daemon=True).start()

def main():
    with open(LOG_FILE, 'w') as f:
        f.write('')

    log('='*60)
    log(' Game Server Probe')
    log(' Each game connection tries a different initial packet')
    log(' Launch the client multiple times to test each strategy')
    log('='*60)

    threading.Thread(target=run_server, args=(7011, handle_version, 'VERSION'), daemon=True).start()
    threading.Thread(target=run_server, args=(7012, handle_game, 'GAME'), daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log('Stopped.')

if __name__ == '__main__':
    main()
