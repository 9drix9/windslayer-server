#!/usr/bin/env python3
"""
patch_data_2009.py - client DATA patches of gate G-CP for the EN 2009 client (Outspark v1.04, Build 14)
====================================================================================================
Builds patched COPIES of four hs\\ data files. It never edits a file in place, and the only modes
that write into a game install are the explicit --install / --uninstall (run by the user/lead).

  cp-1  hs\\windslayer.hii  CardNpc (def+0x150) = 182..185 on the 4 pet rows and the 16 pet-gear rows
        (EN ids 4290..4309). Every pet-sprite builder (FUN_00447f40, FUN_00448350, the 0x6F finalize in
        FUN_0046a2e0) returns silently while CardNpc == 0, so stock data can never show a pet
        (docs/systems_2009/pet.md 1 B1, 9 stage 0a). Values = the KR rows (KR id + 4 = EN id
        above 4248; KR 2025 hii and KR gamedef agree), i.e. hni templates 182..185 = hs/pet/pet001..004.
        Side effect: the card-deck total (itemtable+0x24, counts non-zero CardNpc) goes 900 -> 920.
  cp-4  hs\\windslayer.hui  window 0x50 (player right-click menu): Size 137x220 -> 137x240, Bt_No 11 -> 12,
        plus control 12 = the KR 2025 control (Text 8038, sprite 102/106/059, Pos 20 210, Size 90 19,
        Type 18, Button 1, Event 1, Enable 1). Only Text_Pos is the EN window's own 5 4 (KR 10 4).
        FUN_00450130 (0x450436..0x4504BD) walks to the 12th control node to enable it (target in a
        guild, local player not) or grey it, and pushes {guild id, guild name}; the click
        (FUN_00448730 case 0x50 / control 0xC) opens join dialog 0x4B6 in mode 2, whose OK sends
        C2S 0x89 with a u32 guild id (0x482D2F..0x482D64). With 11 controls the walk finds nothing.
        hs\\UILngKo.lng  adds "8038 Apply#to#Guild" (KR 8038 = "Guild join request"; '#' = space).
        The .lng is plain text read with fopen/fscanf("%d %s") (FUN_00405020): no cipher, no footer.
        (docs/CLIENT_PATCH_SET_RE_2026-10-06.md; docs/systems_2009/guild.md 1.5 and 12 Q11.)
  cp-3d hs\\windslayer.hni  the Pet Bell (EN 4285) on the 8 potion grocers' shop rows: in the `item:`
        column (480 digits = 120 x %04d, FUN_00409150 token 0x13 -> template+0x288) the first empty
        slot, i.e. right after each grocer's Build 14 stock: Misty 8, Eve 48, Hikaru 50, Margaret 86,
        Sophia 107, Celine 138, Catherine 152, Evan 172 = the server's config SHOP_EXTRA_ITEMS, which
        appends 4285 after the hni stock (windslayer_server._handle_buy_item: stock + _shop_extras).
        The shop window FUN_0046f6e0 walks template+0x288 up to the FIRST zero id (0x78 at most), so
        the id must sit exactly in that slot; the hni has no price column: client and server both
        price the row from the hii (4285 Buy 500, def+0x1E0; Cash 0, def+0x1F0). KR lists the bell
        (KR 4281 = EN 4285 - 4) as the last row of its grocers 19 and 23 (KR 2025 hni, KR gamedef).
  cp-5  hs\\windslayer.hni  Moiba (NPC 181, guild menu 0x4BB) `item:` slot 0: 4283 -> 4284. Control 5
        "Purchase advertisement" reads only that first id (0x482248 MOVZX [template+0x288]), titles
        its quantity dialog "How many do you want to buy?(%uGold)" with def+0x1E0 (0x4822A2) and
        sends C2S 0x0B {id} (0x474327); FUN_00471450 skips the gold check for a 0 price. 4283 is the
        PREMIUM board (Type 5, Cash 1, Buy 0) -> "(0Gold)"; 4284 = Guild Billboard (Type 0, Cash 0,
        Buy 1000) = KR Moiba's KR 4280 + 4 (KR 2025 hni and KR gamedef), the item the server sells
        for 1,000 (windslayer_server._buy_guild_board). cp-3d and cp-5 share the file; each subset
        has its own pinned output, and --install / --uninstall add / remove patches on top of what
        the file already holds (the build source is always the pristine original or its backup).

Cipher of .hii/.hui/.hni (ValidationCheck.dll): plain[i] = raw[i] + K[i % 3], K = E9 DE E0 (Encode adds
17 22 20). Footer = 20-byte SHA-1 of the CIPHERED body (CValidationCheck::CheckValid: standard SHA-1 IV,
digest of len-20 bytes, 20-byte compare; the loaders call CheckValid before Decode and stop on failure).

Every source file is checked against a pinned pristine SHA-1 (installer WindSlayer-01_04_0000.exe ==
the 2026-09-23 install) before patching, and every output against a pinned patched SHA-1.

usage (every path is an explicit option, never positional; --only cp-1|cp-3d|cp-4|cp-5, repeatable,
default = all four):
  python patch_data_2009.py --src DIR --out DIR [--only cp-1] [--only cp-3d] [--only cp-4] [--only cp-5]
        build the patched copies into DIR\\hs\\ and verify them. --src is a folder holding pristine
        windslayer.hii / windslayer.hui / UILngKo.lng / windslayer.hni (directly or in hs\\); it is
        only read (the cp-3d/cp-5 price check also reads its windslayer.hii).
        Every output is built and verified in memory first; nothing is created or written unless
        all checks pass. Refused: an --out at or below any folder that holds WindSlayer.exe (a game
        install or a pristine extraction), and an --out whose hs\\ is the folder (or file) a source
        is read from - paths are compared case-insensitively after resolving links (NTFS).
  python patch_data_2009.py --verify DIR --src DIR [--kr DIR] [--only ...]
        re-verify built copies in DIR(\\hs) against the pristine sources (footer, decode, loader
        grammar, structured diff; for the hni the FUN_00409150 parse and the shop walks); --only
        names the patches the copies were built with. --kr (read-only) also compares control 12
        with the KR 2025 hui and the hni rows with the KR 2025 hni.
  python patch_data_2009.py --status --game DIR            read-only state of an install
  python patch_data_2009.py --install --game DIR [--only ...] [--settle SEC]
        writes into the game folder: every file is checked (pinned SHA-1) and its patched copy built and
        checked BEFORE the first write; then each original is copied to <file>.orig-pre-gcp
        (refused if that backup already exists and differs from the pristine original) and replaced
        through <file>.gcp-tmp + os.replace. A file already holding the requested patches is left
        alone (re-running is a no-op); a hni holding one of cp-3d/cp-5 is rebuilt from its backup
        with both. After a failed write (a locked or read-only file: the error names it) fix the
        cause and rerun --install, or run --uninstall. A *.gcp-tmp leftover blocks --install.
  python patch_data_2009.py --uninstall --game DIR [--only ...] [--settle SEC]
        removes the selected patches: a file left with none is restored from its .orig-pre-gcp
        backup (read once, checked against the pinned pristine SHA-1, and exactly those bytes
        written), verified, and then the backup is deleted; a hni left with one of cp-3d/cp-5 is
        rebuilt from that backup and keeps it. Refuses if a file holds unknown content. Removes this
        script's own *.gcp-tmp leftovers (os.replace is the commit point, so a leftover is never the
        only copy of anything).
  Both re-read every file after --settle seconds (default 10) and list stray '* Name clash *' /
  *.gcp-tmp files: a folder-sync client on the Desktop once reverted a renamed-over file.
  Pause the Desktop sync client (or exclude the game folder from it) before --install.
"""
import argparse
import difflib
import hashlib
import os
import re
import sys
import time

K = (0xE9, 0xDE, 0xE0)
BACKUP_SUFFIX = '.orig-pre-gcp'
TMP_SUFFIX = '.gcp-tmp'

