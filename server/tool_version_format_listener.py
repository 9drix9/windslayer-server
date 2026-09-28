"""
# NOTE: NOT a unit test. Binds 7011/7012 with SO_REUSEADDR and answers version requests
# (renamed from test_formats.py on 2026-09-17 so test_*.py globs never start it).
Test different version response formats.
Each client connection tries a different format.
"""
import socket
import struct
import threading
import time
import os

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'format_test.log')

def log(msg):
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')

def hexdump(data):
    lines = []
    for i in range(0, len(data), 16):
        c = data[i:i+16]
        h = ' '.join(f'{b:02X}' for b in c)
        a = ''.join(chr(b) if 32 <= b < 127 else '.' for b in c)
        lines.append(f'  {i:04X}: {h:<48s}  {a}')
    return '\n'.join(lines)

def make_pkt(body, seq=0):
    total = 8 + len(body)
    dw0 = (total & 0x7FF) | ((seq & 0xFF) << 12)
    return struct.pack('<II', dw0, 0) + body

MSG = b'Press start button to start the game.\n\n\xA9 2009 OUTSPARK.com. All rights reserved.'

def build_korean_exact(port=7012):
    """Exact Korean server format: 01 2D 01 51 00 [msg] 01 03 01 01 [ch]"""
    body = bytearray()
    body.append(0x01)                          # byte field
    body.extend(struct.pack('<H', 301))        # uint16 = 0x012D
    body.extend(struct.pack('<H', len(MSG)))   # uint16 = msg length
    body.extend(MSG)
    body.append(0x01)
    body.append(0x03)
    body.append(0x01)  # 1 channel
    body.append(0x01)
    # Channel entry: num(1) + players(2) + name(4) + port(4) = 11 bytes
    body.append(0x01)
    body.extend(struct.pack('<H', 0))
    body.extend(b'Ch1\x00')
    body.extend(struct.pack('<I', port))
    return bytes(body)

def build_result3_format(port=7012):
    """Linter's format: opcode=01, result=3, build=301, msg, channels"""
    body = bytearray()
    body.append(0x01)                          # opcode
    body.extend(struct.pack('<H', 3))          # result = 3
    body.extend(struct.pack('<H', 301))        # build = 301
    body.extend(struct.pack('<H', len(MSG)))
    body.extend(MSG)
    body.append(0x01)
    body.append(0x03)
    body.append(0x01)
    body.append(0x01)
    body.append(0x01)
    body.extend(struct.pack('<H', 0))
    body.extend(b'Ch1\x00')
    body.extend(struct.pack('<I', port))
    return bytes(body)

attempt = [0]

STRATEGIES = [
    # (name, body, seq)
    ("korean_exact_seq0", lambda: build_korean_exact(), 0),
    ("korean_exact_seq1", lambda: build_korean_exact(), 1),
    ("result3_seq0", lambda: build_result3_format(), 0),
    ("result3_seq1", lambda: build_result3_format(), 1),
]

def handle_version(sock, addr):
    idx = attempt[0] % len(STRATEGIES)
    attempt[0] += 1
    name, body_fn, seq = STRATEGIES[idx]
    body = body_fn()
    pkt = make_pkt(body, seq)

    log(f'[VER] Connection from {addr} - Strategy: {name}')
    log(f'[VER] Packet ({len(pkt)} bytes):')
    log(hexdump(pkt))
    sock.sendall(pkt)

    # Wait for response or disconnect
    sock.settimeout(5.0)
    for _ in range(3):
        try:
            data = sock.recv(4096)
            if data:
                log(f'[VER] Recv {len(data)} bytes:')
                log(hexdump(data))
            else:
                log(f'[VER] Client disconnected')
                break
        except socket.timeout:
            continue
        except:
            break

    sock.close()
    log(f'[VER] Done with {name}')

def handle_game(sock, addr):
    log(f'[GAME] Connection from {addr}')
    sock.settimeout(2.0)

    # Peek first bytes
    try:
        first = sock.recv(4, socket.MSG_PEEK)
        if first and first[:3] in (b'GET', b'POS'):
            # HTTP
            data = bytearray()
            while b'\r\n\r\n' not in data:
                chunk = sock.recv(4096)
                if not chunk: break
                data.extend(chunk)
                if len(data) > 8192: break
            req = data.decode('ascii', errors='replace').split('\r\n')[0]
            log(f'[GAME/HTTP] {req}')
            # Respond with empty update list
            resp = b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
            sock.sendall(resp)
            log(f'[GAME/HTTP] Sent empty response')
        else:
            log(f'[GAME/FW] Fireway connection! First bytes: {first.hex() if first else "none"}')
            # Read more
            all_data = bytearray()
            for _ in range(15):
                try:
                    chunk = sock.recv(4096)
                    if not chunk: break
                    all_data.extend(chunk)
                    log(f'[GAME/FW] Recv {len(chunk)} bytes:')
                    log(hexdump(chunk))
                except socket.timeout:
                    continue
            if not all_data:
                log(f'[GAME/FW] No data from client')
    except Exception as e:
        log(f'[GAME] Error: {e}')
    sock.close()
    log(f'[GAME] Done')

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
    with open(LOG, 'w') as f: f.write('')
    log('='*60)
    log(' Version Response Format Tester')
    log(' Each connection tries a different format')
    log(f' Strategies: {[s[0] for s in STRATEGIES]}')
    log('='*60)
    threading.Thread(target=run_server, args=(7011, handle_version, 'VER'), daemon=True).start()
    threading.Thread(target=run_server, args=(7012, handle_game, 'GAME'), daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log('Stopped.')

if __name__ == '__main__':
    main()
