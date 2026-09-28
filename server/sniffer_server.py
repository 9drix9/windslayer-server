"""
WindSlayer Packet Sniffer Server
================================
Captures and logs all raw data from the client to understand the protocol.
Runs on port 7011 (version server) and logs everything.
"""

import socket
import struct
import threading
import time
import sys

def hexdump(data, prefix='  '):
    """Pretty hex dump of binary data."""
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = ' '.join(f'{b:02X}' for b in chunk)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'{prefix}{i:04X}: {hex_part:<48s}  {ascii_part}')
    return '\n'.join(lines)

def log(msg):
    ts = time.strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with open('sniffer.log', 'a') as f:
        f.write(line + '\n')

def handle_version_client(sock, addr):
    """Handle connection to version server (port 7011)."""
    log(f'VERSION: Connection from {addr}')
    sock.settimeout(30.0)

    all_data = bytearray()
    try:
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    log(f'VERSION: Client {addr} disconnected (clean)')
                    break
                all_data.extend(chunk)
                log(f'VERSION: Received {len(chunk)} bytes from {addr} (total: {len(all_data)})')
                log(f'VERSION: Raw hex:\n{hexdump(chunk)}')

                # Try to parse as Fireway packet
                if len(all_data) >= 8:
                    size_dword = struct.unpack_from('<I', all_data, 0)[0]
                    second_dword = struct.unpack_from('<I', all_data, 4)[0]
                    pkt_size = size_dword & 0x7FF
                    flags_high = (size_dword >> 11) & 0x1FFFFF
                    log(f'VERSION: Packet header analysis:')
                    log(f'  DWORD[0] = 0x{size_dword:08X} (size={pkt_size}, flags=0x{flags_high:05X})')
                    log(f'  DWORD[1] = 0x{second_dword:08X}')
                    if pkt_size >= 8 and pkt_size <= len(all_data):
                        data_portion = all_data[8:pkt_size]
                        log(f'  Data ({len(data_portion)} bytes):')
                        if data_portion:
                            log(hexdump(data_portion))
                            # Try decoding as ASCII
                            try:
                                ascii_text = data_portion.decode('ascii', errors='replace')
                                log(f'  ASCII: {repr(ascii_text)}')
                            except:
                                pass

                # Try sending various version responses to see what the client expects
                # Strategy: send a minimal response and see if client sends more data

                # Wait for the full packet before responding
                if len(all_data) >= 8:
                    sd = struct.unpack_from('<I', all_data, 0)[0]
                    ps = sd & 0x7FF
                    if ps >= 8 and ps <= len(all_data):
                        log(f'VERSION: Complete packet received ({ps} bytes). Sending responses...')

                        # Try Response 1: Echo-like with modified opcode
                        # The version server likely sends back version info
                        # Based on the client strings:
                        #   "Downloading update list..."
                        #   "version="
                        #   "Rearranging name of update list..."
                        #
                        # The client seems to expect an update list in response
                        # Let's try sending a simple "no updates" response

                        # Response format attempt: simple acknowledgment
                        # Just the 8-byte header with a status code
                        for attempt, resp in enumerate(generate_version_responses()):
                            try:
                                log(f'VERSION: Sending response attempt #{attempt}:')
                                log(hexdump(resp))
                                sock.sendall(resp)
                                time.sleep(0.5)

                                # Check for follow-up data
                                try:
                                    sock.settimeout(3.0)
                                    more = sock.recv(4096)
                                    if more:
                                        all_data.extend(more)
                                        log(f'VERSION: Follow-up data ({len(more)} bytes):')
                                        log(hexdump(more))
                                except socket.timeout:
                                    log(f'VERSION: No follow-up after response #{attempt}')
                                    continue

                            except Exception as e:
                                log(f'VERSION: Send error: {e}')
                                break
                        break

            except socket.timeout:
                log(f'VERSION: Timeout waiting for data from {addr}')
                break

    except Exception as e:
        log(f'VERSION: Error handling {addr}: {e}')

    finally:
        log(f'VERSION: Total data received from {addr}: {len(all_data)} bytes')
        if all_data:
            log(f'VERSION: Complete dump:\n{hexdump(all_data)}')
        sock.close()


def generate_version_responses():
    """Generate various response attempts for the version server."""
    responses = []

    # Response 1: Minimal header-only packet (size=8, opcode=0)
    resp1 = struct.pack('<II', 8, 0)
    responses.append(resp1)

    # Response 2: Packet with result code 0 (success/no update needed)
    data2 = struct.pack('<I', 0)  # result = 0 (OK)
    size2 = 8 + len(data2)
    resp2 = struct.pack('<II', size2, 0) + data2
    responses.append(resp2)

    # Response 3: Packet with version string and server info
    # The client looks for "version=" in the response
    ver_str = b'version="1.0.0" \x00'
    size3 = 8 + len(ver_str)
    resp3 = struct.pack('<II', size3, 1) + ver_str
    responses.append(resp3)

    # Response 4: Packet mimicking an update list response (empty list = no updates)
    # The update list format is likely XML or a simple text format
    update_data = struct.pack('<I', 0)  # 0 files to update
    size4 = 8 + len(update_data)
    resp4 = struct.pack('<II', size4, 2) + update_data
    responses.append(resp4)

    return responses


def handle_game_client(sock, addr):
    """Handle connection to game server (port 7012)."""
    log(f'GAME: Connection from {addr}')
    sock.settimeout(30.0)

    all_data = bytearray()
    try:
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    log(f'GAME: Client {addr} disconnected')
                    break
                all_data.extend(chunk)
                log(f'GAME: Received {len(chunk)} bytes from {addr}')
                log(f'GAME: Raw hex:\n{hexdump(chunk)}')

                if len(all_data) >= 8:
                    size_dword = struct.unpack_from('<I', all_data, 0)[0]
                    second_dword = struct.unpack_from('<I', all_data, 4)[0]
                    pkt_size = size_dword & 0x7FF
                    log(f'GAME: DWORD[0]=0x{size_dword:08X} (size={pkt_size}) DWORD[1]=0x{second_dword:08X}')

            except socket.timeout:
                log(f'GAME: Timeout from {addr}')
                break

    except Exception as e:
        log(f'GAME: Error: {e}')
    finally:
        log(f'GAME: Total from {addr}: {len(all_data)} bytes')
        sock.close()


def run_server(port, handler_func, name):
    """Run a TCP server on the given port."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', port))
    srv.listen(5)
    log(f'{name} listening on port {port}')

    while True:
        try:
            client_sock, addr = srv.accept()
            threading.Thread(target=handler_func, args=(client_sock, addr), daemon=True).start()
        except Exception as e:
            log(f'{name} accept error: {e}')


def main():
    # Clear old log
    with open('sniffer.log', 'w') as f:
        f.write('')

    log('=' * 60)
    log('WindSlayer Packet Sniffer Server')
    log('=' * 60)

    # Start version server
    t1 = threading.Thread(target=run_server, args=(7011, handle_version_client, 'VERSION-SRV'), daemon=True)
    t1.start()

    # Start game server
    t2 = threading.Thread(target=run_server, args=(7012, handle_game_client, 'GAME-SRV'), daemon=True)
    t2.start()

    log('Servers started. Launch the game client now.')
    log('Press Ctrl+C to stop.')

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log('Shutting down.')


if __name__ == '__main__':
    main()