# name -> (sha1, size) of the Build 14 originals (installer data1/data2.cab == the install)
PRISTINE = {
    'windslayer.hii': ('82615c9f5cb210e6ff3b1c565f72d982d76262be', 2199660),
    'windslayer.hui': ('47fa6b7a9af91666e279383bfa297fd9ee288c9d', 1800149),
    'UILngKo.lng': ('070195b722159444d9d40a74145ffb6bccb414c5', 252759),
    'windslayer.hni': ('fd9ea75fd25af0b354961f72544da2f6622f8b9f', 349083),
}
CIPHERED = ('windslayer.hii', 'windslayer.hui', 'windslayer.hni')
PATCHES = {'cp-1': ('windslayer.hii',), 'cp-3d': ('windslayer.hni',), 'cp-4': ('UILngKo.lng', 'windslayer.hui'),
           'cp-5': ('windslayer.hni',)}
# name -> {frozenset(patches applied): (sha1, size)} of this script's outputs (deterministic). A file
# two patches share (the hni: cp-3d and cp-5) has one pinned output per non-empty subset.
_V = frozenset
PATCHED = {
    'windslayer.hii': {_V({'cp-1'}): ('e759b301ebabb2eb0d005856897062c894abb7a0', 2199700)},
    'windslayer.hui': {_V({'cp-4'}): ('634e97c029924906d19f65c15a888de895762004', 1800412)},
    'UILngKo.lng': {_V({'cp-4'}): ('c6cea897f7ec0bac414c861cedd5eb1457267be0', 252780)},
    'windslayer.hni': {_V({'cp-3d'}): ('b2e775ee9fa0ed813696db41533abd4185d658e0', 349083),
                       _V({'cp-5'}): ('024ee5e601bb82263008b774d168ecf161219634', 349083),
                       _V({'cp-3d', 'cp-5'}): ('b49219f403ac77ca7ac5679660cfa11b11b2e3f2', 349083)},
}
# install order: the text before the control that shows it; the hni (independent) last
FILE_ORDER = ('windslayer.hii', 'UILngKo.lng', 'windslayer.hui', 'windslayer.hni')


def file_patches(name):
    """Every patch that edits file `name`."""
    return _V(p for p, names in PATCHES.items() if name in names)


def vname(variant):
    """'cp-3d+cp-5' for a variant set ('-' for none)."""
    return '+'.join(sorted(variant)) or '-'


# ---------------------------------------------------------------------------------------- cp-1
# EN id: (CardNpc, Type, Kind, Spr_Num). Species s = CardNpc - 181; pet Spr_Num = s, gear Spr_Num // 100 = s.
CP1_ROWS = {
    4290: (182, 1, 16, 101), 4291: (182, 1, 16, 102), 4292: (182, 1, 15, 101), 4293: (182, 1, 15, 102),
    4294: (182, 6, 14, 1),      # Picky
    4295: (183, 1, 16, 201), 4296: (183, 1, 16, 202), 4297: (183, 1, 15, 202), 4298: (183, 1, 15, 201),
    4299: (183, 6, 14, 2),      # Ulie
    4300: (184, 1, 16, 302), 4301: (184, 1, 16, 301), 4302: (184, 1, 15, 301), 4303: (184, 1, 15, 302),
    4304: (184, 6, 14, 3),      # ChikaPuka
    4305: (185, 1, 15, 401), 4306: (185, 1, 15, 402), 4307: (185, 1, 16, 402), 4308: (185, 1, 16, 401),
    4309: (185, 6, 14, 4),      # GuriGuri
}
HII_HEADER = b'Number_of_ITEM: 4322'
CARD_TOTAL_BEFORE = 900

# ---------------------------------------------------------------------------------------- cp-4
W80_OLD = (b'80 883 Sprite: 142000000143000000144000000 Pos: 0 0 Size: 137 220 Type: 0 Stretch: 1 '
           b'Check_UI: 1 Not_Close: 0 First: 1 Bt_No: 11 Data: 0')
W80_NEW = (b'80 883 Sprite: 142000000143000000144000000 Pos: 0 0 Size: 137 240 Type: 0 Stretch: 1 '
           b'Check_UI: 1 Not_Close: 0 First: 1 Bt_No: 12 Data: 0')
_CTL_TAIL = (b'Flag: 0 Color: -1 Type_Color: -1 Stretch: 0 Button: 1 Title: 0 Event: 1 Link: 0 Return: 0 '
             b'Up: 0 Down: 0 Left: 0 Right: 0 Data: 0 Cmt: # Enable: 1 Multi: 0 LSize: 0 ')
W80_C11 = (b'11 Text: 894 Sprite: 102106059000000000000000000 Pos: 20 190 Size: 90 19 Type: 18 '
           b'Text_Pos: 5 4 ' + _CTL_TAIL)
W80_C12 = (b'12 Text: 8038 Sprite: 102106059000000000000000000 Pos: 20 210 Size: 90 19 Type: 18 '
           b'Text_Pos: 5 4 ' + _CTL_TAIL)
W80_C12_KR = (b'12 Text: 8038 Sprite: 102106059000000000000000000 Pos: 20 210 Size: 90 19 Type: 18 '
              b'Text_Pos: 10 4 ' + _CTL_TAIL)
W81_HEAD = b'81 895 Sprite: '
HUI_HEADER = b'Number_of_UI: 1245'
LNG_ID, LNG_TEXT = 8038, b'Apply#to#Guild'
LNG_PREV, LNG_NEXT = b'8034 ', b'8039 To#Buddy'

# ------------------------------------------------------------------------------------ cp-3d, cp-5
HNI_HEADER = b'Number_of_NPC: 204'
HNI_ITEM_SLOTS, HNI_ITEM_DIGITS = 120, 4      # `item:` = 0x1E0 chars, sscanf "%04d" x 0x78 (FUN_00409150)
HNI_DROP_SLOTS, HNI_DROP_DIGITS = 120, 6      # `Drop:` = 0x78 x "%06d"
UI_SHOP, UI_GUILD_NPC = 13, 0x4BB             # hni `UI:` 13 = shop window 0xD; 1211 = Moiba's menu
PET_BELL, BELL_BUY = 4285, 500
# hni idx: (title text id, NPCLngKo name, Build 14 stock). The bell takes slot len(stock), the first
# zero: the same place the server's `stock + _shop_extras` list gives it (config SHOP_EXTRA_ITEMS).
CP3D_GROCERS = {
    8: (353, 'Misty', (3, 5, 6, 7, 51, 17, 614, 1270, 1340)),
    48: (383, 'Eve', (3, 5, 6, 7, 51, 141, 1065, 1270, 1271, 1340, 1341, 614, 22)),
    50: (385, 'Hikaru', (3, 5, 141, 142, 1065, 1064, 614, 35, 1269, 1271, 1341, 1342)),
    86: (407, 'Margaret', (3, 5, 614, 1305, 141, 142, 51, 1065, 1064, 1271, 1269, 1341, 1342)),
    107: (423, 'Sophia', (3, 5, 614, 104, 141, 142, 3438, 1065, 1064, 3440, 1271, 1269, 1341, 1342)),
    138: (430, 'Celine', (3, 5, 614, 3105, 142, 3438, 3439, 1064, 3440, 3441, 1271, 1269, 1341, 1342)),
    152: (436, 'Catherine', (3, 5, 614, 142, 3438, 3439, 1064, 3440, 3441, 3236, 1271, 1269, 1341, 1342)),
    172: (444, 'Evan', (3, 5, 614, 142, 3438, 3439, 1064, 3440, 3441, 3950, 1271, 1269, 1341, 1342)),
}
CP5_NPC, CP5_TITLE = 181, 293                 # Moiba
CP5_OLD, CP5_NEW, BOARD_BUY = 4283, 4284, 1000
# hii rows the two patches name (identical in the pristine and the cp-1 hii): id -> (Type, Buy, Cash)
HII_PRICE_ROWS = {PET_BELL: (0, BELL_BUY, 0), CP5_NEW: (0, BOARD_BUY, 0), CP5_OLD: (5, 0, 1)}
# KR 2025 hni cross-check (--kr): KR ids = EN - 4 above 4248 (arch09-id-shift)
KR_SHIFT, KR_BELL_GROCERS = 4, (19, 23)
HNI_ITEM_RE = re.compile(rb' item: (\d{%d}) Drop: (\d{%d}) ' % (HNI_ITEM_SLOTS * HNI_ITEM_DIGITS,
                                                                 HNI_DROP_SLOTS * HNI_DROP_DIGITS))

