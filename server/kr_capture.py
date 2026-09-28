"""
Captures TCP packets between this machine and the Korean WindSlayer server
(Sesisoft, 223.130.135.178). Saves a timestamped log of every packet with
direction, size, and raw bytes. Run as ADMINISTRATOR.

Usage:
  1. Right-click Command Prompt → Run as administrator
  2. cd C:\\Users\\ohdri\\Desktop\\WindSlayer2Game\\server
  3. python kr_capture.py
  4. In a separate window, launch the Korean client and play through
     login → character select → enter world → loading screen.
  5. When capture is done, press Ctrl+C in the capture window.
  6. Output goes to: kr_capture_YYYYMMDD_HHMMSS.txt
"""
import socket
import struct
import time
import os
import sys
import ctypes

KR_SERVER_IP = '223.130.135.178'
KR_PORTS = (7011, 7012, 7022)  # version + possible game ports

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def parse_ipv4(pkt):
    """Return (src_ip, dst_ip, protocol, tcp_src, tcp_dst, tcp_flags, payload)."""
    if len(pkt) < 20:
        return None
    version_ihl = pkt[0]
    ver = version_ihl >> 4
    ihl = (version_ihl & 0x0F) * 4
    if ver != 4 or ihl < 20:
        return None
    proto = pkt[9]
    total_len = struct.unpack('>H', pkt[2:4])[0]
    src = '.'.join(str(b) for b in pkt[12:16])
    dst = '.'.join(str(b) for b in pkt[16:20])
    if proto != 6:  # only TCP
        return (src, dst, proto, None, None, None, pkt[ihl:total_len])
    # Parse TCP header
    tcp = pkt[ihl:total_len]
    if len(tcp) < 20:
        return None
    tcp_src = struct.unpack('>H', tcp[0:2])[0]
    tcp_dst = struct.unpack('>H', tcp[2:4])[0]
    data_off = (tcp[12] >> 4) * 4
    flags = tcp[13]
    payload = tcp[data_off:]
    return (src, dst, proto, tcp_src, tcp_dst, flags, payload)

def hexdump(data, max_bytes=256):
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

def main():
    if not is_admin():
        print('ERROR: Must run as administrator. Raw sockets require admin rights on Windows.')
        print('Right-click Command Prompt / Terminal → Run as administrator')
        sys.exit(1)

    # Find local IPv4 (outbound interface)
    t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        t.connect((KR_SERVER_IP, 80))
        local_ip = t.getsockname()[0]
    finally:
        t.close()
    print(f'[INFO] Local IP: {local_ip}')
    print(f'[INFO] Watching for traffic to/from {KR_SERVER_IP} (ports {KR_PORTS})')

    # Raw socket sniffer on Windows uses SOCK_RAW with IPPROTO_IP
    sniff = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
    sniff.bind((local_ip, 0))
    # Enable promiscuous mode
    sniff.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
    SIO_RCVALL = 0x98000001
    RCVALL_ON = 1
    sniff.ioctl(SIO_RCVALL, RCVALL_ON)

    outpath = f'kr_capture_{time.strftime("%Y%m%d_%H%M%S")}.txt'
    print(f'[INFO] Writing capture to: {outpath}')
    print('[INFO] Launch the Korean client now. Ctrl+C to stop.')

    pkt_count = 0
    try:
        with open(outpath, 'w', encoding='utf-8') as f:
            f.write(f'# Korean WindSlayer TCP capture\n')
            f.write(f'# Server: {KR_SERVER_IP}\n')
            f.write(f'# Started: {time.strftime("%Y-%m-%d %H:%M:%S")}\n\n')
            f.flush()
            while True:
                try:
                    pkt, addr = sniff.recvfrom(65535)
                except Exception as e:
                    print(f'[WARN] recv error: {e}')
                    continue
                parsed = parse_ipv4(pkt)
                if not parsed: continue
                src, dst, proto, sport, dport, flags, payload = parsed
                # Filter to KR server traffic only
                if src != KR_SERVER_IP and dst != KR_SERVER_IP:
                    continue
                # Direction
                if src == local_ip and dst == KR_SERVER_IP:
                    direction = 'CLIENT->SERVER'
                elif dst == local_ip and src == KR_SERVER_IP:
                    direction = 'SERVER->CLIENT'
                else:
                    continue
                pkt_count += 1
                ts = time.strftime('%H:%M:%S') + f'.{int((time.time()%1)*1000):03d}'
                flag_s = []
                if flags & 0x01: flag_s.append('FIN')
                if flags & 0x02: flag_s.append('SYN')
                if flags & 0x04: flag_s.append('RST')
                if flags & 0x08: flag_s.append('PSH')
                if flags & 0x10: flag_s.append('ACK')
                flag_str = '+'.join(flag_s)
                line = (f'[{ts}] {direction}  '
                        f'{src}:{sport} -> {dst}:{dport}  '
                        f'[{flag_str}]  payload={len(payload)}B\n')
                f.write(line)
                if payload:
                    f.write(hexdump(payload))
                    f.write('\n')
                f.write('\n')
                f.flush()
                # Also console echo (short)
                if pkt_count % 5 == 0:
                    print(f'[{pkt_count}] captured ({direction} {len(payload)}B)')
    except KeyboardInterrupt:
        print(f'\n[INFO] Stopped. Captured {pkt_count} packets. Output: {outpath}')
    finally:
        try:
            sniff.ioctl(SIO_RCVALL, 0)
            sniff.close()
        except Exception:
            pass

if __name__ == '__main__':
    main()
