"""Listen on many ports to find where the client connects."""
import socket
import threading
import time

def listen(port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', port))
        s.listen(5)
        print(f'Listening on {port}')
        while True:
            try:
                c, addr = s.accept()
                print(f'*** GOT CONNECTION ON PORT {port} from {addr} at {time.time():.3f}')
                try:
                    c.settimeout(5)
                    data = c.recv(256)
                    if data:
                        print(f'  Port {port} received: {data[:64].hex()}')
                except:
                    pass
                c.close()
            except Exception as e:
                print(f'Port {port} error: {e}')
                break
    except Exception as e:
        print(f'Port {port} bind failed: {e}')

# Listen on a range of ports commonly used by games
# Skip 7011 (our version server), try others
ports = [
    7010, 7013, 7014, 7015,          # near version server
    8011, 8012, 9011, 9012,          # alternate ranges
    5555, 6000, 6121, 7000, 7100,    # game-common ports
    10011, 10012, 11011, 11012,      # higher ranges
    14001, 14002, 15001,             # other game ports
]

for p in ports:
    threading.Thread(target=listen, args=(p,), daemon=True).start()
    time.sleep(0.05)

print(f'\nListening on {len(ports)} ports. Press Ctrl+C to stop.')
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print('\nStopped.')