HUI_HDR_RE = re.compile(rb'^(\d+) (\d+) Sprite: (\d{27}) Pos: (-?\d+) (-?\d+) Size: (-?\d+) (-?\d+) '
                        rb'Type: (-?\d+) Stretch: (-?\d+) Check_UI: (-?\d+) Not_Close: (-?\d+) '
                        rb'First: (-?\d+) Bt_No: (\d+) Data: (-?\d+)$')
HUI_CTL_RE = re.compile(rb'^(\d+) Text: (-?\d+) Sprite: (\d{27}) Pos: (-?\d+) (-?\d+) Size: (-?\d+) '
                        rb'(-?\d+) Type: (-?\d+)(?: Mul_Txt: (-?\d+))? Text_Pos: (-?\d+) (-?\d+) '
                        rb'Flag: (-?\d+) Color: (-?\d+) Type_Color: (-?\d+) Stretch: (-?\d+) '
                        rb'Button: (-?\d+) Title: (-?\d+) Event: (-?\d+) Link: (-?\d+) Return: (-?\d+) '
                        rb'Up: (-?\d+) Down: (-?\d+) Left: (-?\d+) Right: (-?\d+) Data: (-?\d+) '
                        rb'Cmt: (\S+) Enable: (-?\d+) Multi: (-?\d+) LSize: (-?\d+) $')


class PatchError(Exception):
    pass


class WriteError(PatchError):
    """An OS error while writing a file (locked, read-only, disk full). Its .gcp-tmp is removed."""
    pass


def need(cond, msg):
    if not cond:
        raise PatchError(msg)


def sha1(b):
    return hashlib.sha1(b).hexdigest()


# ------------------------------------------------------------------- cipher + footer (ValidationCheck)
def decode(body):
    return bytes((b + K[i % 3]) & 0xFF for i, b in enumerate(body))


def encode(plain):
    return bytes((b - K[i % 3]) & 0xFF for i, b in enumerate(plain))


def check_valid(raw):
    """CValidationCheck::CheckValid(buf, len - 20, footer): SHA-1 of the ciphered body == footer."""
    return len(raw) > 20 and hashlib.sha1(raw[:-20]).digest() == raw[-20:]


def unseal(name, raw):
    if name not in CIPHERED:
        return raw
    need(check_valid(raw), '%s: footer check failed' % name)
    return decode(raw[:-20])


def seal(name, plain):
    if name not in CIPHERED:
        return plain
    body = encode(plain)
    return body + hashlib.sha1(body).digest()


# ------------------------------------------------------------------------------------- patches
def hii_field(line, key):
    m = re.search(rb' ' + key + rb': (-?\d+) ', line)
    return int(m.group(1)) if m else None


