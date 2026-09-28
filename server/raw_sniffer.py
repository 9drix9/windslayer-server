"""
Raw TCP sniffer - captures every single byte from the client.
No assumptions about packet format - just logs everything raw.
"""
import socket
import struct
import threading
import time
import sys
import os

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sniffer.log')

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

def handle_version(sock, addr):
    name = 'VERSION'
    log(f'[{name}] === Connection from {addr} ===')

    sock.settimeout(2.0)
    all_data = bytearray()

    # Wait to see if client sends first
    log(f'[{name}] Waiting for client data...')
    got_data = False
    for _ in range(3):  # 3 x 2 sec = 6 sec
        try:
            chunk = sock.recv(4096)
            if not chunk:
                log(f'[{name}] Client disconnected')
                sock.close()
                return
            all_data.extend(chunk)
            log(f'[{name}] Received {len(chunk)} bytes:')
            log(hexdump(chunk))
            got_data = True
            # Parse as Fireway
            if len(all_data) >= 8:
                dw0 = struct.unpack_from('<I', all_data, 0)[0]
                dw1 = struct.unpack_from('<I', all_data, 4)[0]
                pkt_size = dw0 & 0x7FF
                log(f'[{name}] Fireway parse: size={pkt_size} opcode=0x{dw1:08X}')
            break
        except socket.timeout:
            log(f'[{name}] No data yet (timeout)')
            continue

    if not got_data:
        log(f'[{name}] Client sent nothing - server must speak first')

    # Now try multiple response strategies
    strategies = [
        # Strategy A: Simple Fireway packet opcode=0, no data
        ('Empty packet opcode 0', struct.pack('<II', 8, 0)),
        # Strategy B: Fireway packet with 4-byte zero response
        ('Packet opcode 0 + 4 zero bytes', struct.pack('<II', 12, 0) + b'\x00\x00\x00\x00'),
        # Strategy C: Raw seed (4 bytes, no framing)
        ('Raw 4-byte seed', struct.pack('<I', int(time.time()*1000) & 0xFFFFFFFF)),
        # Strategy D: Seed in Fireway packet
        ('Fireway seed packet', struct.pack('<II', 12, 0) + struct.pack('<I', int(time.time()*1000) & 0xFFFFFFFF)),
    ]

    for desc, payload in strategies:
        log(f'[{name}] Trying: {desc}')
        log(f'[{name}] Sending:')
        log(hexdump(payload))
        try:
            sock.sendall(payload)
        except Exception as e:
            log(f'[{name}] Send failed: {e}')
            sock.close()
            return

        # Wait for response
        time.sleep(1.0)
        try:
            resp = sock.recv(4096)
            if resp:
                all_data.extend(resp)
                log(f'[{name}] Got response! {len(resp)} bytes:')
                log(hexdump(resp))
                if len(resp) >= 8:
                    dw0 = struct.unpack_from('<I', resp, 0)[0]
                    dw1 = struct.unpack_from('<I', resp, 4)[0]
                    log(f'[{name}] Fireway: size={dw0 & 0x7FF} op=0x{dw1:08X}')
                # Don't try more strategies if we got a response
                break
            else:
                log(f'[{name}] Client disconnected after {desc}')
                sock.close()
                return
        except socket.timeout:
            log(f'[{name}] No response to {desc}')
            continue
        except Exception as e:
            log(f'[{name}] Error: {e}')
            sock.close()
            return

    # Keep reading
    log(f'[{name}] Reading remaining data...')
    for _ in range(10):
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            all_data.extend(chunk)
            log(f'[{name}] More data ({len(chunk)} bytes):')
            log(hexdump(chunk))
        except socket.timeout:
            continue
        except:
            break

    log(f'[{name}] Total received: {len(all_data)} bytes')
    sock.close()

def handle_game(sock, addr):
    name = 'GAME'
    log(f'[{name}] === Connection from {addr} ===')

    sock.settimeout(2.0)
    all_data = bytearray()

    # Read everything for 15 seconds
    start = time.time()
    while time.time() - start < 15.0:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                log(f'[{name}] Client disconnected')
                break
            all_data.extend(chunk)
            log(f'[{name}] Received {len(chunk)} bytes:')
            log(hexdump(chunk))
        except socket.timeout:
            continue
        except:
            break

    log(f'[{name}] Total received: {len(all_data)} bytes')
    sock.close()

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
    # Clear old log
    with open(LOG_FILE, 'w') as f:
        f.write('')

    log('='*60)
    log(' WindSlayer Raw Packet Sniffer')
    log('='*60)

    threading.Thread(target=run_server, args=(7011, handle_version, 'VERSION'), daemon=True).start()
    threading.Thread(target=run_server, args=(7012, handle_game, 'GAME'), daemon=True).start()

    log('Servers ready. Launch the game!')

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log('Stopped.')

if __name__ == '__main__':
    main()
