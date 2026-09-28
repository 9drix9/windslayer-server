#!/usr/bin/env python3
"""
packets.py - the single spec-driven packet layer (roadmap F1: lc-codec)
=======================================================================
Every packet the server builds or parses goes through the byte-exact grammars in
protocol_spec.json (wsproto DSL), loaded once and cached per key. New server
code must not hand-pack payloads with struct.

Client builds (client-2009-login)
---------------------------------
Two clients are supported, each with its own spec file (SPEC_FILES): '2008'
protocol_spec.json (the default) and '2009' protocol_spec_2009.json (EN Outspark v1.04
Build 14; its C2S keys carry 2009 send VAs, e.g. '0x451CE5/0x01'). Every public function
takes a keyword `client_build` (None = DEFAULT_BUILD, '2008'); send() takes it from the
server (server.client_build = config CLIENT_BUILD). The build is always an argument, never
a module global, so tests run 2008 and 2009 servers in one process without leaking state.
DEFAULT_ASSUME is the 2008 table; the 2009 one (assume_table('2009')) keeps every 2008
entry whose condition text still exists in the 2009 grammar plus DEFAULT_ASSUME_2009.

    import packets as P
    body = P.build(0x16, {'sender_name': P.name17(name), 'text': text})   # S2C payload
    P.send(server, sock, session, 0x16, {...})                           # build + encrypt + send
    rec = P.parse(0x7E, payload)                                         # C2S: tries every send site
    rec.key, rec.name, rec.candidates, rec.assume                        # which variant matched

Keys: S2C '0x4F' (or the int 0x4F), C2S send-site keys '0x4484CC/0x27' (or the
opcode, resolved by exact length/shape), UDP 'UDP-S2C:0x06' / 'UDP-C2S:0x4236F5/0x03'.
A full key only resolves in its own direction: build('UDP-S2C:0x06') needs
direction='UDP-S2C', so a UDP or C2S body can never go out as a TCP S2C packet
that shares its opcode byte (send() and wsdev sendspec are TCP S2C only).

Receiver-state rule (roadmap 1.2.3, bugs S1-02/S1-03)
-----------------------------------------------------
Some grammars branch on state only the receiving client knows ("is this uid
spawned", "am I in a messenger room"). wsproto reads an unstated condition as
False, which silently truncates packets (0x61 -> 0 B, 0x96 -> 8 B). build():
  1. applies DEFAULT_ASSUME[key]: the form the server commits to. The caller must
     only send that packet to receivers for whom the form is true (spawned uid,
     room member, ...). Entries that depend on who receives it (0x72/0x76 local
     form, 0x2A target) resolve from `receiver_uid`;
  2. applies rec['__assume__'] and the `assume` argument ({condition text: bool});
  3. raises MissingAssume if any remaining condition would change the bytes,
     unless allow_unassumed=True (the old wsproto default-False behaviour).

Text safety (roadmap 1.2.4, S1-13, S1-14)
-----------------------------------------
Every fixed str[N] field (str[17] names, str[25] titles, ...) goes through
cut_text(v, N-1) in build(), so it ends in a NUL without splitting a cp949
character (name17() does the same for callers that want the padded bytes).
Length-prefixed text is clamped to the client's stack buffers: 0x16 <= 60,
0x09/0x0A <= 60 and the sprintf limits with the name, 0x61 <= 58, 0x15 <= 88,
0x90/0x91 <= 87. build() fills a missing length field from the clamped text and
refuses an explicit length over the limit. Text is raw bytes end to end (cp949 on
the client): bytes pass through, see to_bytes() for str.

List safety (trade-codec guard; spec gates_and_hazards)
-------------------------------------------------------
Item descriptor option lists (u8 n + n x u16) land in a 6-word client buffer;
build() refuses n > 5 for every key in LIST_CAPS. canonical_options() gives the
wire form of a stored block for comparisons (trade.md "Item descriptor").
"""
import json
import os
import re
import sqlite3
import struct
import threading
from collections import defaultdict

from wsproto import Grammar, GrammarError, _eval, ClientStateCondition

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.path.join(HERE, 'protocol_spec.json')
GAMEDEF_PATH = os.path.join(HERE, 'gamedef.sqlite3')
# config CLIENT_BUILD -> spec file (client-2009-login). '2008' is the default everywhere.
DEFAULT_BUILD = '2008'
SPEC_FILES = {'2008': 'protocol_spec.json', '2009': 'protocol_spec_2009.json'}


def _build_name(client_build):
    build = DEFAULT_BUILD if client_build is None else str(client_build)
    if build not in SPEC_FILES:
        raise ValueError(f'client build must be one of {sorted(SPEC_FILES)}, not {client_build!r}')
    return build


def spec_path(client_build=None):
    """The spec file of a client build."""
    build = _build_name(client_build)
    return SPEC_PATH if build == DEFAULT_BUILD else os.path.join(HERE, SPEC_FILES[build])


class PacketError(Exception):
    pass


class MissingAssume(PacketError):
    """A client-state condition changes the encoded bytes but nobody said which way."""

    def __init__(self, key, conditions):
        self.key = key
        self.conditions = list(conditions)
        super().__init__(f'{key}: client-state condition(s) change the packet bytes but have no assume '
                         f'value: {self.conditions}. Pass assume={{condition: True|False}} for the '
                         f'receiver, or allow_unassumed=True.')


class ParseError(PacketError):
    pass


# ------------------------------------------------------------------ spec db ---
class _SpecDB:
    def __init__(self, path, client_build=DEFAULT_BUILD):
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        self.path = path
        self.client_build = client_build
        self._assume = None                  # assume_table(): built on first use
        self.meta = d.get('meta', {})
        self.by_key = {}
        self.s2c = {}                        # opcode int -> spec (TCP)
        self.c2s = defaultdict(list)         # opcode int -> [spec] in spec order (TCP)
        self.udp_s2c = {}                    # opcode int -> spec
        self.udp_c2s = defaultdict(list)     # opcode int -> [spec]
        for s in d['specs']:
            if s.get('grammar') is None:
                continue
            key = s['key']
            self.by_key[key] = s
            op = int(s['opcode'], 16)
            if key.startswith('UDP-S2C:'):
                self.udp_s2c[op] = s
            elif key.startswith('UDP-C2S:'):
                self.udp_c2s[op].append(s)
            elif s['direction'] == 'S2C':
                self.s2c[op] = s
            elif s['direction'] == 'C2S':
                self.c2s[op].append(s)
        self._grammars = {}
        self._glock = threading.Lock()

    def grammar(self, key):
        g = self._grammars.get(key)
        if g is None:
            with self._glock:
                g = self._grammars.get(key)
                if g is None:
                    g = self._grammars[key] = Grammar(self.by_key[key]['grammar'])
        return g


