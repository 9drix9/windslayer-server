"""
Build Number Tester
Tries different build numbers in the version response to find the one
that makes the English client skip the update check (HTTP GET for G_UpdateList.h).

Each client launch tests a different build number.
Watch the log to see if the client makes an HTTP request (update mode)
or a Fireway connection (game mode).
"""
import socket
import struct
import threading
import time
import os

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'build_test.log')

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

def make_pkt(body, seq=1):
    total = 8 + len(body)
    dw0 = (total & 0x7FF) | ((seq & 0xFF) << 12)
    return struct.pack('<II', dw0, 0) + body

MSG = b'Press start button to start the game.\n\n\xA9 2009 OUTSPARK.com. All rights reserved.'

def build_version_body(build_num, port=7012):
    """Korean-exact format (proven working with seq=1)"""
    body = bytearray()
    body.append(0x01)
    body.extend(struct.pack('<H', build_num))
    body.extend(struct.pack('<H', len(MSG)))
    body.extend(MSG)
    body.append(0x01)  # server group
    body.append(0x03)  # server type
    body.append(0x01)  # 1 channel
    body.append(0x01)  # max channels
    body.append(0x01)  # channel num
    body.extend(struct.pack('<H', 0))  # 0 players
    body.extend(b'Ch1\x00')
    body.extend(struct.pack('<I', port))
    return bytes(body)

# Build numbers to try, in order
BUILDS = [
    0,      # zero - maybe means "no version"
    1,      # minimal
    275,    # Korean client build (0x0113)
    276,    # Korean client build + 1
    277,    # next
    100,    # round number
    200,    # round number
    300,    # just below Korean server's 301
    301,    # Korean server value (known to trigger update)
    302,    # above Korean server
    999,    # high value
    65535,  # max uint16
]

attempt = [0]

def handle_version(sock, addr):
    idx = attempt[0] % len(BUILDS)
    build = BUILDS[idx]
    attempt[0] += 1

    body = build_version_body(build)
    pkt = make_pkt(body, seq=1)

    log(f'[VER] Connection from {addr} - Build: {build} (0x{build:04X})')
    log(f'[VER] Packet ({len(pkt)} bytes)')
    sock.sendall(pkt)

    # Wait to see if client sends anything back
    sock.settimeout(15.0)
    try:
        data = sock.recv(4096)
        if data:
            log(f'[VER] Recv {len(data)} bytes from client')
            log(hexdump(data))
        else:
            log(f'[VER] Client disconnected (clean)')
    except socket.timeout:
        log(f'[VER] Client timed out (stayed connected 15s)')
    except Exception as e:
        log(f'[VER] Error: {e}')
    sock.close()
    log(f'[VER] Done with build={build}')

game_events = []

def handle_game(sock, addr):
    log(f'[GAME] Connection from {addr}')
    sock.settimeout(5.0)
    try:
        first = sock.recv(4, socket.MSG_PEEK)
        if first and first[:3] in (b'GET', b'POS'):
            # HTTP request = update mode
            data = bytearray()
            while b'\r\n\r\n' not in data:
                chunk = sock.recv(4096)
                if not chunk: break
                data.extend(chunk)
                if len(data) > 8192: break
            req = data.decode('ascii', errors='replace').split('\r\n')[0]
            log(f'[GAME/HTTP] {req}  <-- UPDATE MODE (build mismatch)')
            # Send empty 200 response
            resp = b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
            sock.sendall(resp)
            game_events.append(('HTTP', time.time()))
        else:
            # Fireway connection = game mode!
            log(f'[GAME/FIREWAY] *** GAME MODE! First bytes: {first.hex() if first else "none"} ***')
            game_events.append(('FIREWAY', time.time()))
            # Read whatever the client sends
            all_data = bytearray()
            for _ in range(5):
                try:
                    chunk = sock.recv(4096)
                    if not chunk: break
                    all_data.extend(chunk)
                    log(f'[GAME/FW] Recv {len(chunk)} bytes:')
                    log(hexdump(chunk))
                except socket.timeout:
                    continue
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
    log(' Build Number Tester')
    log(f' Testing builds: {BUILDS}')
    log(' Launch client multiple times to test each build.')
    log(' Watch for FIREWAY vs HTTP on game port.')
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
