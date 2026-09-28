"""
Listen on a WIDE range of ports to find where the game client connects.
Run this alongside the main server. Don't conflict with 7011/7012.
"""
import socket
import threading
import time
import sys

LOG = 'multi_port.log'
logf = open(LOG, 'w', buffering=1)

def log(msg):
    line = f'{time.strftime("%H:%M:%S")} {msg}'
    print(line)
    logf.write(line + '\n')

def listen(port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', port))
        s.listen(5)
        while True:
            try:
                c, addr = s.accept()
                log(f'*** CONNECTION on port {port} from {addr}')
                try:
                    c.settimeout(3)
                    data = c.recv(256)
                    if data:
                        log(f'    port {port} data: {data[:64].hex()}')
                except:
                    pass
                c.close()
            except Exception:
                break
    except Exception as e:
        pass  # skip ports we can't bind

# Listen on wide range, skipping 7011/7012 (main server uses those)
ports = []
# Near 7011/7012
ports += [7010, 7013, 7014, 7015, 7016, 7017, 7018, 7019, 7020]
# Common game server ports
ports += list(range(7000, 7011))
ports += list(range(7050, 7100))
ports += list(range(7100, 7200, 5))
ports += [7777, 8000, 8001, 8080, 9000, 9001, 9100, 10000, 10001]
ports += [5555, 6666, 12345, 14001, 15001]

log(f'Listening on {len(ports)} ports...')

threads = []
for p in ports:
    t = threading.Thread(target=listen, args=(p,), daemon=True)
    t.start()
    threads.append(t)
    time.sleep(0.01)

log('Ready. Waiting for connections...')

try:
    while True:
        time.sleep(5)
except KeyboardInterrupt:
    log('Stopped')
    logf.close()