_DBS = {}                                   # client build -> _SpecDB
_DB_LOCK = threading.Lock()


def load(path=None, client_build=None):
    """(Re)load a build's spec. Called lazily on first use; tests may point it elsewhere
    (load(path) replaces the default 2008 spec, as it did before the 2009 build existed)."""
    build = _build_name(client_build)
    with _DB_LOCK:
        db = _DBS[build] = _SpecDB(path or spec_path(build), build)
    return db


def _db(client_build=None):
    build = _build_name(client_build)
    db = _DBS.get(build)
    if db is None:
        with _DB_LOCK:
            db = _DBS.get(build)
            if db is None:
                db = _DBS[build] = _SpecDB(spec_path(build), build)
    return db


_OPCODE_TEXT = re.compile(r'^(?:0[xX])?([0-9a-fA-F]{1,2})$')


def _opcode_of(key):
    if isinstance(key, int):
        return key
    m = _OPCODE_TEXT.match(str(key).strip())
    return int(m.group(1), 16) if m else None


DIRECTIONS = ('S2C', 'C2S', 'UDP-S2C', 'UDP-C2S')


def key_direction(s):
    """Direction a spec's key belongs to. The key shape decides it: S2C keys are the
    plain opcode, C2S keys '<send_va>/0xNN', UDP keys carry their 'UDP-S2C:' /
    'UDP-C2S:' prefix (the UDP specs' own `direction` field only says S2C/C2S)."""
    key = s['key']
    for prefix in ('UDP-S2C', 'UDP-C2S'):
        if key.startswith(prefix + ':'):
            return prefix
    return s['direction']


def variants(key, direction='C2S', *, client_build=None):
    """Every spec a key/opcode can mean, in spec order. A send-site or UDP key names
    one spec and must belong to `direction`; opcode-shaped keys ('0x7E', 0x7E, '7E' -
    S2C keys look like this too) resolve through `direction`."""
    if direction not in DIRECTIONS:
        raise ValueError(f'direction must be S2C, C2S, UDP-S2C or UDP-C2S, not {direction!r}')
    db = _db(client_build)
    op = _opcode_of(key)
    if op is None:
        if isinstance(key, str) and key in db.by_key:
            s = db.by_key[key]
            if key_direction(s) != direction:
                # UDP-S2C:0x06 / 0x4484CC/0x27 share opcode bytes with TCP S2C packets;
                # resolving them as S2C would inject the wrong body (roadmap 4.2).
                raise KeyError(f'{key!r} is a {key_direction(s)} key, not {direction}')
            return [s]
        raise KeyError(f'unknown packet key {key!r}')
    if direction == 'S2C':
        found = [db.s2c[op]] if op in db.s2c else []
    elif direction == 'C2S':
        found = list(db.c2s.get(op, []))
    elif direction == 'UDP-S2C':
        found = [db.udp_s2c[op]] if op in db.udp_s2c else []
    else:
        found = list(db.udp_c2s.get(op, []))
    if not found:
        raise KeyError(f'no {direction} spec for {key!r}')
    return found


def spec(key, direction='S2C', *, client_build=None):
    """The single spec for a key (an opcode must be unambiguous in `direction`)."""
    found = variants(key, direction, client_build=client_build)
    if len(found) > 1:
        raise KeyError(f'{key!r} has {len(found)} {direction} send sites '
                       f'({[s["key"] for s in found]}); use the full key')
    return found[0]


def grammar(key, direction='S2C', *, client_build=None):
    return _db(client_build).grammar(spec(key, direction, client_build=client_build)['key'])


def client_state_exprs(key, direction='S2C', *, client_build=None):
    """Conditions in the grammar that packet fields alone cannot decide."""
    return grammar(key, direction, client_build=client_build).client_state_exprs()


# ------------------------------------------------------------- text helpers ---
def to_bytes(s):
    """Wire text. bytes pass through. A str made only of U+0000..U+00FF is raw bytes
    (latin-1): wsproto and parse() return str fields that way, so parse -> build is
    byte-exact even for cp949 names. Any other str ('한') is cp949, the client's
    codepage, instead of the UnicodeEncodeError a plain latin-1 encode would raise."""
    if s is None:
        return b''
    if isinstance(s, (bytes, bytearray)):
        return bytes(s)
    s = str(s)
    try:
        return s.encode('latin-1')
    except UnicodeEncodeError:
        return s.encode('cp949', 'replace')


def cut_text(s, limit):
    """NUL-cut, then truncate to `limit` bytes without splitting a cp949 double-byte
    character (lead bytes 0x81..0xFE take the next byte with them)."""
    b = to_bytes(s).split(b'\x00', 1)[0]
    if limit <= 0:
        return b''
    if len(b) <= limit:
        return b
    i = 0
    while i < len(b):
        step = 2 if 0x81 <= b[i] <= 0xFE and i + 1 < len(b) else 1
        if i + step > limit:
            break
        i += step
    return b[:i]


def name17(s):
    """A str[17] name: at most 16 bytes then NUL padding, so client strcpy/sprintf
    always stops inside the field (roadmap S1-13)."""
    return cut_text(s, 16).ljust(17, b'\x00')


def _name_len(v):
    return len(cut_text(v, 16))


def canonical_options(words):
    """Wire form of an item descriptor's option words: the leading non-zero words among
    words 0..4 (at most 5). The client stops counting at the first zero, so [5, 0, 7]
    travels as [5]; compare stored blocks in this form (trade.md "Item descriptor wire
    encoding"). Word 5 (opt_extra / item_ext) is a separate field and not included."""
    out = []
    for w in list(words)[:OPTION_LIST_MAX]:
        w = int(w) & 0xFFFF
        if w == 0:
            break
        out.append(w)
    return out


