#!/usr/bin/env python3
"""
dump_item_catalog.py - export the EN client's loaded item catalog from memory
==============================================================================
The client resolves item ids through FUN_00404210: catalog = [scene+0xF84],
a std::vector<ItemDef*> (begin +8, end +0xC); id is valid iff 1 <= id <= count.
ItemDef: name str at +0x00, type at +0x154 (0 stack, 1 equipment, 2 misc stack,
3 skill book, 4 class change - per the S2C 0x18 spec).

Writes en_item_catalog.json {id: {"name": ..., "type": ...}}. Client must be
running NON-elevated and past login (scene allocated).
"""
import ctypes
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wsview as V  # noqa: E402

SCENE_PTR = 0x70EECC


def main():
    proc, pid = V._open_inworld()
    if not proc:
        print('no readable client (launch it non-elevated)'); return
    rd = V._reader(proc)
    u32 = lambda a: struct.unpack('<I', rd(a, 4))[0]
    scene = u32(SCENE_PTR)
    cat = u32(scene + 0xF84)
    begin, end = u32(cat + 8), u32(cat + 0xC)
    count = (end - begin) // 4
    print(f'PID {pid} scene=0x{scene:08X} catalog=0x{cat:08X} items={count}')
    out = {}
    for i in range(count):
        d = u32(begin + 4 * i)
        if not d:
            continue
        raw = rd(d, 64) or b''
        name = raw.split(b'\x00', 1)[0].decode('cp949', 'replace')
        typ = struct.unpack('<I', rd(d + 0x154, 4))[0]
        out[i + 1] = {'name': name, 'type': typ}
    path = os.path.join(HERE, 'en_item_catalog.json')
    json.dump(out, open(path, 'w', encoding='utf-8'), indent=0, ensure_ascii=False)
    types = {}
    for v in out.values():
        types[v['type']] = types.get(v['type'], 0) + 1
    print(f'wrote {path}: {len(out)} items, by type {dict(sorted(types.items()))}')


if __name__ == '__main__':
    main()
