"""
build_data.py — convert raw asset dumps into JSON tables consumed by the server.

Inputs (in C:/Users/ohdri/Downloads):
  - message.txt     : 5 packet-handler files + opcode lists
  - message(1).txt  : map code lookup table (markdown)
  - message(2).txt  : 4609-row item table   (markdown)
  - NPC.xlsx        : 320-row NPC/mob spreadsheet

Outputs (in this directory):
  - opcodes.json    : { "0x01": "Main_Tcp_Handler", ... }
  - map_codes.json  : { "101": { xml_mapcode, name, file_name }, ... }
  - items.json      : { "1":   { title, text, type, sprite, hp, mp }, ... }
  - npcs.json       : { "1":   { idx, hsifile, AI, ... } }

Run from anywhere; paths are absolute. UTF-8 throughout (Korean text preserved).
"""

import json
import os
import re
import sys

DOWNLOADS = r'C:\Users\ohdri\Downloads'
OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def parse_opcodes():
    """Parse message.txt → opcode → handler name map."""
    src = os.path.join(DOWNLOADS, 'message.txt')
    with open(src, 'r', encoding='utf-8') as f:
        text = f.read()

    handler_pat = re.compile(r'^([A-Za-z_]+\.c):\s*\d+\s*$', re.M)
    handlers = list(handler_pat.finditer(text))

    out = {}
    for i, m in enumerate(handlers):
        handler_name = m.group(1).replace('.c', '')
        if handler_name == 'All_opcode':
            continue
        start = m.end()
        end = handlers[i + 1].start() if i + 1 < len(handlers) else len(text)
        chunk = text[start:end]
        for hex_match in re.finditer(r'0x[0-9a-fA-F]+', chunk):
            opcode = int(hex_match.group(0), 16)
            key = f'0x{opcode:02X}'
            if key in out:
                # Same opcode in two handlers — record both (rare but possible).
                if handler_name not in out[key].split(','):
                    out[key] = f'{out[key]},{handler_name}'
            else:
                out[key] = handler_name

    # Also capture the full unique opcode list as a sanity check.
    all_match = re.search(r'^All_opcode:\s*(\d+)\s*$', text, re.M)
    expected_total = int(all_match.group(1)) if all_match else None
    return out, expected_total


def parse_map_codes():
    """Parse message(1).txt → file_mapcode → {xml_mapcode, name, file_name}."""
    src = os.path.join(DOWNLOADS, 'message(1).txt')
    with open(src, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    out = {}
    for line in lines:
        line = line.strip()
        if not line.startswith('|') or '---' in line:
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) < 4:
            continue
        if cells[0] in ('file_mapcode',):  # header
            continue
        try:
            file_mapcode = int(cells[0])  # "0101" -> 101 ; "0400" -> 400
            xml_mapcode = int(cells[1])
        except ValueError:
            continue
        out[str(file_mapcode)] = {
            'xml_mapcode': xml_mapcode,
            'name': cells[2],
            'file_name': cells[3],
        }
    return out


def parse_items():
    """Parse message(2).txt → idx → {title, text, type, sprite, hp, mp}."""
    src = os.path.join(DOWNLOADS, 'message(2).txt')
    with open(src, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    out = {}
    for line in lines:
        line = line.rstrip('\n')
        if not line.startswith('|') or '---' in line:
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) < 7:
            continue
        if cells[0] in ('idx',):
            continue
        try:
            idx = int(cells[0])
            type_ = int(cells[3])
            sprite = int(cells[4])
            hp = int(cells[5])
            mp = int(cells[6])
        except ValueError:
            continue
        out[str(idx)] = {
            'title': cells[1],
            'text': cells[2].replace('\\n', '\n'),
            'type': type_,
            'sprite': sprite,
            'hp': hp,
            'mp': mp,
        }
    return out


def parse_npcs():
    """Parse NPC.xlsx → idx → {all columns}. Uses openpyxl read-only mode."""
    import openpyxl
    src = os.path.join(DOWNLOADS, 'NPC.xlsx')
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]

    rows = ws.iter_rows(values_only=True)
    headers = [str(h) if h is not None else '' for h in next(rows)]

    out = {}
    for row in rows:
        if not row or row[0] is None:
            continue
        try:
            idx = int(row[0])
        except (ValueError, TypeError):
            continue
        record = {}
        for i, h in enumerate(headers):
            if not h:
                continue
            v = row[i] if i < len(row) else None
            if v is None:
                record[h] = None
            elif isinstance(v, (int, float, str, bool)):
                record[h] = v
            else:
                record[h] = str(v)
        out[str(idx)] = record
    return out


def write_json(name, data):
    path = os.path.join(OUT_DIR, name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
    size = os.path.getsize(path)
    print(f'  wrote {name:<18s} {len(data):>5d} entries  ({size:>9d} B)')


def main():
    print('Parsing data sources...')

    print('[1/4] opcodes.json')
    opcodes, total = parse_opcodes()
    write_json('opcodes.json', opcodes)
    if total is not None:
        print(f'       message.txt declares All_opcode={total}; we mapped {len(opcodes)} unique opcodes')

    print('[2/4] map_codes.json')
    write_json('map_codes.json', parse_map_codes())

    print('[3/4] items.json')
    write_json('items.json', parse_items())

    print('[4/4] npcs.json (xlsx, slower)')
    write_json('npcs.json', parse_npcs())

    print('Done.')


if __name__ == '__main__':
    main()