# ------------------------------------------------------------- list caps ---
OPTION_LIST_MAX = 5

# key -> {u8 count field: max}. Item descriptor option lists are read into a 6-word
# (12 B) client buffer whose word 5 is the trailing extra word, and no handler checks
# the count (spec gates_and_hazards; trade.md: "Never send n > 5"). Checked in build().
LIST_CAPS = {
    '0x03': {'option_count': OPTION_LIST_MAX},   # >5 corrupts the next item record / arrays
    '0x11': {'opt_count': OPTION_LIST_MAX},      # 12-byte stack buffer (esp+0x218)
    '0x12': {'opt_count': OPTION_LIST_MAX},      # same stack overflow as 0x11
    '0x19': {'opt_count': OPTION_LIST_MAX},      # >=63 reaches the /GS cookie
    '0x1D': {'stone_count': OPTION_LIST_MAX},    # 6 loses stone 6, >=7 writes past the block
    '0x1E': {'option_count': OPTION_LIST_MAX},   # 12-byte equip record (duplication hazard)
    '0x23': {'opt_count': OPTION_LIST_MAX},      # same as 0x19
    '0x24': {'option_count': OPTION_LIST_MAX},   # same 12-byte record as 0x1E
    '0x4B': {'opt_count': OPTION_LIST_MAX},      # >=59 reaches the /GS cookie
    '0x52': {'option_count': OPTION_LIST_MAX},   # spills into the next equip entry
    '0x65': {'option_count': OPTION_LIST_MAX},   # spills into equip_attr_last / next slot
    '0x66': {'opt_count': OPTION_LIST_MAX},
    '0x67': {'opt_count': OPTION_LIST_MAX},
    '0x87': {'opt_count': OPTION_LIST_MAX},      # >=69 reaches the /GS cookie
    '0x88': {'opt_count': OPTION_LIST_MAX},
    '0x89': {'opt_count': OPTION_LIST_MAX},
}
_OPTION_LIST_NAME = re.compile(r'opt|option|stone|socket|enchant')


def item_descriptor(rec):
    """(item_id, qty, n, opts, extra) of a decoded C2S item descriptor (shop_storage.md 1.2,
    3.5): u16 id, u16 qty, u8 n, n x u16, u16 extra. The send sites name the same five
    fields differently - 0x0C / 0x3C socket_count + socket_stone_id + item_extra, the
    equipment 0x3D opt_count + opt + attr_0c (2008 0x469BA8, 2009 0x473E14), the stackable
    2008 0x469D9C/0x3D socket_count + item_extra with no list - and the 0x3C non-equipment
    branch (spec `if(item_type == 1)`, parse() takes it when the u8 is 0) writes `u8 zero,
    u16 zero` under ONE key, so the record keeps the LAST value: the u16 extra word. n is
    the count as sent (callers refuse n > OPTION_LIST_MAX: the client's 6-word buffer)."""
    item = int(rec.get('item_id', 0))
    qty = int(rec.get('qty', rec.get('amount', 0)))
    if 'socket_count' in rec or 'opt_count' in rec:
        count_field = 'socket_count' if 'socket_count' in rec else 'opt_count'
        n = int(rec.get(count_field, 0))
        opts = [int(next(iter(e.values()), 0)) if isinstance(e, dict) else int(e)
                for e in rec.get(f'repeat[{count_field}]', [])]
        extra = int(rec.get('item_extra', rec.get('attr_0c', 0)))
    else:
        n, opts, extra = 0, [], int(rec.get('zero', 0))
    return item, qty, n, opts, extra


def _walk_fields(nodes, rec, visit):
    """Call visit(node, level) for every field node of a grammar with the record dict
    that holds its value. repeat blocks descend into rec['repeat[<expr>]'] (copied, so
    the caller's nested dicts are never modified); both if branches are visited, since
    a value in the untaken branch never reaches the wire."""
    for n in nodes:
        if n[0] in ('scalar', 'str', 'bytes'):
            visit(n, rec)
        elif n[0] == 'repeat':
            k = f'repeat[{n[1]}]'
            items = rec.get(k)
            if isinstance(items, (list, tuple)):
                rec[k] = items = [dict(item) for item in items]
                for item in items:
                    _walk_fields(n[2], item, visit)
        elif n[0] == 'if':
            _walk_fields(n[2], rec, visit)
            _walk_fields(n[3], rec, visit)


def _apply_field_rules(skey, g, rec, unsafe_counts):
    """str[N] literals -> cut_text(v, N-1) (name17 for str[17]); LIST_CAPS counts refused."""
    caps = LIST_CAPS.get(skey, {})

    def visit(n, level):
        name = n[2]
        if name not in level:
            return
        if n[0] == 'str' and n[1].isdigit():
            level[name] = cut_text(level[name], int(n[1]) - 1)
        elif n[0] == 'scalar' and name in caps and not unsafe_counts:
            count = int(level[name])
            if not 0 <= count <= caps[name]:
                raise PacketError(f'{skey}: {name}={count} overflows the client option buffer '
                                  f'(max {caps[name]}; spec gates_and_hazards, trade-codec guard); '
                                  f'use packets.canonical_options()')
    _walk_fields(g.nodes, rec, visit)


