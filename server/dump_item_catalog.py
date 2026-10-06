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

--cash (T-CAT, premium_cash-catalog): writes nothing. It reads the mall columns of every
def instead - +0x1F0 Cash, +0x1F4 Cash_Cls, +0x1F6 Cash_T, +0x1FC Cash_P, the offsets cash.py
documents but only inferred from the hii column order (the S2C 0x72 period gate relies on
def+0x1F6 == hii Cash_T) - and diffs them against the server's own data source: cash.cash_def
over this build's hii, whose sold rows are cash.mall_catalog(). Every mismatch is printed with
the raw 16 bytes +0x1F0..+0x1FF, then a summary; the exit status is 1 on any mismatch.
2008 layout only (scene+0xF84 catalog, def+0x154 type): the 2009 catalog pointer and def
layout are not mapped yet, so `--build 2009 --cash` stops with a message.
"""
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCENE_PTR = 0x70EECC
CASH_BLOCK = 0x1F0                 # def+0x1F0 .. +0x1FF: the 16 bytes --cash reads
# (field, offset in the def, struct format): the widths are the smallest that hold the hii
# values (Cash 0/1, Cash_Cls <= 19, Cash_T 0..2, prices up to 50000); a wider field whose
# upper bytes are not zero shows up as a mismatch with its raw bytes printed.
CASH_FIELDS = (('cash', 0x1F0, '<B'), ('cls', 0x1F4, '<H'), ('duration_type', 0x1F6, '<H'),
               ('price', 0x1FC, '<I'))


def read_cash_columns(block):
    """The CASH_FIELDS values of one def from its 16 bytes at +0x1F0."""
    block = bytes(block or b'').ljust(16, b'\x00')
    return {name: struct.unpack_from(fmt, block, off - CASH_BLOCK)[0] for name, off, fmt in CASH_FIELDS}


def cash_diff(dumped, cash_def):
    """[(id, field, client value, server value)] for every id of `dumped` ({id: columns})
    whose client columns differ from `cash_def(id)` (cash.cash_def; None = an id the server's
    table has not got: every non-zero client column is then a mismatch)."""
    out = []
    for item_id in sorted(dumped):
        client = dumped[item_id]
        d = cash_def(item_id)
        for name, _off, _fmt in CASH_FIELDS:
            server = getattr(d, name, 0) if d is not None else 0
            if int(client.get(name, 0)) != int(server):
                out.append((item_id, name, int(client.get(name, 0)), int(server)))
    return out


def _defs(V):
    """(pid, reader, [(id, def address)]) of the running client's catalog, or three Nones."""
    proc, pid = V._open_inworld()
    if not proc:
        print('no readable client (launch it non-elevated)')
        return None, None, None
    rd = V._reader(proc)
    u32 = lambda a: struct.unpack('<I', rd(a, 4))[0]  # noqa: E731
    scene = u32(SCENE_PTR)
    cat = u32(scene + 0xF84)
    begin, end = u32(cat + 8), u32(cat + 0xC)
    count = (end - begin) // 4
    print(f'PID {pid} scene=0x{scene:08X} catalog=0x{cat:08X} items={count}')
    return pid, rd, [(i + 1, u32(begin + 4 * i)) for i in range(count)]


def dump_cash(V):
    """--cash: the T-CAT check (module docstring). Returns the exit status."""
    if str(getattr(V, 'BUILD', '2008')) != '2008':
        print(f'--cash knows the 2008 def layout only; build {V.BUILD} is not mapped yet')
        return 2
    import cash as CASH
    import config as cfgmod
    import en_content as EC
    cfg = cfgmod.load()
    EC.configure(cfg['CLIENT_DIR'], '2008')
    pid, rd, defs = _defs(V)
    if defs is None:
        return 2
    dumped, raw = {}, {}
    for item_id, d in defs:
        if not d:
            continue
        block = rd(d + CASH_BLOCK, 16) or b''
        cols = read_cash_columns(block)
        if any(cols.values()) or CASH.cash_def(item_id) is not None and CASH.cash_def(item_id).cash:
            dumped[item_id], raw[item_id] = cols, block
    bad = cash_diff(dumped, CASH.cash_def)
    for item_id, name, client, server in bad[:200]:
        print(f'  MISMATCH {item_id} {EC.item_name(item_id)!r} {name}: client {client} vs hii {server}  '
              f'(+0x1F0: {raw[item_id].hex(" ")})')
    sold = CASH.mall_catalog()
    print(f'T-CAT 2008: {len(dumped)} defs with mall columns read, {len(sold)} rows in cash.mall_catalog() '
          f'({CASH.catalog_source()}); {len(bad)} mismatch(es)'
          + (' - the def offsets match the hii columns' if not bad else ''))
    return 1 if bad else 0


def main():
    import wsview as V
    if '--cash' in sys.argv[1:]:
        return dump_cash(V)
    pid, rd, defs = _defs(V)
    if defs is None:
        return 2
    out = {}
    for item_id, d in defs:
        if not d:
            continue
        raw = rd(d, 64) or b''
        name = raw.split(b'\x00', 1)[0].decode('cp949', 'replace')
        typ = struct.unpack('<I', rd(d + 0x154, 4))[0]
        out[item_id] = {'name': name, 'type': typ}
    path = os.path.join(HERE, 'en_item_catalog.json')
    json.dump(out, open(path, 'w', encoding='utf-8'), indent=0, ensure_ascii=False)
    types = {}
    for v in out.values():
        types[v['type']] = types.get(v['type'], 0) + 1
    print(f'wrote {path}: {len(out)} items, by type {dict(sorted(types.items()))}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
