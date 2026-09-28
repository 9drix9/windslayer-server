#!/usr/bin/env python3
"""
test_protocol.py - check the RE protocol spec against real bytes
=================================================================
1. Every grammar in protocol_spec.json parses and round-trips (encode->decode).
2. Every C2S packet the client actually sent (decoded packets in server_live.log
   plus the hand-recorded captures below) decodes EXACTLY with a spec variant of
   its opcode - no trailing bytes, no underflow.
3. Every S2C packet the server actually sent (server_live.log "Encoded:" dumps,
   decrypted with CEncMsg) decodes EXACTLY with the spec. A failure here means
   either the server's builder or the spec is wrong for that opcode.

usage: python test_protocol.py [spec.json] [log ...]   (default log: server_history.log)
"""
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from wsproto import Grammar, GrammarError, hexbytes  # noqa: E402
from cencmsg import CEncMsg  # noqa: E402

DEFAULT_SPEC = os.path.join(HERE, 'protocol_spec.json')

# Hand-recorded client captures (payload after the opcode byte), 2026-09-17.
MANUAL_C2S = [
    (0x03, '02 68 69'),
    (0x16, '1A 00'),
    (0x0F, 'B3 00 00 00 00'),
    (0x74, ''), (0x75, ''), (0x2C, ''), (0x2D, ''), (0x38, ''), (0x2F, ''), (0x63, ''),
    (0x7E, '17 00 00 00'),
]


def load_specs(path):
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    if 'result' in d:
        d = d['result']
    entries = d.get('specs') or [s for r in d.get('results', []) for s in r['agreed'] + r['adjudicated']]
    by = {'S2C': defaultdict(list), 'C2S': defaultdict(list)}
    for s in entries:
        if s.get('grammar') is None or s.get('direction') not in by or str(s.get('key', '')).startswith('UDP-'):
            continue
        for op in re.findall(r'0x[0-9A-Fa-f]{1,2}', s.get('opcode', '')):
            by[s['direction']][int(op, 16)].append(s)
    return entries, by


def log_packets(path):
    """Yield ('C2S'|'S2C', opcode, payload bytes) from a server log."""
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = f.read().splitlines()

    def hexblock(j):
        out = []
        while j < len(lines) and re.match(r'\s+[0-9A-F]{4}:', lines[j]):
            out += re.findall(r'\b[0-9A-F]{2}\b', lines[j].split(':', 1)[1][:49])
            j += 1
        return out

    for i, line in enumerate(lines):
        m = re.search(r'\[FIREWAY\] Pkt: opcode=0x([0-9A-Fa-f]+) size=(\d+) seq=\d+ no_enc=\w+ payload=(\d+)B', line)
        if m:
            j = i - 1
            while j >= 0 and re.match(r'\s+[0-9A-F]{4}:', lines[j]):
                j -= 1
            hx = hexblock(j + 1)
            plen = int(m.group(3))
            if len(hx) >= 9 + plen:
                yield 'C2S', int(m.group(1), 16), bytes(int(x, 16) for x in hx[9:9 + plen])
            continue
        m = re.search(r'Send: opcode=0x([0-9A-Fa-f]+) seq=\d+ size=(\d+) by_array=True', line)
        if m and i + 1 < len(lines) and 'Encoded' in lines[i + 1]:
            size = int(m.group(2))
            hx = hexblock(i + 2)
            if len(hx) < size:
                continue
            pkt = bytearray(int(x, 16) for x in hx[:size])
            if CEncMsg().decode_by_array(pkt):
                yield 'S2C', int(m.group(1), 16), bytes(pkt[9:])


def try_decode(specs, payload):
    """Return (ok, detail). ok if any variant decodes exactly."""
    errors = []
    for s in specs:
        try:
            Grammar(s['grammar']).decode(payload)
            return True, s.get('key')
        except (GrammarError, Exception) as e:           # noqa: BLE001 - report every failure mode
            errors.append(f'{s.get("key")}: {e}')
    return False, '; '.join(errors)


def main():
    spec_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SPEC
    logs = sys.argv[2:] or [os.path.join(HERE, 'server_history.log')]
    if not any(os.path.exists(p) for p in logs):
        logs = [os.path.join(HERE, 'server_live.log')]
    entries, by = load_specs(spec_path)
    fails = 0

    print(f'== grammars ({len(entries)} specs)')
    for s in entries:
        if s.get('grammar') is None:
            continue
        try:
            g = Grammar(s['grammar'])
            g.decode(g.encode({}), allow_trailing=False)
        except Exception as e:                            # noqa: BLE001
            fails += 1
            print(f'  FAIL parse/roundtrip {s.get("key")} {s.get("name")}: {e}')

    samples = [('C2S', op, hexbytes(hx) if hx else b'') for op, hx in MANUAL_C2S]
    for lp in logs:
        if os.path.exists(lp):
            samples += list(log_packets(lp))
    seen = defaultdict(lambda: [0, 0, None])            # (dir, op) -> [ok, bad, example]
    for direction, op, payload in samples:
        specs = by[direction].get(op)
        rec = seen[(direction, op)]
        if not specs:
            rec[2] = rec[2] or 'NO SPEC'
            rec[1] += 1
            continue
        ok, detail = try_decode(specs, payload)
        if ok:
            rec[0] += 1
        else:
            rec[1] += 1
            rec[2] = rec[2] or f'{len(payload)}B {payload[:48].hex(" ")} -> {detail[:300]}'

    print(f'== real packets ({len(samples)} samples, {len(seen)} distinct direction/opcode)')
    for (direction, op), (ok, bad, ex) in sorted(seen.items()):
        status = 'PASS' if bad == 0 else 'FAIL'
        if bad:
            fails += 1
        print(f'  {status} {direction} 0x{op:02X}  exact={ok} mismatched={bad}' + (f'\n       {ex}' if bad else ''))
    print(f'\n{"ALL PASS" if fails == 0 else f"{fails} FAILURE(S)"}')
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