def check_list_caps(client_build=None):
    """Problems in LIST_CAPS: an entry that is not a u8 repeat count of its grammar, or an
    S2C option/stone/socket list (u8 count, single u16 body) the table does not cover."""
    problems = []
    db = _db(client_build)

    def lists(nodes, scalars, out):
        local = dict(scalars)
        for n in nodes:
            if n[0] == 'scalar':
                local[n[2]] = n[1]
            elif n[0] == 'repeat':
                body = n[2]
                if (len(body) == 1 and body[0][0] == 'scalar' and body[0][1] == 'u16'
                        and local.get(n[1]) == 'u8'):
                    out.append((n[1], body[0][2]))
                lists(body, local, out)
            elif n[0] == 'if':
                lists(n[2], local, out)
                lists(n[3], local, out)
        return out

    for key, caps in LIST_CAPS.items():
        s = db.s2c.get(int(key, 16))
        if s is None or s['key'] != key:
            problems.append(f'{key}: no such S2C spec')
            continue
        counts = {c for c, _ in lists(db.grammar(key).nodes, {}, [])}
        for field in caps:
            if field not in counts:
                problems.append(f'{key}: {field} is not a u8 count of a u16 list in the grammar')
    for s in db.s2c.values():
        for count, elem in lists(db.grammar(s['key']).nodes, {}, []):
            if (_OPTION_LIST_NAME.search(count) or _OPTION_LIST_NAME.search(elem)) \
                    and count not in LIST_CAPS.get(s['key'], {}):
                problems.append(f'{s["key"]}: option list {count} x {elem} has no LIST_CAPS entry')
    return problems


def _whisper_result_limit(rec):
    # S2C 0x09 sprintf_s into 88 B: status 0x65 "<To: %s> %s" -> name + msg <= 80;
    # channel form -> name + digits(status) + msg <= 70; 0x66/0x67 carry no message.
    status = int(rec.get('status', 0)) & 0xFF
    if status in (0x66, 0x67):
        return None
    name = _name_len(rec.get('target_name'))
    if status == 0x65:
        return min(60, 80 - name)
    return min(60, 70 - name - len(str(status)))


def _whisper_received_limit(rec):
    # S2C 0x0A: channel 0x65 -> name + msg <= 78; channel form -> name + digits + msg <= 68.
    channel = int(rec.get('channel', 0)) & 0xFF
    name = _name_len(rec.get('sender_name'))
    if channel == 0x65:
        return min(60, 78 - name)
    return min(60, 68 - name - len(str(channel)))


# key -> (length field, text field, max bytes | fn(rec) -> max bytes or None)
TEXT_FIELDS = {
    '0x16': ('text_len', 'text', 60),      # 61-byte stack buffer; 61 leaves no NUL
    '0x09': ('msg_len', 'message', _whisper_result_limit),
    '0x0A': ('msg_len', 'message', _whisper_received_limit),
    '0x61': ('msg_len', 'message', 58),    # _sprintf_s(buf, 0x3D, "  %s")
    '0x15': ('text_len', 'text', 88),      # 89-byte buffer
    '0x90': ('text_len', 'text', 87),      # 88-byte buffer
    '0x91': ('text_len', 'text', 87),
}


def text_limit(key, rec=None, *, client_build=None):
    """Max text bytes the receiving client can hold for this packet (None = no text)."""
    entry = TEXT_FIELDS.get(spec(key, client_build=client_build)['key'])
    if not entry:
        return None
    limit = entry[2]
    value = limit(rec or {}) if callable(limit) else limit
    return None if value is None else max(0, value)


def clamp_text(key, text, rec=None, *, client_build=None):
    """Text cut at NUL and clamped to the receiver's buffer for S2C `key`."""
    limit = text_limit(key, rec, client_build=client_build)
    return b'' if limit is None else cut_text(text, limit)


def _apply_text_rules(skey, rec, unsafe, client_build=None):
    entry = TEXT_FIELDS.get(skey)
    if not entry:
        return
    len_f, txt_f, _ = entry
    limit = text_limit(skey, rec, client_build=client_build)
    if limit is None:
        return
    if len_f not in rec:
        data = cut_text(rec.get(txt_f, b''), limit) if not unsafe else to_bytes(rec.get(txt_f, b''))
        rec[txt_f] = data
        rec[len_f] = len(data)
        return
    if not unsafe and int(rec[len_f]) > limit:
        raise PacketError(f'{skey}: {len_f}={rec[len_f]} exceeds the client buffer limit {limit} '
                          f'(roadmap S1-14); clamp with packets.clamp_text()')
    if txt_f in rec and not isinstance(rec[txt_f], (bytes, bytearray)):
        rec[txt_f] = to_bytes(rec[txt_f])


# ----------------------------------------------------------- assume table ---
class Receiver:
    """What build() knows about the client that will read the packet."""
    __slots__ = ('uid',)

    def __init__(self, uid=None):
        self.uid = None if uid is None else int(uid)


def _field_is_receiver(field):
    def resolve(rec, rx):
        return None if rx.uid is None else int(rec.get(field, 0)) == rx.uid
    resolve.__doc__ = f'{field} == receiver uid'
    return resolve


def _field_is_not_receiver(field):
    def resolve(rec, rx):
        return None if rx.uid is None else int(rec.get(field, 0)) != rx.uid
    resolve.__doc__ = f'{field} != receiver uid'
    return resolve


_CASH_T = {}
_CASH_T_LOCK = threading.Lock()


def cash_duration_type(item_id):
    """gamedef items.Cash_T (0 permanent, 1 counted, 2 period) or None if unknown.
    The EN client's item def +0x1F6 is this column (premium_cash.md 1.7)."""
    item_id = int(item_id)
    with _CASH_T_LOCK:
        if item_id in _CASH_T:
            return _CASH_T[item_id]
        value = None
        if os.path.exists(GAMEDEF_PATH):
            try:
                con = sqlite3.connect(f'file:{GAMEDEF_PATH}?mode=ro', uri=True)
                try:
                    row = con.execute('SELECT Cash_T FROM items WHERE idx = ?', (item_id,)).fetchone()
                finally:
                    con.close()
                value = None if row is None or row[0] is None else int(row[0])
            except sqlite3.Error:
                value = None
        _CASH_T[item_id] = value
        return value


def _cash_item_is_period(rec, rx):
    """gamedef items.Cash_T(item_id) == 2 (period item; unknown id = required)"""
    t = cash_duration_type(rec.get('item_id', 0))
    return None if t is None else t == 2


def _buff_has_ground_point(rec, rx):
    """0x0A31 <= item_or_skill_id <= 0x0A3B (target spawned, buff has a duration)"""
    # 0x3B: the spec asks the server to always append x,y for ids 0xA31..0xA3B
    # (the target is spawned and the buff has a duration when we send it).
    return 0x0A31 <= int(rec.get('item_or_skill_id', 0)) <= 0x0A3B