def patch_cp1(plain):
    lines = plain.split(b'\r\n')
    need(lines[0] == HII_HEADER, 'hii: unexpected header %r' % lines[0][:40])
    for iid, (npc, typ, kind, spr) in sorted(CP1_ROWS.items()):
        line = lines[iid]
        need(line.startswith(b'%d Title: ' % iid), 'hii: line %d is not item %d' % (iid, iid))
        got = tuple(hii_field(line, k) for k in (b'CardNpc', b'Type', b'Kind', b'Spr_Num'))
        need(got == (0, typ, kind, spr), 'hii: item %d has %r, expected (0, %d, %d, %d)'
             % (iid, got, typ, kind, spr))
        species = npc - 181
        need((spr if typ == 6 else spr // 100) == species, 'hii: item %d species mismatch' % iid)
        new, n = re.subn(rb' CardNpc: 0 ', b' CardNpc: %d ' % npc, line)
        need(n == 1, 'hii: item %d CardNpc token count %d' % (iid, n))
        lines[iid] = new
    return b'\r\n'.join(lines)


def patch_cp4_hui(plain):
    lines = plain.split(b'\r\n')
    need(lines[0] == HUI_HEADER, 'hui: unexpected header %r' % lines[0][:40])
    idx = [i for i, l in enumerate(lines) if l == W80_OLD]
    need(len(idx) == 1, 'hui: window 80 header found %d times (already patched?)' % len(idx))
    i = idx[0]
    need(lines[i + 11] == W80_C11, 'hui: window 80 control 11 differs from Build 14')
    need(lines[i + 12].startswith(W81_HEAD), 'hui: window 80 is not followed by window 81')
    lines[i] = W80_NEW
    lines.insert(i + 12, W80_C12)
    return b'\r\n'.join(lines)


def patch_cp4_lng(plain):
    lines = plain.split(b'\r\n')
    need(not any(l.startswith(b'%d ' % LNG_ID) for l in lines), 'lng: id %d already present' % LNG_ID)
    j = [k for k, l in enumerate(lines) if l == LNG_NEXT]
    need(len(j) == 1 and lines[j[0] - 1].startswith(LNG_PREV), 'lng: 8034/8039 neighbours not found')
    lines.insert(j[0], b'%d %s' % (LNG_ID, LNG_TEXT))
    return b'\r\n'.join(lines)


def hni_line(lines, idx, title):
    """The hni record line of template idx (the EN file numbers its 204 records 1..204 in file
    order, so the line index is the idx and the client list position, FUN_00409dd0 tail-append)."""
    line = lines[idx]
    need(line.startswith(b'%d %d ./hs/' % (idx, title)), 'hni: line %d is not template %d (title %d): %r'
         % (idx, idx, title, line[:30]))
    return line


def hni_item_column(line):
    """(start offset in the line, [120 ids]) of the `item:` token."""
    m = HNI_ITEM_RE.search(line)
    need(m is not None, 'hni: no 480-digit item: / 720-digit Drop: pair in %r' % line[:30])
    tok = m.group(1)
    return m.start(1), [int(tok[k:k + HNI_ITEM_DIGITS]) for k in range(0, len(tok), HNI_ITEM_DIGITS)]


def set_item_slot(line, start, slot, value):
    pos = start + slot * HNI_ITEM_DIGITS
    return line[:pos] + b'%04d' % value + line[pos + HNI_ITEM_DIGITS:]


def patch_cp3d(plain):
    lines = plain.split(b'\r\n')
    need(lines[0] == HNI_HEADER, 'hni: unexpected header %r' % lines[0][:40])
    for idx, (title, _name, stock) in sorted(CP3D_GROCERS.items()):
        line = hni_line(lines, idx, title)
        need(hni_field(line, b'UI') == UI_SHOP, 'hni: template %d is not a merchant (UI %r)'
             % (idx, hni_field(line, b'UI')))
        start, ids = hni_item_column(line)
        n = len(stock)
        need(tuple(ids[:n]) == stock and not any(ids[n:]), 'hni: template %d stock %r is not the Build 14 %r'
             % (idx, [i for i in ids if i], stock))
        lines[idx] = set_item_slot(line, start, n, PET_BELL)
    return b'\r\n'.join(lines)


def patch_cp5(plain):
    lines = plain.split(b'\r\n')
    need(lines[0] == HNI_HEADER, 'hni: unexpected header %r' % lines[0][:40])
    line = hni_line(lines, CP5_NPC, CP5_TITLE)
    need(hni_field(line, b'UI') == UI_GUILD_NPC, 'hni: template %d UI %r is not 0x4BB'
         % (CP5_NPC, hni_field(line, b'UI')))
    start, ids = hni_item_column(line)
    need(ids[0] == CP5_OLD and not any(ids[1:]), 'hni: Moiba item: %r is not [4283]' % [i for i in ids if i])
    lines[CP5_NPC] = set_item_slot(line, start, 0, CP5_NEW)
    return b'\r\n'.join(lines)


def hni_field(line, key):
    m = re.search(rb' ' + key + rb': (-?\d+)(?: |$)', line)
    return int(m.group(1)) if m else None


# (patch, file) -> function plain -> plain. A file's patches are applied in sorted patch order.
PATCH_FN = {('cp-1', 'windslayer.hii'): patch_cp1, ('cp-4', 'windslayer.hui'): patch_cp4_hui,
            ('cp-4', 'UILngKo.lng'): patch_cp4_lng, ('cp-3d', 'windslayer.hni'): patch_cp3d,
            ('cp-5', 'windslayer.hni'): patch_cp5}


def check_pristine(name, raw):
    need(sha1(raw) == PRISTINE[name][0], '%s: SHA-1 %s is not the pinned pristine %s'
         % (name, sha1(raw), PRISTINE[name][0]))


def build_one(name, raw, variant):
    """pristine raw bytes -> raw bytes (sealed) with the patches in `variant` applied."""
    check_pristine(name, raw)
    need(variant and variant <= file_patches(name), '%s: no patch set %s' % (name, vname(variant)))
    plain = unseal(name, raw)
    for p in sorted(variant):
        plain = PATCH_FN[(p, name)](plain)
    return seal(name, plain)


def pinned(name, variant):
    """The pinned (sha1, size) of `name` built with `variant`."""
    need(variant in PATCHED[name], '%s: no pinned output for %s' % (name, vname(variant)))
    return PATCHED[name][variant]


# ---------------------------------------------------------------------------- verification
def lng_records(plain):
    """FUN_00405020: fscanf(f, "%d %s") until EOF; '#' -> ' '. Returns list of (id, text)."""
    toks = plain.split()
    need(len(toks) % 2 == 0, 'lng: odd token count')
    out = []
    for k in range(0, len(toks), 2):
        need(re.fullmatch(rb'-?\d+', toks[k]) is not None, 'lng: bad id token %r' % toks[k][:20])
        need(len(toks[k + 1]) < 1024, 'lng: text > 1023 chars')
        out.append((int(toks[k]), toks[k + 1].replace(b'#', b' ')))
    return out


def hui_windows(plain):
    """Walk the hui as FUN_00495ed0 does: a header line, then exactly Bt_No control lines."""
    lines = plain.split(b'\r\n')
    m = re.fullmatch(rb'Number_of_UI: (\d+)', lines[0])
    need(m is not None, 'hui: bad first line')
    need(lines[-1] == b'', 'hui: file must end with CRLF')
    wins, i = {}, 1
    while i < len(lines) - 1:
        h = HUI_HDR_RE.match(lines[i])
        need(h is not None, 'hui: line %d is not a window header' % (i + 1))
        n = int(h.group(13))
        ctls = []
        for j in range(1, n + 1):
            c = HUI_CTL_RE.match(lines[i + j]) if i + j < len(lines) - 1 else None
            need(c is not None and int(c.group(1)) == j, 'hui: window %s control %d bad (line %d)'
                 % (h.group(1).decode(), j, i + j + 1))
            ctls.append(c)
        wid = int(h.group(1))
        need(wid not in wins, 'hui: duplicate window %d' % wid)
        wins[wid] = (h, ctls)
        i += n + 1
    need(len(wins) == int(m.group(1)), 'hui: %d windows, header says %s' % (len(wins), m.group(1).decode()))
    return wins


def hii_items(plain):
    lines = plain.split(b'\r\n')
    m = re.fullmatch(rb'Number_of_ITEM: (\d+)', lines[0])
    need(m is not None and lines[-1] == b'', 'hii: bad header or missing final CRLF')
    rows = lines[1:-1]
    need(len(rows) == int(m.group(1)), 'hii: row count %d != %s' % (len(rows), m.group(1).decode()))
    for n, l in enumerate(rows, 1):
        need(l.startswith(b'%d Title: ' % n), 'hii: row %d out of order' % n)
    return rows


# FUN_00409150 (the hni loader): token index on a record line -> (template offset, conversion). The
# offsets are the decomp's stack slots relative to the record base local_12d0 (memset 0xAA4 per line,
# copied whole by FUN_00409dd0); index 0 (the idx) is atol'd and dropped, 4/0x12/0x14/... are keys.
HNI_CASES = {1: (0x000, 'text'), 2: (0x052, 'str'), 3: (0x0D2, 'str'),
             0x13: (0x288, ('scan', HNI_ITEM_DIGITS, HNI_ITEM_SLOTS)),
             0x15: (0x468, ('scan', HNI_DROP_DIGITS, HNI_DROP_SLOTS)),
             0x17: (0x648, 'atol'), 0x19: (0x64C, 'atol'), 0x1B: (0x684, 'atol'), 0x1D: (0x650, 'atol'),
             0x1F: (0x654, 'atol'), 0x21: (0x658, 'atol'), 0x23: (0x65C, 'atol'), 0x25: (0x660, 'atol'),
             0x27: (0x66C, 'atol'), 0x29: (0x664, 'atol'), 0x2B: (0x668, 'atol'), 0x2D: (0x670, 'atol'),
             0x2F: (0x674, 'speed'), 0x31: (0x678, 'atol'), 0x33: (0x67C, 'atol'), 0x35: (0x680, 'atol'),
             0x37: (0x158, ('scan', 3, 0x15)), 0x38: (0x15C, ('scan', 3, 0x15)), 0x39: (0x160, ('scan', 3, 0x15)),
             0x3B: (0x68C, 'atol'), 0x3D: (0x690, 'atol'), 0x3F: (0x6AC, 'text'), 0x41: ('Cmt', 'text'),
             0x43: (0x011, 'text')}
HNI_CASES.update({k: (0x254 + 4 * (k - 5), 'atol') for k in range(5, 0x12)})     # AI: 13 dwords
TOKEN_BUF = 0x400                    # local_810: no bound check in the loader, so a token must stay below it


def _c_atol(buf, o):
    m = re.match(rb'[ \t\n\v\f\r]*([+-]?\d+)', bytes(buf[o:buf.index(0, o)]))
    return int(m.group(1)) if m else 0


def _c_scan(buf, o, width):
    """sscanf(buf + o, "%0<width>d"): the int, or None when nothing converts (the target keeps its
    memset 0). Reads leftover bytes past the token's NUL exactly as the client would."""
    if buf[o] == 0:
        return None
    s = re.match(rb'[ \t\n\v\f\r]*', bytes(buf[o:o + 64])).end()
    m = re.match(rb'[+-]?\d*', bytes(buf[o + s:buf.index(0, o + s)]))
    tok = m.group(0)[:width]
    return int(tok) if tok.lstrip(b'+-') else None


def hni_client_parse(plain):
    """Emulate FUN_00409150 on a decoded hni: the reused 0x400-byte token buffer (leftovers
    included), ' ' / '\\r' as separators, '\\n' skipped, line 1 only arms the parser, and every
    later '\\r' commits the record (FUN_00409dd0: needs the title and the .hsi path). Returns
    (declared count, [{field: value}]) in list order; field = template offset or a name."""
    buf = bytearray(TOKEN_BUF)
    n = k = declared = 0
    rec, out = {}, []
    for c in plain:
        if c in (0x20, 0x0D):
            buf[n] = 0
            if declared:
                case = HNI_CASES.get(k)
                if case is not None:
                    off, conv = case
                    if conv == 'atol':
                        rec[off] = _c_atol(buf, 0)
                    elif conv == 'speed':
                        v = _c_atol(buf, 0)
                        rec[off] = 0.0 if v == 0 else v * 0.0001
                    elif conv in ('str', 'text'):
                        rec[off] = bytes(buf[:n])           # 'text': an lng id (or '#'), resolved by FUN_004051f0
                    else:
                        _scan, width, count = conv
                        vals = []
                        for j in range(count):
                            v = _c_scan(buf, j * width, width)
                            vals.append(0 if v is None else v)
                        rec[off] = vals
                k += 1
            if c == 0x0D:
                if not declared:
                    declared = _c_atol(buf, 0)
                else:
                    need(rec.get(0x000) and rec.get(0x052), 'hni: record %d has no title or .hsi (the loader '
                         'would abort)' % (len(out) + 1))
                    rec['position'] = len(out) + 1                 # FUN_00409dd0: +0x154 = list count
                    out.append(rec)
                k, rec = 0, {}
            n = 0
        elif c != 0x0A:
            need(n < TOKEN_BUF - 1, 'hni: token longer than the loader buffer')
            buf[n] = c
            n += 1
    return declared, out


def hni_shop_rows(rec):
    """FUN_0046f6e0: the ids the shop window 0xD lists for a template, from +0x288 up to the first 0."""
    rows = []
    for v in rec[0x288][:HNI_ITEM_SLOTS]:
        if v == 0:
            break
        rows.append(v)
    return rows


def hii_rows(hii_plain, ids):
    """{id: (Type, Buy = def+0x1E0, Cash = def+0x1F0)} from a decoded hii (the line index is the id)."""
    lines = hii_plain.split(b'\r\n')
    out = {}
    for i in ids:
        need(0 < i < len(lines) - 1 and lines[i].startswith(b'%d Title: ' % i), 'hii: no item %d' % i)
        out[i] = tuple(hii_field(lines[i], k) for k in (b'Type', b'Buy', b'Cash'))
    return out


def hni_expected(variant):
    """[(idx, slot, old id, new id)] of the intended hni edits for a variant."""
    exp = []
    if 'cp-3d' in variant:
        exp += [(idx, len(stock), 0, PET_BELL) for idx, (_t, _n, stock) in sorted(CP3D_GROCERS.items())]
    if 'cp-5' in variant:
        exp.append((CP5_NPC, 0, CP5_OLD, CP5_NEW))
    return sorted(exp)


def verify_hni(a, b, orig_raw, new_raw, variant, diff, hii_plain=None, kr_hni_plain=None, report=print):
    """The hni checks of verify_pair: diff shape, raw bytes, the client's own parse and walks."""
    exp = hni_expected(variant)
    need(len(diff) == len(exp) and all(d[0] == 'replace' for d in diff), 'hni: diff shape %r'
         % [(d[0], d[1]) for d in diff])
    spans = []
    for d, (idx, slot, old, new) in zip(diff, exp):
        td = token_delta(d[3], d[4])
        need(d[1] == idx + 1 and td is not None and len(td) == 1 and td[0][0] == 0x13 and td[0][1] == b'item:',
             'hni: line %d change %r is not template %d item:' % (d[1], td and [t[:2] for t in td], idx))
        start = HNI_ITEM_RE.search(d[3]).start(1)
        pos = slot * HNI_ITEM_DIGITS
        o, n_ = td[0][2], td[0][3]
        need(o[:pos] == n_[:pos] and o[pos + 4:] == n_[pos + 4:] and o[pos:pos + 4] == b'%04d' % old
             and n_[pos:pos + 4] == b'%04d' % new, 'hni: template %d item: change is not slot %d %d -> %d'
             % (idx, slot, old, new))
        spans.append((d[5] + start + pos, idx, slot, old, new, d[1], d[5]))
    # raw (ciphered) bytes: the same length, so exactly the edited digits differ, plus the footer
    need(len(orig_raw) == len(new_raw), 'hni: size changed')
    rawdiff = [i for i in range(len(orig_raw) - 20) if orig_raw[i] != new_raw[i]]
    allowed = {p + j for p, *_ in spans for j in range(HNI_ITEM_DIGITS)}
    need(set(rawdiff) <= allowed and rawdiff, 'hni: raw bytes differ outside the edited digits: %r'
         % sorted(set(rawdiff) - allowed)[:8])
    report('   size %d B unchanged; raw body bytes changed: %d (all inside the edited digits) + the footer'
           % (len(new_raw), len(rawdiff)))
    # the client's own parse (FUN_00409150) of both files
    da, ra = hni_client_parse(a)
    db, rb_ = hni_client_parse(b)
    need(da == db == len(ra) == len(rb_) == 204, 'hni: Number_of_NPC %d/%d, records %d/%d'
         % (da, db, len(ra), len(rb_)))
    lb = b.split(b'\r\n')
    need(all(r['position'] == p and lb[p].startswith(b'%d ' % p) for p, r in enumerate(rb_, 1)),
         'hni: a template idx is not its list position')
    want ={(idx, slot): (old, new) for idx, slot, old, new in exp}
    changed = []
    for p, (x, y) in enumerate(zip(ra, rb_), 1):
        need(set(x) == set(y), 'hni: record %d field set changed' % p)
        for f in x:
            if x[f] == y[f]:
                continue
            need(f == 0x288, 'hni: record %d field %r changed' % (p, f))
            for s, (u, v) in enumerate(zip(x[f], y[f])):
                if u != v:
                    changed.append((p, s, u, v))
    need(changed == [(i, s, o, n) for (i, s), (o, n) in sorted(want.items())],
         'hni: client-parsed changes %r != intended %r' % (changed, sorted(want.items())))
    report('   FUN_00409150 parse: %d records = Number_of_NPC, position = idx; only template+0x288 slots '
           'changed: %s' % (len(rb_), ', '.join('%d[%d] %d->%d' % c for c in changed)))
    for pos, idx, slot, old, new, line_no, line_off in spans:
        name = CP3D_GROCERS[idx][1] if idx in CP3D_GROCERS else 'Moiba'
        report('   line %3d (template %3d %-9s line at plain 0x%05X): item: slot %3d (+0x288+%3d) %04d -> %04d'
               ' at plain/raw offset 0x%05X' % (line_no, idx, name, line_off, slot, 4 * slot, old, new, pos))
    rows = {}
    for idx, (title, name, stock) in sorted(CP3D_GROCERS.items()):
        x, y = hni_shop_rows(ra[idx - 1]), hni_shop_rows(rb_[idx - 1])
        need(x == list(stock), 'hni: %s stock' % name)
        need(y == list(stock) + ([PET_BELL] if 'cp-3d' in variant else []), 'hni: %s shop walk %r' % (name, y))
        need(rb_[idx - 1][0x64C] == UI_SHOP, 'hni: %s UI' % name)
        rows[idx] = y
    if 'cp-3d' in variant:
        report('   FUN_0046f6e0 shop walk (to the first 0): the 8 grocers list their Build 14 stock + 4285 as '
               'the last row (rows %s)' % ', '.join('%d:%d' % (i, len(r)) for i, r in sorted(rows.items())))
    moiba = rb_[CP5_NPC - 1]
    first = moiba[0x288][0] & 0xFFFF
    need(moiba[0x64C] == UI_GUILD_NPC and first == (CP5_NEW if 'cp-5' in variant else CP5_OLD),
         'hni: Moiba first id %d' % first)
    report('   0x482248 Moiba control 5 (MOVZX [template+0x288]): item %d' % first)
    if hii_plain is not None:
        ids = sorted({i for r in rows.values() for i in r} | {first})
        hr = hii_rows(hii_plain, ids)
        for i, want_row in HII_PRICE_ROWS.items():
            if i in hr:
                need(hr[i] == want_row, 'hii: item %d (Type, Buy, Cash) %r != %r' % (i, hr[i], want_row))
        bad = [i for i in ids if hr[i][2] != 0 and i != first]
        need(not bad, 'hni: a grocer lists a cash item: %r' % bad)
        if 'cp-3d' in variant:
            report('   hii: 4285 Pet Bell Type 0, Buy (def+0x1E0) %d, Cash (def+0x1F0) 0; every id the 8 grocers '
                   'show is a hii row with Cash 0' % hr[PET_BELL][1])
        report('   hii: Moiba\'s %d -> Type %d, Buy %d, Cash %d: the quantity dialog reads "How many do you want to '
               'buy?(%dGold)"' % (first, hr[first][0], hr[first][1], hr[first][2], hr[first][1]))
        if 'cp-5' in variant:
            need(hr[first] == (0, BOARD_BUY, 0), 'hii: 4284 row')
    if kr_hni_plain is not None:
        # The KR 2025 hni has a second `Drop:` column (its own, newer loader), so it is read by key
        # here, not with the EN FUN_00409150 token positions.
        lk = kr_hni_plain.split(b'\r\n')

        def kr_row(idx):
            tok = lk[idx].split(b' ')
            need(tok[0] == b'%d' % idx and b'item:' in tok and b'UI:' in tok, 'KR hni: line %d is not template %d'
                 % (idx, idx))
            col = tok[tok.index(b'item:') + 1]
            ids = [int(col[k:k + 4]) for k in range(0, len(col), 4)]
            return int(tok[tok.index(b'UI:') + 1]), ids[:ids.index(0)] if 0 in ids else ids

        ui_m, kr_moiba = kr_row(CP5_NPC)
        need(ui_m == UI_GUILD_NPC and kr_moiba[:1] == [CP5_NEW - KR_SHIFT],
             'KR hni: Moiba (UI %d) sells %r, not KR %d' % (ui_m, kr_moiba, CP5_NEW - KR_SHIFT))
        kr_rows = dict((i, kr_row(i)) for i in KR_BELL_GROCERS)
        need(all(u == UI_SHOP and r and r[-1] + KR_SHIFT == PET_BELL for u, r in kr_rows.values()),
             'KR hni: grocers %r do not end with the bell %d' % (KR_BELL_GROCERS, PET_BELL - KR_SHIFT))
        report('   KR 2025 cross-check: KR Moiba sells KR %d (= EN %d); KR grocers %s list the bell KR %d (= EN %d) '
               'as their last row' % (kr_moiba[0], kr_moiba[0] + KR_SHIFT,
                                      '/'.join(map(str, KR_BELL_GROCERS)), PET_BELL - KR_SHIFT, PET_BELL))


def structured_diff(a, b):
    """Line diff a -> b. Returns [(op, a_line_no, b_line_no, a_line, b_line, plain_offset_a)]."""
    al, bl = a.split(b'\r\n'), b.split(b'\r\n')
    offs, o = [], 0
    for l in al:
        offs.append(o)
        o += len(l) + 2
    out = []
    sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        if tag == 'replace' and i2 - i1 == j2 - j1:
            for k in range(i2 - i1):
                out.append(('replace', i1 + k + 1, j1 + k + 1, al[i1 + k], bl[j1 + k], offs[i1 + k]))
        else:
            for k in range(i1, i2):
                out.append(('delete', k + 1, None, al[k], None, offs[k]))
            for k in range(j1, j2):
                out.append(('insert', i1 + 1, k + 1, None, bl[k], offs[i1] if i1 < len(offs) else o))
    return out


def token_delta(x, y):
    tx, ty = x.split(b' '), y.split(b' ')
    if len(tx) != len(ty):
        return None
    return [(k, tx[k - 1] if k else b'', tx[k], ty[k]) for k in range(len(tx)) if tx[k] != ty[k]]


def expected_changes(name):
    """The intended change set, as (op, line_in_original, payload)."""
    if name == 'windslayer.hii':
        return [('replace', iid + 1, (b'CardNpc:', b'0', b'%d' % v[0])) for iid, v in sorted(CP1_ROWS.items())]
    if name == 'windslayer.hui':
        return [('replace', None, ((b'Size:', b'220', b'240'), (b'Bt_No:', b'11', b'12'))),
                ('insert', None, W80_C12)]
    if name == 'windslayer.hni':
        return []                                   # per variant: hni_expected()
    return [('insert', None, b'%d %s' % (LNG_ID, LNG_TEXT))]


def verify_pair(name, orig_raw, new_raw, variant, kr=None, hii_plain=None, report=print):
    """Full check of one patched file (built with the patch set `variant`) against its pristine
    original. kr = {file name: KR 2025 plain text} for the cross-checks; hii_plain = a decoded EN hii
    (pristine or cp-1) for the hni price checks. Raises PatchError."""
    kr = kr or {}
    report('== %s (%s)' % (name, vname(variant)))
    need(sha1(orig_raw) == PRISTINE[name][0], '%s: source is not pristine' % name)
    report('   source  sha1 %s  %d B  (pinned pristine: yes)' % (sha1(orig_raw), len(orig_raw)))
    want = pinned(name, variant)[0]
    report('   patched sha1 %s  %d B  (pinned patched: %s)' % (
        sha1(new_raw), len(new_raw), 'yes' if sha1(new_raw) == want else 'NO, pinned ' + want))
    if name in CIPHERED:
        need(check_valid(new_raw), '%s: footer check FAILED' % name)
        report('   footer  SHA-1(ciphered body[0:%d]) == last 20 B: OK' % (len(new_raw) - 20))
        a, b = decode(orig_raw[:-20]), decode(new_raw[:-20])
        need(encode(b) == new_raw[:-20], '%s: re-encode round trip failed' % name)
        report('   decode  round trip OK, plain %d -> %d B' % (len(a), len(b)))
    else:
        a, b = orig_raw, new_raw
        report('   plain text (fopen/fscanf), no cipher, no footer')
    need(re.search(rb'[^\x09\x0a\x0d\x20-\x7e]', b) is None or name == 'UILngKo.lng',
         '%s: non-ASCII bytes in plain text' % name)
    diff = structured_diff(a, b)
    exp = expected_changes(name)
    if name == 'windslayer.hii':
        ra, rb_ = hii_items(a), hii_items(b)
        need(len(ra) == len(rb_), 'hii: row count changed')
        need(len(diff) == len(exp) and all(d[0] == 'replace' for d in diff), 'hii: unexpected diff shape')
        for d, e in zip(diff, exp):
            td = token_delta(d[3], d[4])
            need(d[1] == e[1] and td is not None and len(td) == 1 and td[0][1:] == e[2],
                 'hii: line %d change %r not the intended %r' % (d[1], td, e))
            report('   line %5d (item %d, plain offset 0x%06X): CardNpc 0 -> %s' % (
                d[1], d[1] - 1, d[5], td[0][3].decode()))
        card = [sum(1 for r in rows if (hii_field(r, b'CardNpc') or 0) != 0) for rows in (ra, rb_)]
        need(card == [CARD_TOTAL_BEFORE, CARD_TOTAL_BEFORE + 20], 'hii: card total %r' % card)
        report('   rows %d unchanged in count; non-zero CardNpc %d -> %d' % (len(rb_), card[0], card[1]))
    elif name == 'windslayer.hui':
        wa, wb = hui_windows(a), hui_windows(b)
        need(sorted(wa) == sorted(wb), 'hui: window set changed')
        for wid in wa:
            if wid == 80:
                continue
            need(wa[wid][0].group(0) == wb[wid][0].group(0) and
                 [c.group(0) for c in wa[wid][1]] == [c.group(0) for c in wb[wid][1]],
                 'hui: window %d changed' % wid)
        h, ctls = wb[80]
        need(int(h.group(13)) == 12 and len(ctls) == 12 and ctls[11].group(0) == W80_C12,
             'hui: window 80 not as intended')
        need([c.group(0) for c in ctls[:11]] == [c.group(0) for c in wa[80][1]], 'hui: controls 1-11 changed')
        need(len(diff) == 2 and diff[0][0] == 'replace' and diff[1][0] == 'insert', 'hui: diff shape %r'
             % [d[:3] for d in diff])
        td = token_delta(diff[0][3], diff[0][4])
        need(diff[0][3] == W80_OLD and diff[0][4] == W80_NEW and td is not None and
             [t[1:] for t in td] == [(b'137', b'220', b'240'), (b'Bt_No:', b'11', b'12')],
             'hui: header change %r' % td)
        need(diff[1][4] == W80_C12 and diff[1][2] == diff[0][2] + 12, 'hui: inserted line misplaced')
        report('   %d windows (= Number_of_UI), every window except 80 byte-identical' % len(wb))
        report('   line %d (window 80 header, plain offset 0x%06X): Size 137 220 -> 137 240, Bt_No 11 -> 12'
               % (diff[0][1], diff[0][5]))
        report('   + line %d (after control 11, plain offset 0x%06X): %s' % (
            diff[1][2], diff[1][5], diff[1][4].decode().rstrip()))
        c = ctls[11]
        report('     control 12: Text %s, Pos %s,%s, Size %sx%s, Type %s, Text_Pos %s,%s, Button %s, Event %s, '
               'Enable %s; bottom edge %d <= window height 240' % (
                   c.group(2).decode(), c.group(4).decode(), c.group(5).decode(), c.group(6).decode(),
                   c.group(7).decode(), c.group(8).decode(), c.group(10).decode(), c.group(11).decode(),
                   c.group(16).decode(), c.group(18).decode(), c.group(27).decode(),
                   int(c.group(5)) + int(c.group(7))))
        if kr.get('windslayer.hui') is not None:
            wk = hui_windows(kr['windslayer.hui'])
            hk, ck = wk[80]
            need(ck[11].group(0) == W80_C12_KR, 'KR hui: window 80 control 12 is not the reference line')
            dk = token_delta(ck[11].group(0), ctls[11].group(0))
            report('   KR 2025 cross-check: window 80 header Size %s %s Bt_No %s; control 12 differs from KR only in %s'
                   % (hk.group(6).decode(), hk.group(7).decode(), hk.group(13).decode(),
                      ', '.join('%s %s->%s' % (t[1].decode(), t[2].decode(), t[3].decode()) for t in dk)))
    elif name == 'windslayer.hni':
        verify_hni(a, b, orig_raw, new_raw, variant, diff, hii_plain, kr.get('windslayer.hni'), report)
    else:
        ra, rb_ = lng_records(a), lng_records(b)
        need(len(rb_) == len(ra) + 1, 'lng: record count %d -> %d' % (len(ra), len(rb_)))
        ids = [r[0] for r in rb_]
        need(len(set(ids)) == len(ids) and ids == sorted(ids), 'lng: ids not unique/sorted')
        need(dict(rb_)[LNG_ID] == LNG_TEXT.replace(b'#', b' '), 'lng: 8038 text')
        need([r for r in rb_ if r[0] != LNG_ID] == ra, 'lng: other records changed')
        need(len(diff) == 1 and diff[0][0] == 'insert' and diff[0][4] == exp[0][2], 'lng: diff %r' % diff)
        report('   %d -> %d records (fscanf "%%d %%s" walk), ids unique and sorted' % (len(ra), len(rb_)))
        report('   + line %d (plain offset 0x%06X, between 8034 and 8039): %s  -> "%s"' % (
            diff[0][2], diff[0][5], diff[0][4].decode(), LNG_TEXT.replace(b'#', b' ').decode()))
    report('   structured diff == intended change set: OK')
    return b


def check_text_ids(hui_plain, lng_plain, report=print):
    """Window 80's text ids all resolve in UILngKo (FUN_004051f0 linear lookup by id)."""
    have = {i for i, _ in lng_records(lng_plain)}
    w = hui_windows(hui_plain)
    h, ctls = w[80]
    ids = [int(h.group(2))] + [int(c.group(2)) for c in ctls]
    miss = [i for i in ids if i not in have]
    need(not miss, 'window 80 text ids missing from UILngKo: %r' % miss)
    lng = dict(lng_records(lng_plain))
    report('   window 80 labels: ' + ' | '.join(lng[i].decode('latin-1') for i in ids[1:]))


# ------------------------------------------------------------------------------------- helpers
def find_src(d, name):
    for p in (os.path.join(d, 'hs', name), os.path.join(d, name)):
        if os.path.isfile(p):
            return p
    raise PatchError('%s not found in %s or %s\\hs' % (name, d, d))


def read(p):
    with open(p, 'rb') as f:
        return f.read()


def write_atomic(path, data):
    """Write through <path>.gcp-tmp + os.replace (the commit point). On an OS error the tmp file is
    removed and WriteError names the file; the target keeps its previous content."""
    tmp = path + TMP_SUFFIX
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        left = ''
        try:
            if os.path.lexists(tmp):
                os.remove(tmp)
        except OSError as e2:
            left = '; could not remove %s either (%s): delete it by hand' % (tmp, e2.strerror or e2)
        raise WriteError('%s: write failed (%s)%s; the file keeps its previous content'
                         % (path, e.strerror or e, left))
    need(read(path) == data, '%s: read-back mismatch after write' % path)


def norm(p):
    """Comparable form of a path: absolute, links resolved, case-folded (NTFS is case-insensitive)."""
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def game_ancestor(d):
    """The nearest folder at or above d that holds WindSlayer.exe (a game install or a pristine
    extraction), or None. d need not exist."""
    p = norm(d)
    while True:
        if os.path.isfile(os.path.join(p, 'WindSlayer.exe')):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def same_file(a, b):
    if norm(a) == norm(b):
        return True
    try:
        return os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return False


def selected(only):
    """The patch set --only names (default: every patch)."""
    return _V(only or PATCHES)


def requested(name, only):
    """The patches of file `name` that --only selects."""
    return file_patches(name) & selected(only)


def selected_files(only):
    return [n for n in FILE_ORDER if requested(n, only)]


def hii_plain_from(d):
    """A decoded EN hii (the pristine one or a pinned cp-1 output; rows 4283..4285 are the same in
    both) from folder d, for the hni price checks."""
    raw = read(find_src(d, 'windslayer.hii'))
    ok = {PRISTINE['windslayer.hii'][0]} | {v[0] for v in PATCHED['windslayer.hii'].values()}
    need(sha1(raw) in ok, '%s: windslayer.hii is neither pristine nor a pinned cp-1 output' % d)
    return unseal('windslayer.hii', raw)


# --------------------------------------------------------------------------------------- modes
def do_build(src, out, only):
    g = game_ancestor(out)
    need(g is None, '--out %s is at or below %s, which holds WindSlayer.exe (a game install or a pristine '
         'extraction); build copies elsewhere and use --install for the game' % (out, g))
    hs = os.path.join(out, 'hs')
    jobs = []
    for name in selected_files(only):                     # 1. every check, nothing created or written
        variant = requested(name, only)
        sp = find_src(src, name)
        dp = os.path.join(hs, name)
        need(norm(os.path.dirname(sp)) != norm(hs) and not same_file(sp, dp),
             'source and output are the same file: %s -> %s (paths compare case-insensitively)' % (sp, dp))
        orig = read(sp)
        new = build_one(name, orig, variant)
        verify_pair(name, orig, new, variant,
                    hii_plain=hii_plain_from(src) if name == 'windslayer.hni' else None)
        need(sha1(new) == pinned(name, variant)[0], '%s: output %s is not the pinned patched SHA-1 of %s'
             % (name, sha1(new), vname(variant)))
        jobs.append((name, dp, new))
    if 'cp-4' in selected(only):
        built = dict((n, b) for n, _d, b in jobs)
        check_text_ids(unseal('windslayer.hui', built['windslayer.hui']), built['UILngKo.lng'])
    os.makedirs(hs, exist_ok=True)                         # 2. only now: create and write
    for name, dp, new in jobs:
        write_atomic(dp, new)
        print('   wrote %s' % dp)
    print('build OK')


def do_verify(built, src, kr, only):
    names = selected_files(only)
    krp = {}
    for name in ('windslayer.hui', 'windslayer.hni'):
        if kr and name in names:
            krp[name] = unseal(name, read(find_src(kr, name)))
    for name in names:
        variant = requested(name, only)
        orig = read(find_src(src, name))
        new = read(find_src(built, name))
        verify_pair(name, orig, new, variant, kr=krp,
                    hii_plain=hii_plain_from(src) if name == 'windslayer.hni' else None)
        need(sha1(new) == pinned(name, variant)[0], '%s: not the pinned patched SHA-1 of %s'
             % (name, vname(variant)))
    if 'cp-4' in selected(only):
        check_text_ids(unseal('windslayer.hui', read(find_src(built, 'windslayer.hui'))),
                       read(find_src(built, 'UILngKo.lng')))
    print('verify OK')


def state_of(game, name):
    """(path, backup path, state, backup state, variant). state: pristine / patched / missing /
    UNKNOWN; variant: the patch set the file holds (empty when pristine, None when not known)."""
    p = os.path.join(game, 'hs', name)
    b = p + BACKUP_SUFFIX
    cur = sha1(read(p)) if os.path.isfile(p) else None
    st, variant = 'UNKNOWN', None
    if cur is None:
        st = 'missing'
    elif cur == PRISTINE[name][0]:
        st, variant = 'pristine', _V()
    else:
        for v, (h, _size) in PATCHED[name].items():
            if cur == h:
                st, variant = 'patched', v
    bak = None
    if os.path.exists(b):
        bak = 'ok' if os.path.isfile(b) and sha1(read(b)) == PRISTINE[name][0] else 'DIFFERS'
    return p, b, st, bak, variant


def game_dir(g):
    need(os.path.isdir(os.path.join(g, 'hs')) and os.path.isfile(os.path.join(g, 'WindSlayer.exe')),
         '--game %s has no hs\\ or no WindSlayer.exe' % g)
    return g


def strays(game):
    """Leftovers a crash or a folder-sync client can leave next to the data files."""
    hs = os.path.join(game, 'hs')
    stems = [n.rsplit('.', 1)[0].lower() for n in FILE_ORDER]
    return sorted(f for f in os.listdir(hs)
                  if f.endswith(TMP_SUFFIX) or ('name clash' in f.lower() or 'conflict' in f.lower())
                  and any(f.lower().startswith(s) for s in stems))


def own_tmps(game):
    """This script's own temp files (<file>.gcp-tmp, <file>.orig-pre-gcp.gcp-tmp). os.replace is the
    commit point, so a leftover one is never the only copy of anything."""
    hs = os.path.join(game, 'hs')
    return [t for n in FILE_ORDER for t in (os.path.join(hs, n) + TMP_SUFFIX,
                                             os.path.join(hs, n) + BACKUP_SUFFIX + TMP_SUFFIX)
            if os.path.lexists(t)]


def settle_check(game, want, settle):
    """Re-read every file after a pause: a folder-sync client (one turned a
    rename-over into a '(# Name clash ... #)' copy and put the OLD content back) must not undo us.
    want = {file name: the patch set it must hold (empty = pristine)}."""
    if settle > 0:
        time.sleep(settle)
    bad = []
    for n, v in want.items():
        _p, _b, st, _bak, got = state_of(game, n)
        if got != v:
            bad.append((n, st if got is None else 'pristine' if not got else vname(got),
                        'pristine' if not v else vname(v)))
    s = strays(game)
    if s:
        print('WARNING: stray files in hs\\: %s' % ', '.join(s))
    need(not bad, 'after %.0f s these files are not as intended (file, state, wanted): %r (a sync client may '
         'have reverted them; run --status)' % (settle, bad))


def do_status(game):
    for name in FILE_ORDER:
        p, b, st, bak, v = state_of(game, name)
        print('%-15s %-21s backup: %s' % (name, '%s %s' % (st, vname(v)) if st == 'patched' else st,
                                          {None: 'none', 'ok': 'ok (pristine)',
                                           'DIFFERS': 'DIFFERS from pristine'}[bak]))
    s = strays(game)
    print('stray files in hs\\: %s' % (', '.join(s) if s else 'none'))


def do_install(game, only, settle):
    """Adds the selected patches to what each file already holds. The build source is always the
    pristine original: the file itself while pristine, else its checked backup (read once)."""
    names = selected_files(only)
    plan = []
    for name in names:                    # preflight: build + check everything before the first write
        p, b, st, bak, cur = state_of(game, name)
        need(st in ('pristine', 'patched'), '%s: current content is %s (sha1 not pinned); refusing' % (p, st))
        need(bak != 'DIFFERS', '%s exists and differs from the pristine original; refusing' % b)
        need(st == 'pristine' or bak == 'ok', '%s is patched but has no pristine backup; refusing' % p)
        target = cur | requested(name, only)
        orig = new = None
        if target != cur:
            src = p if st == 'pristine' else b
            orig = read(src)
            need(sha1(orig) == PRISTINE[name][0], '%s changed while being checked; refusing' % src)
            new = build_one(name, orig, target)
            need(sha1(new) == pinned(name, target)[0], '%s: built output is not the pinned patched SHA-1 of %s'
                 % (name, vname(target)))
        plan.append((name, p, b, st, bak, cur, target, orig, new))
    tmps = [f for f in strays(game) if f.endswith(TMP_SUFFIX)]
    need(not tmps, 'hs\\ holds %s, left by an interrupted run; delete them (or run --uninstall, which removes '
         'its own) and rerun --install' % ', '.join(tmps))
    for name, p, b, st, bak, cur, target, orig, new in plan:
        if new is None:
            print('%s: already installed (%s)' % (name, vname(cur)))
            continue
        if bak is None:                   # only a pristine file can lack it (preflight)
            write_atomic(b, orig)
            print('%s: backup -> %s' % (name, b))
        write_atomic(p, new)
        need(state_of(game, name)[4] == target, '%s: post-install check failed' % name)
        print('%s: installed %s (%s)%s' % (name, vname(target), pinned(name, target)[0],
                                           ', was %s' % vname(cur) if cur else ''))
    settle_check(game, dict((x[0], x[6]) for x in plan), settle)
    print('install OK')


def do_uninstall(game, only, settle):
    """Removes the selected patches. A file left with none is restored from its backup (read once,
    checked, exactly those bytes written), and the backup is deleted after the settle re-check; a
    file left with some (the hni: one of cp-3d/cp-5) is rebuilt from the backup, which stays."""
    names = selected_files(only)
    plan = []
    for name in names:
        p, b, st, bak, cur = state_of(game, name)
        need(st in ('pristine', 'patched', 'missing'), '%s: current content is %s; refusing' % (p, st))
        need(bak != 'DIFFERS', '%s differs from the pristine original; refusing' % b)
        need(bak == 'ok' or st == 'pristine', '%s: no backup %s; refusing' % (p, b))
        target = cur - requested(name, only) if st == 'patched' else _V()
        new = None
        if st == 'missing' or (st == 'patched' and target != cur):
            good = read(b)                # read the backup ONCE; these checked bytes are the source
            need(sha1(good) == PRISTINE[name][0], '%s changed while being checked; refusing' % b)
            new = build_one(name, good, target) if target else good
            if target:
                need(sha1(new) == pinned(name, target)[0], '%s: rebuilt output is not the pinned SHA-1 of %s'
                     % (name, vname(target)))
        plan.append((name, p, b, st, bak, cur, target, new))
    for t in own_tmps(game):
        os.remove(t)
        print('removed leftover %s' % t)
    for name, p, b, st, bak, cur, target, new in plan:
        if new is not None:
            write_atomic(p, new)
        need(state_of(game, name)[4] == target, '%s: restore check failed' % name)
        if not target:
            print('%s: pristine%s' % (name, ' (restored from backup)' if new is not None else ''))
        elif new is not None:
            print('%s: now %s (rebuilt from the backup, which is kept)' % (name, vname(target)))
        else:
            print('%s: %s kept (%s not installed)' % (name, vname(cur), vname(requested(name, only))))
    settle_check(game, dict((x[0], x[6]) for x in plan), settle)
    for name, p, b, st, bak, cur, target, new in plan:    # backups go only after the files have settled
        if bak == 'ok' and not target:
            os.remove(b)
            print('%s: removed backup' % name)
    print('uninstall OK')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--verify', metavar='DIR', help='verify built copies in DIR (or DIR\\hs)')
    mode.add_argument('--status', action='store_true', help='read-only state of --game')
    mode.add_argument('--install', action='store_true', help='install into --game (modifies your game files; backups are kept)')
    mode.add_argument('--uninstall', action='store_true',
                      help='remove patches from --game (restores from backups)')
    ap.add_argument('--src', metavar='DIR', help='pristine sources (read only)')
    ap.add_argument('--out', metavar='DIR', help='build output folder (files go to DIR\\hs)')
    ap.add_argument('--game', metavar='DIR', help='game install folder (holds WindSlayer.exe and hs\\)')
    ap.add_argument('--kr', metavar='DIR',
                    help='KR 2025 install for the control-12 and hni cross-checks (read only)')
    ap.add_argument('--only', action='append', choices=sorted(PATCHES),
                    help='limit to cp-1 / cp-3d / cp-4 / cp-5 (repeatable; default: all)')
    ap.add_argument('--settle', metavar='SEC', type=float, default=10.0,
                    help='install/uninstall: re-check every file after this pause (default 10)')
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    try:
        if a.status or a.install or a.uninstall:
            need(a.game, '--game DIR is required')
            g = game_dir(a.game)
            if a.status:
                do_status(g)
            elif a.install:
                do_install(g, a.only, a.settle)
            else:
                do_uninstall(g, a.only, a.settle)
        elif a.verify:
            need(a.src, '--src DIR is required')
            do_verify(a.verify, a.src, a.kr, a.only)
        else:
            need(a.src and a.out, 'build needs --src DIR and --out DIR (see --help)')
            do_build(a.src, a.out, a.only)
    except (PatchError, OSError) as e:
        io = isinstance(e, (WriteError, OSError))
        if isinstance(e, OSError):        # a read/remove outside write_atomic (locked file, permissions)
            e = '%s: %s' % (getattr(e, 'filename', None) or 'file access', e.strerror or e)
        print('ERROR: %s' % e, file=sys.stderr)
        if io and (a.install or a.uninstall):
            print('The steps printed above were completed; the rest were not. See the state with\n'
                  '  python patch_data_2009.py --status --game "%s"\n'
                  'then fix the cause (close the game, clear a read-only flag, pause the sync client) and\n'
                  'rerun --install (it skips what is already installed) or run --uninstall.' % a.game,
                  file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