# key -> {exact grammar condition text: bool | fn(rec, Receiver) -> bool | None}
# Values are the receiver state the server commits to when it sends the packet
# (roadmap 1.2.3 table plus every other client-state gate in the spec). "entity
# exists" gates are True/"== null" gates False: send only to clients that have the
# uid spawned. A resolver returning None means "cannot decide" -> MissingAssume.
DEFAULT_ASSUME = {
    # --- presence / movement (world) ---
    '0x1B': {'no entity with uid in scene list': False},        # tails come from state_blob (derived)
    '0x2A': {'find_entity(mover_uid) == null': False,
             # peers only, never the mover; the victim of the action skips the position block.
             # Assumes the mover is not dead on the receiver (state 0x10) - pass assume if it is.
             'mover.state_904 != 0x10 && target_uid != local_player_uid': _field_is_not_receiver('target_uid')},
    '0x9E': {'find_entity(mover_uid) == null': False},
    '0x9F': {'find_entity(mover_uid) == null': False},
    '0x29': {'entity_lookup(uid) == null': False},
    '0x40': {'entity_lookup(uid) == null': False},
    '0x21': {'scene+0x970 == 0': False},                          # local player registered (after own 0x07)
    '0x7F': {'scene+0x970 == 0': False},
    # --- combat / buffs ---
    '0x3B': {'hii_record(item_or_skill_id) == null': False,
             'entity_by_uid(target_uid) != null && hii_record(item_or_skill_id).duration_ms_1C8 != 0 && '
             'item_or_skill_id >= 0xA31 && item_or_skill_id <= 0xA3B': _buff_has_ground_point},
    '0x3C': {'item_def(buff_item_id) == null': False},
    '0x43': {'item_def(buff_item_id) == null': False},
    '0x41': {'entity_by_uid(target_uid) == null': False},
    '0x42': {'entity_by_uid(target_uid) == null': False},
    '0x57': {'entity_with_uid_in_scene': True},
    '0x8E': {'!client_item_table_has(equip_item_id)': False},
    '0x8F': {'item_def(tool_item_id) == null': False},
    # --- character (login_character) ---
    '0x58': {'find_entity_by_uid(uid) != null': True},
    '0x74': {'entity_with_uid_exists': True},
    '0x76': {'entity_by_uid(target_uid) == NULL': False,
             'target_uid == local_player_uid': _field_is_receiver('target_uid')},   # 18 B owner / 12 B observer
    # --- party ---
    '0x4F': {'no UI window 0x75..0x78 exists with window+0x134 == 0': False},     # a free frame exists
    # --- messenger / social ---
    '0x60': {'friend_list contains entry named friend_name && entry.uid == friend_uid': True},   # always 23 B
    '0x61': {'client.messenger_room_id != 0': True},
    '0x62': {'messenger_room_id != 0': True},
    '0x96': {'entity(target_uid) exists in scene list && target_uid == local_uid': True},        # always 25 B
    # --- premium cash ---
    '0x6F': {'char_bag_slots_0x361 <= 45 && char_bag_slots_0x362 <= 45 && char_bag_slots_0x363 <= 45': True},
    '0x72': {'entity_by_uid(player_uid) == null': False,
             'player_uid != local_player_uid': _field_is_not_receiver('player_uid'),
             'item_def(item_id) == null': False,
             'item_def(item_id).duration_type == 2': _cash_item_is_period},
    # --- stalls ---
    '0x85': {'entity_by_uid(owner_uid) != NULL && entity.stall_open(+0x14F4) == 0': True},
    '0x87': {'local_own_stall_open_or_pending': False},
    '0x88': {'local_own_stall_open_or_pending': False},
    # --- pvp ---
    '0x20': {'client_in_battlefield_map': False},
    '0x35': {'entity_by_uid(uid) == null': False},
    '0x36': {'[[game_state+0x4D8]+0x70] != 1': False},            # receiver is on the arena map
    '0x37': {'entity_with_uid_exists_in_scene': True},
    '0x5B': {'find_entity_by_uid(uid) != null': True},
    # --- UDP room host (mode-1 maps only; UDP-S2C:0x04 is deliberately absent: never send it) ---
    **{f'UDP-S2C:0x{op:02X}': {'map_mode != 1': False, 'entity_lookup(uid) == null': False}
       for op in (0x06, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10)},
    **{f'UDP-S2C:0x{op:02X}': {'map_mode != 1': False, '(hdr >> 18) != map_code': False}
       for op in (0x07, 0x08, 0x09)},
}

# Grammars whose client-state gates are deliberately left without a default.
NO_DEFAULT_ASSUME = {
    '0x01': 'loop counters (slot_no / entries_read) are not packet fields: keep the hand-built '
            'version/server-list reply',
    'UDP-S2C:0x04': 'field-map loopback snapshot; the server must never send it (roadmap F11)',
}



def _rows_not_receiver(repeat_key, field):
    """Resolver for a per-record receiver gate inside a repeat (one assume value covers every
    row, so the rows must agree): True when no row is the receiver, False when every row is,
    None (MissingAssume) for a mix - split the records per receiver instead."""
    def resolve(rec, rx):
        if rx.uid is None:
            return None
        verdicts = {int(row.get(field, 0)) != rx.uid for row in (rec.get(repeat_key) or [])}
        return verdicts.pop() if len(verdicts) == 1 else None
    resolve.__doc__ = f'every {repeat_key} {field} != receiver uid'
    return resolve


# 2009-only receiver-state defaults (client-2009-login), merged over the 2008 entries whose
# condition text survives in the 2009 grammar (assume_table). Same rule as DEFAULT_ASSUME:
# the value is the form the server commits to; send it only to receivers for whom it holds.
# client-2009-world adds the world packets whose 2008 condition text changed (scene+0x970 ->
# scene+0x988, game_state+0x4D8 -> +0x4FC, bag bytes 0x361.. -> 0x373..) and the new gates.
DEFAULT_ASSUME_2009 = {
    # spec_2009 0x02: gs+0x408 (relogin / change-channel pending) makes the client read only
    # the result byte and send C2S 0x452133/0x2B by itself; the rest is ignored. The server
    # always sends the full list, which is also what a client without the flag needs.
    '0x02': {'client_relogin_pending_gs_408': False},
    # spec_2009 0x07 / 0x04: the pet block (bool has_pet [+ u8 + str[13]]) is read only for
    # records whose uid != the RECEIVER's login uid (scene+0x224): the own record has none.
    # Rows of one packet must agree (records.player_list per receiver), else MissingAssume.
    '0x07': {'uid != local_player_uid': _rows_not_receiver('repeat[player_count]', 'uid')},
    '0x04': {'uid != local_player_uid': _rows_not_receiver('repeat[player_count]', 'uid')},
    # spec_2009 0x2B / 0x2E arena rosters: the same per-receiver pet block (no builder yet;
    # pvp rooms own them), so a future builder cannot get the receiver's own row wrong.
    '0x2B': {'uid != receiver_login_uid': _rows_not_receiver('repeat[player_count]', 'uid')},
    '0x2E': {'uid != receiver_login_uid': _rows_not_receiver('repeat[player_count]', 'uid')},
    # spec_2009 0x21 ExpDelta: dropped by a client with no local player (scene+0x988, 2008
    # scene+0x970) - sent only after the own 0x07. The trailing u32 guild_points exists only
    # when exp_delta > 0 AND the receiver's own entity+0x12 guild id is >= 2. There are no
    # guilds (GameServer._receiver_guild_id is 0), so the default is False; a guild-aware
    # sender passes {'local_player_guild_id > 1': guild_id >= 2} explicitly.
    '0x21': {'scene+0x988 == 0': False, 'local_player_guild_id > 1': False},
    # spec_2009 0x7F (mentor exp share): same local-player gate as 0x21, no guild tail.
    '0x7F': {'scene+0x988 == 0': False},
    # spec_2009 0x36 room result: the receiver is on the room map (2008 [[gs+0x4D8]+0x70]) and
    # has a local player for the dungeon hp/mp tail.
    '0x36': {'[[game_state+0x4FC]+0x7C] != 1': False, 'scene+0x988 == 0': False},
    # spec_2009 0x6F: the 2009 text is the negation of the 2008 one ("> 45" -> stop): the bag
    # capacities the server sends in 0x03 are always 1..45 (inventory.MAX_CAPACITY).
    '0x6F': {'char_bag_slots_0x373 > 45 || char_bag_slots_0x374 > 45 || char_bag_slots_0x375 > 45': False},
    # spec_2009 0x0A: NEW client blacklist gate (FUN_00484190 by sender name). No server-side
    # blacklist model exists, so the whisper is sent in full; a receiver that blacklisted
    # the sender reads only the header and drops it (same bytes to it, nothing shown).
    '0x0A': {'sender_on_local_blacklist': False},
}
BUILD_ASSUME = {'2009': DEFAULT_ASSUME_2009}

# 2009 grammars whose client-state gates are deliberately left without a default: the pet
# packets need a pet model the server does not have, so it never sends them.
NO_DEFAULT_ASSUME_2009 = {
    **{key: 'pet packet (S2C 0xAB..0xC0): no pet model, never sent (client-2009-world)'
       for key in ('0xAB', '0xAD', '0xAE', '0xAF', '0xB0', '0xB1', '0xC0')},
    '0x01': NO_DEFAULT_ASSUME['0x01'],
}

# S2C opcodes a build must never receive (spec gates_and_hazards). packets.send() refuses
# them and GameServer._send_encrypted refuses the raw opcode too (admin injection).
FORBIDDEN_S2C = {
    '2009': {
        # spec_2009 0xC5: X-Trap challenge; the patched client's stubbed X-Trap answers with
        # garbage (C2S 0x9E) and the session is lost.
        0xC5: 'X-Trap challenge: the stubbed client answers garbage and disconnects',
    },
}


# Builds whose S2C build() refuses a field its grammar never reads (unknown_fields). Off by
# default (a production server never pays for the walk); the 2009 test suites switch it on so
# every packet a 2009 flow sends is audited against the 2009 names (client-2009-world).
STRICT_FIELDS = set()


def forbidden_reason(opcode, client_build=None):
    """Why S2C `opcode` must never go to a client of this build, or None."""
    return FORBIDDEN_S2C.get(_build_name(client_build), {}).get(int(opcode))


def unknown_fields(key, fields, direction='S2C', *, client_build=None):
    """Field names in `fields` that the grammar of `key` never reads, recursively through
    its repeat blocks ('repeat[<expr>]' rows), as dotted paths. wsproto encodes a missing
    field as 0, so a record built with another build's names (0x1A unk_8cf vs the 2009
    unk_95b) silently sends zeros - this is the audit that catches it (client-2009-world).
    '__assume__' and packet-derived helpers (_DERIVED_U32) are not reported."""
    s = spec(key, direction, client_build=client_build)
    g = _db(client_build).grammar(s['key'])
    derived = set(_DERIVED_U32.get(s['key'], ()))
    bad = []

    def names(nodes, into):
        for n in nodes:
            if n[0] in ('scalar', 'str', 'bytes'):
                into.setdefault(n[2], None)
            elif n[0] == 'repeat':
                # two blocks with the same count share one key (0x03: repeat(5) ids, then
                # repeat(5) progress bytes -> one row carries both fields)
                k = f'repeat[{n[1]}]'
                into[k] = names(n[2], dict(into.get(k) or {}))
            elif n[0] == 'if':
                names(n[2], into)
                names(n[3], into)
        return into

    def walk(level, known, path):
        for k, v in level.items():
            if k == '__assume__' or k in derived:
                continue
            if k not in known:
                bad.append(path + k)
            elif known[k] is not None and isinstance(v, (list, tuple)):
                for i, row in enumerate(v):
                    if isinstance(row, dict):
                        walk(row, known[k], f'{path}{k}[{i}].')
    walk(dict(fields or {}), names(g.nodes, {}), '')
    return bad


def assume_table(client_build=None):
    """{S2C/UDP key: {condition: value | resolver}} of a build. 2008 is DEFAULT_ASSUME; a
    later build keeps each 2008 entry whose exact condition text is still a client-state
    condition of its own grammar (a changed text - 0x21's scene+0x970 is scene+0x988 in
    2009 - is dropped, so build() raises MissingAssume instead of guessing) and adds
    BUILD_ASSUME[build]."""
    db = _db(client_build)
    if db._assume is None:
        if db.client_build == DEFAULT_BUILD:
            table = DEFAULT_ASSUME
        else:
            table = {}
            for key, entries in DEFAULT_ASSUME.items():
                if key not in db.by_key:
                    continue
                exprs = set(db.grammar(key).client_state_exprs())
                kept = {e: v for e, v in entries.items() if e in exprs}
                if kept:
                    table[key] = kept
            for key, entries in BUILD_ASSUME.get(db.client_build, {}).items():
                table[key] = {**table.get(key, {}), **entries}
        db._assume = table
    return db._assume


# Packet-derived helper names used by grammar conditions (not client state).
_DERIVED_U32 = {
    '0x1B': ('state_blob', 'state_blob_u32_0', 'state_blob_u32_1'),
    '0x2A': ('move_bits', 'move_bits_lo', 'move_bits_hi'),
}


def _derive(skey, rec):
    entry = _DERIVED_U32.get(skey)
    if not entry:
        return
    blob_f, lo_f, hi_f = entry
    if blob_f in rec:
        blob = to_bytes(rec[blob_f])[:8].ljust(8, b'\x00')
        lo, hi = struct.unpack('<II', blob)
        for f, v in ((lo_f, lo), (hi_f, hi)):
            if f in rec and int(rec[f]) & 0xFFFFFFFF != v:
                raise PacketError(f'{skey}: {f}=0x{int(rec[f]):X} disagrees with {blob_f} (0x{v:X})')
        rec[blob_f], rec[lo_f], rec[hi_f] = blob, lo, hi
    else:
        lo = int(rec.get(lo_f, 0)) & 0xFFFFFFFF
        hi = int(rec.get(hi_f, 0)) & 0xFFFFFFFF
        rec[blob_f], rec[lo_f], rec[hi_f] = struct.pack('<II', lo, hi), lo, hi


def assume_defaults(key, direction='S2C', *, client_build=None):
    """The DEFAULT_ASSUME entry for a key as {condition: value or resolver description}."""
    table = assume_table(client_build).get(spec(key, direction, client_build=client_build)['key'], {})
    return {e: (v if not callable(v) else f'<{_resolver_text(v)}>') for e, v in table.items()}


def _resolver_text(fn):
    doc = (fn.__doc__ or '').strip()
    return doc.splitlines()[0] if doc else fn.__name__


def _merged_assume(skey, rec, assume, receiver, client_build=None):
    explicit = {**(rec.pop('__assume__', None) or {}), **(assume or {})}
    merged = {}
    for expr, value in assume_table(client_build).get(skey, {}).items():
        if expr in explicit:
            continue
        if callable(value):
            value = value(rec, receiver)
            if value is None:
                continue
        merged[expr] = bool(value)
    merged.update({e: bool(v) for e, v in explicit.items()})
    return merged


# ------------------------------------------------------------------- build ---
def build(key, fields=None, assume=None, *, receiver_uid=None, allow_unassumed=False,
          unsafe_text=False, unsafe_counts=False, direction='S2C', client_build=None):
    """Payload bytes (opcode excluded) for `key` built from its grammar.

    fields: grammar field values; nested repeats use "repeat[<expr>]": [{...}].
            '__assume__' inside fields is honoured (wsdev sendspec JSON).
    assume: {condition text: bool} for this receiver; overrides DEFAULT_ASSUME.
    receiver_uid: uid of the receiving client, for receiver-dependent forms.
    allow_unassumed: encode unstated byte-changing conditions as False instead of raising.
    unsafe_text: skip the client buffer clamps (only for deliberate overflow tests).
    unsafe_counts: skip the LIST_CAPS option-count refusal (same).
    direction: 'S2C' (default), 'C2S', 'UDP-S2C' or 'UDP-C2S'; a full key must match it.
    client_build: '2008' (None, the default) or '2009': whose spec the key resolves in.
    """
    s = spec(key, direction, client_build=client_build)
    skey = s['key']
    g = _db(client_build).grammar(skey)
    if _build_name(client_build) in STRICT_FIELDS and direction == 'S2C':
        bad = unknown_fields(skey, fields, direction, client_build=client_build)
        if bad:
            raise PacketError(f'{skey} ({_build_name(client_build)}): fields the grammar never reads: {bad} '
                              f'(another build\'s names? wsproto would send 0 for the real ones)')
    rec = dict(fields or {})
    rx = Receiver(receiver_uid)
    merged = _merged_assume(skey, rec, assume, rx, client_build)
    _derive(skey, rec)
    _apply_text_rules(skey, rec, unsafe_text, client_build)
    _apply_field_rules(skey, g, rec, unsafe_counts)
    unresolved = []
    raw = g.encode(rec, assume=merged, unresolved=unresolved)
    if unresolved and not allow_unassumed:
        missing = [e for e in unresolved if g.encode(rec, assume={**merged, e: True}) != raw]
        if missing:
            raise MissingAssume(skey, missing)
    return raw


def opcode(key, direction='S2C', *, client_build=None):
    return int(spec(key, direction, client_build=client_build)['opcode'], 16)


# ------------------------------------------------------------------- parse ---
class Record(dict):
    """Decoded fields plus which spec variant matched."""
    key = None
    name = None
    candidates = ()
    assume = None


_MAX_ASSUME_TRIES = 64


def _derived_env(skey, partial):
    entry = _DERIVED_U32.get(skey)
    if not entry or entry[0] not in partial:
        return None
    blob = bytes(partial[entry[0]])[:8].ljust(8, b'\x00')
    lo, hi = struct.unpack('<II', blob)
    return {**partial, entry[1]: lo, entry[2]: hi}


def _decode_variant(s, payload, assume, allow_trailing, client_build=None):
    """Decode one spec exactly. Client-state conditions start False; if that does not
    consume the payload exactly, conditions are flipped True breadth-first (fewest
    flips wins). Conditions over packet-derived helpers (0x1B state_blob_u32_0) are
    evaluated from the bytes instead of guessed."""
    skey = s['key']
    g = _db(client_build).grammar(skey)
    base = dict(assume or {})
    queue, seen, errors = [base], set(), []
    while queue and len(seen) < _MAX_ASSUME_TRIES:
        a = queue.pop(0)
        sig = tuple(sorted(a.items()))
        if sig in seen:
            continue
        seen.add(sig)
        unresolved = []
        try:
            rec = g.decode(payload, assume=a, allow_trailing=allow_trailing, unresolved=unresolved)
            return rec, {e: v for e, v in a.items() if base.get(e) != v}
        except (GrammarError, ClientStateCondition, ValueError, TypeError, struct.error) as e:
            errors.append(str(e))
        new = [e for e in unresolved if e not in a]
        if not new:
            continue
        try:
            partial = g.decode(payload, assume=a, allow_trailing=True)
        except Exception:                                     # noqa: BLE001 - partial read is best effort
            partial = None
        env = _derived_env(skey, partial) if partial is not None else None
        if env is not None:
            fixed = {}
            for e in new:
                try:
                    fixed[e] = bool(_eval(e, env, {}))
                except ClientStateCondition:
                    pass
            if fixed:
                queue.insert(0, {**a, **fixed})
                new = [e for e in new if e not in fixed]
        for e in new:
            queue.append({**a, e: True})
    raise ParseError(f'{skey}: ' + ('; '.join(dict.fromkeys(errors)) or 'no exact decode'))


def parse(key, payload, assume=None, *, direction='C2S', allow_trailing=False, client_build=None):
    """Decode a payload (opcode excluded). An opcode tries every send-site variant of
    that opcode and returns the first exact decode; rec.candidates lists every key
    that also decoded exactly (same-length send sites cannot be told apart by bytes).
    client_build picks the spec ('2008' by default, '2009')."""
    payload = bytes(payload)
    matches, errors = [], []
    for s in variants(key, direction, client_build=client_build):
        try:
            rec, used = _decode_variant(s, payload, assume, allow_trailing, client_build)
        except ParseError as e:
            errors.append(str(e))
            continue
        matches.append((s, rec, used))
    if not matches:
        raise ParseError(f'{len(payload)}B {payload[:32].hex(" ")} matches no {direction} spec for '
                         f'{key!r}: ' + ' | '.join(errors))
    s, rec, used = matches[0]
    out = Record(rec)
    out.key, out.name = s['key'], s.get('name')
    out.candidates = tuple(m[0]['key'] for m in matches)
    out.assume = used or None
    return out


# -------------------------------------------------------------------- send ---
def session_uid(session):
    """The receiver's uid as the client knows it (scene+0x220 = S2C 0x02 account_id)."""
    uid = session.get('uid')
    if uid is None:
        uid = session.get('account_id') or None
    return uid


def send(server, sock, session, key, fields=None, assume=None, **kw):
    """Build S2C `key` for this receiving session and send it through
    GameServer._send_encrypted (which owns the per-session send_lock and seq).
    The encoding mode follows the receiver's last C2S (session['no_enc']); every
    live client packet so far used EncodebyArray, so that is the default.
    Only TCP S2C keys resolve here (spec() raises KeyError for UDP / C2S keys).
    The spec is the server's client build (server.client_build) unless client_build is
    passed. flush=False only queues the packet on the receiver's outbox even on its own
    connection thread (GameServer._send_encrypted): a caller holding a shared lock (the
    messenger's, P6) must never block on a socket write."""
    flush = kw.pop('flush', None)
    kw.setdefault('client_build', getattr(server, 'client_build', None))
    s = spec(key, 'S2C', client_build=kw['client_build'])
    why = forbidden_reason(int(s['opcode'], 16), kw['client_build'])
    if why:
        raise PacketError(f'S2C {s["key"]} must never be sent to a {kw["client_build"]} client: {why}')
    if kw.pop('direction', 'S2C') != 'S2C':
        # direction='UDP-S2C' with an opcode key would build the UDP body of that opcode
        raise PacketError('send() writes the TCP game socket: direction must be S2C')
    kw.setdefault('receiver_uid', session_uid(session))
    payload = build(s['key'], fields, assume, **kw)
    extra = {} if flush is None else {'flush': flush}
    server._send_encrypted(sock, session, int(s['opcode'], 16), payload,
                           use_by_array=session.get('no_enc', True), **extra)
    return payload


def check_assume_table(client_build=None):
    """Problems in DEFAULT_ASSUME (a condition text that no longer matches its grammar);
    for a later build, in its BUILD_ASSUME entries (the carried 2008 ones are filtered)."""
    problems = []
    db = _db(client_build)
    own = DEFAULT_ASSUME if db.client_build == DEFAULT_BUILD else BUILD_ASSUME.get(db.client_build, {})
    for key, table in own.items():
        if key not in db.by_key:
            problems.append(f'{key}: no such spec')
            continue
        exprs = db.grammar(key).client_state_exprs()
        for expr in table:
            if expr not in exprs:
                problems.append(f'{key}: {expr!r} is not a client-state condition of the grammar {exprs}')
    return problems


if __name__ == '__main__':
    import sys
    # python packets.py [--build 2009] <key> [fields json]
    argv = sys.argv[1:]
    cb = None
    if argv[:1] == ['--build'] and len(argv) > 1:
        cb, argv = argv[1], argv[2:]
    if not argv:
        print(__doc__)
        sys.exit(0)
    k = argv[0]
    d = next((p for p in ('UDP-S2C', 'UDP-C2S') if k.startswith(p + ':')), 'C2S' if '/' in k else 'S2C')
    s = spec(k, d, client_build=cb)
    print(f'{s["key"]} {s["name"]}\n{s["grammar"]}\n')
    print('client-state conditions:', client_state_exprs(k, d, client_build=cb))
    print('default assume:', assume_defaults(k, d, client_build=cb))
    if len(argv) > 1:
        body = build(k, json.loads(argv[1]), direction=d, client_build=cb)
        print(f'{len(body)}B {body.hex(" ")}')
