#!/usr/bin/env python3
"""
wsproto.py - executable WindSlayer packet grammars
===================================================
Turns the RE workflow's wire-grammar DSL into byte-exact encoders/decoders so a
spec can be tested against live captures and used by the server directly.

DSL (one statement per line, opcode byte excluded):
    u8|u16|u32|u64|i8|i16|i32|f32|f64|bool NAME
    str[N] NAME            exactly N bytes, NUL padded text (decoded as latin-1, so
                           every byte survives decode -> encode). A literal N (a fixed
                           char array such as str[17]) is always NUL-terminated on
                           encode; str[len_field] carries exactly len bytes
    bytes[N] NAME          exactly N raw bytes (N may be an expression)
    repeat(EXPR) { ... }   EXPR evaluated against fields decoded so far
    if(EXPR) { ... } else { ... }
    stop                   reading ends on this path
Expressions use C operators (&&, ||, !, >>, &, ==) over earlier field names.
A condition that cannot be evaluated from packet fields (it depends on client
state, e.g. "no entity with uid in scene list") is a CLIENT-STATE condition:
decode() takes it as false unless `assume` supplies a value; encode() uses the
same rule.

    g = Grammar(text)
    rec = g.decode(payload_bytes)          # -> dict, raises on under/overflow
    raw = g.encode(rec)                    # -> bytes

A Grammar is safe to share between threads (decode keeps its read cursor local).
Pass `unresolved=[]` to encode/decode to collect the client-state conditions
that had no `assume` value on that call (server/packets.py uses it to refuse
packets whose bytes depend on an unstated receiver state).
"""
import re
import struct

SCALARS = {
    'u8': '<B', 'u16': '<H', 'u32': '<I', 'u64': '<Q', 'i8': '<b', 'i16': '<h',
    'i32': '<i', 'i64': '<q', 'f32': '<f', 'f64': '<d', 'bool': '<B',
}


class GrammarError(Exception):
    pass


class ClientStateCondition(Exception):
    """Raised internally when an expression needs client state, not packet fields."""


# ------------------------------------------------------------------ parsing ---
_TOKEN = re.compile(r'\s*(\{|\}|[^{}\n]+)')


_STMT_START = re.compile(r'(?<![\w\[])((u8|u16|u32|u64|i8|i16|i32|i64|f32|f64|bool)(?!\w)|str\s*\[|bytes\s*\[|repeat\s*\(|if\s*\(|else\b|stop\b)')


def _split_multi(chunk):
    """'u16 a  repeat(6)' -> ['u16 a', 'repeat(6)']: split where a new statement
    keyword starts at parenthesis depth 0 (so expressions are never split)."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(chunk):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif depth == 0 and i > start and chunk[i - 1].isspace() and _STMT_START.match(chunk, i):
            parts.append(chunk[start:i].strip())
            start = i
    parts.append(chunk[start:].strip())
    return [p for p in parts if p]


def _statements(text):
    """Split into statements; braces become their own tokens."""
    out = []
    for raw in text.replace('\r', '').split('\n'):
        line = raw.split('//', 1)[0].strip()
        if not line:
            continue
        pos = 0
        while pos < len(line):
            m = _TOKEN.match(line, pos)
            if not m:
                break
            tok = m.group(1).strip()
            if tok in ('{', '}'):
                out.append(tok)
            elif tok:
                out.extend(_split_multi(tok))
            pos = m.end()
    return out


def _split_header(stmt, kw):
    """'repeat(expr)' / 'if(expr)' -> expr, handling nested parentheses."""
    s = stmt[len(kw):].strip()
    if not s.startswith('('):
        raise GrammarError(f'expected "(" after {kw}: {stmt!r}')
    depth = 0
    for i, ch in enumerate(s):
        depth += ch == '('
        depth -= ch == ')'
        if depth == 0:
            return s[1:i].strip(), s[i + 1:].strip()
    raise GrammarError(f'unbalanced parentheses: {stmt!r}')


def _expand(toks):
    """Normalize headers so every block opener is its own '{' token:
    'repeat(n) u16 x' / 'else if(c)' / 'else' splits are handled here."""
    out = []
    for t in toks:
        for kw in ('repeat', 'if'):
            if t.startswith(kw) and re.match(rf'^{kw}\s*\(', t):
                expr, rest = _split_header(t, kw)
                out.append(f'{kw}({expr})')
                if rest:
                    out.extend(_statements(rest))
                break
        else:
            if t.startswith('else') and t != 'else':
                out.append('else')
                out.extend(_expand([t[4:].strip()]))
            else:
                out.append(t)
    return out


def _parse_stmt(toks, i):
    t = toks[i]
    if t.startswith('repeat('):
        expr = t[len('repeat('):-1]
        if toks[i + 1] != '{':
            raise GrammarError(f'repeat without body: {t!r}')
        body, i = _parse_block(toks, i + 2)
        return ('repeat', expr, body), i
    if t.startswith('if('):
        expr = t[len('if('):-1]
        if toks[i + 1] != '{':
            raise GrammarError(f'if without body: {t!r}')
        then, i = _parse_block(toks, i + 2)
        other = []
        if i < len(toks) and toks[i] == 'else':
            if toks[i + 1] == '{':
                other, i = _parse_block(toks, i + 2)
            else:                                        # else if(...) chain
                node, i = _parse_stmt(toks, i + 1)
                other = [node]
        return ('if', expr, then, other), i
    if t == 'stop' or t.startswith('stop '):
        return ('stop',), i + 1
    m = re.match(r'^(u8|u16|u32|u64|i8|i16|i32|i64|f32|f64|bool)\s+(\w+)', t)
    if m:
        return ('scalar', m.group(1), m.group(2)), i + 1
    m = re.match(r'^(str|bytes)\s*\[\s*([^\]]+)\]\s+(\w+)', t)
    if m:
        return (m.group(1), m.group(2).strip(), m.group(3)), i + 1
    raise GrammarError(f'cannot parse statement: {t!r}')


def _parse_block(toks, i):
    nodes = []
    while i < len(toks):
        if toks[i] == '}':
            return nodes, i + 1
        if toks[i] == '{':
            raise GrammarError('unexpected "{"')
        node, i = _parse_stmt(toks, i)
        nodes.append(node)
    return nodes, i


# ------------------------------------------------------------- expressions ---
_ALLOWED = re.compile(r'^[\w\s\(\)\+\-\*/%<>=!&|\^~]+$')
_HEX = re.compile(r'(?<![\w])0[xX][0-9a-fA-F]+')
_CAST = re.compile(r'\(\s*(?:unsigned\s+|signed\s+)?(?:int|uint|short|ushort|char|uchar|byte|long|ulong|'
                   r'u8|u16|u32|u64|i8|i16|i32|i64|bool)\s*\)')


def _pyexpr(expr):
    """C expression -> Python: casts dropped, hex literals made decimal (so `0x0A31`
    is never mistaken for an identifier), && || ! mapped, `/` is integer division."""
    e = _CAST.sub('', expr)
    e = _HEX.sub(lambda m: str(int(m.group(0), 16)), e)
    e = e.replace('&&', ' and ').replace('||', ' or ')
    e = re.sub(r'!(?!=)', ' not ', e)
    e = re.sub(r'(?<!/)/(?!/)', '//', e)
    return e


def _eval(expr, env, assume):
    if expr in assume:
        return assume[expr]
    py = _pyexpr(expr)
    if re.fullmatch(r'\s*\d+\s*', py):
        return int(py)
    if not _ALLOWED.match(py):
        raise ClientStateCondition(expr)
    names = set(re.findall(r'[A-Za-z_]\w*', py)) - {'and', 'or', 'not'}
    if any(n not in env for n in names):
        raise ClientStateCondition(expr)
    try:
        return eval(py, {'__builtins__': {}}, dict(env))
    except Exception:
        raise ClientStateCondition(expr)


# ---------------------------------------------------------------- codec ---
class _Stop(Exception):
    pass


class _Cursor:
    """Per-call read position. Kept off the Grammar so one cached Grammar can
    decode on several server threads at once (packets.py shares them)."""
    __slots__ = ('data', 'off')

    def __init__(self, data):
        self.data = data
        self.off = 0

    def take(self, n):
        if self.off + n > len(self.data):
            raise GrammarError(f'underflow: need {n} byte(s) at offset {self.off}, '
                               f'have {len(self.data) - self.off}')
        b = self.data[self.off:self.off + n]
        self.off += n
        return b


def _note(unresolved, expr):
    if unresolved is not None and expr not in unresolved:
        unresolved.append(expr)


class Grammar:
    def __init__(self, text):
        self.text = text
        self.nodes, _ = _parse_block(_expand(_statements(text)) + ['}'], 0)
        self.client_state_conditions = set()

    # decode ---------------------------------------------------------------
    def decode(self, data, assume=None, allow_trailing=False, unresolved=None):
        """Decode `data` exactly. Client-state conditions without an `assume` value
        read as False; they are appended to `unresolved` (a list) when given."""
        assume = assume or {}
        rec, env = {}, {}
        cur = _Cursor(bytes(data))
        try:
            self._dec(cur, self.nodes, rec, env, assume, unresolved)
        except _Stop:
            pass
        if cur.off != len(cur.data) and not allow_trailing:
            raise GrammarError(f'{len(cur.data) - cur.off} trailing byte(s) after decode '
                               f'(consumed {cur.off} of {len(cur.data)})')
        return rec

    def _cond(self, expr, env, assume, unresolved=None):
        try:
            return bool(_eval(expr, env, assume))
        except ClientStateCondition:
            self.client_state_conditions.add(expr)
            _note(unresolved, expr)
            return False

    def _dec(self, cur, nodes, rec, env, assume, unresolved):
        for n in nodes:
            k = n[0]
            if k == 'scalar':
                fmt = SCALARS[n[1]]
                v = struct.unpack(fmt, cur.take(struct.calcsize(fmt)))[0]
                rec[n[2]] = v
                env[n[2]] = v
            elif k in ('str', 'bytes'):
                size = int(_eval(n[1], env, assume))
                raw = cur.take(size)
                v = raw.split(b'\x00', 1)[0].decode('latin-1') if k == 'str' else raw
                rec[n[2]] = v
                env[n[2]] = v
            elif k == 'repeat':
                count = int(_eval(n[1], env, assume))
                items = []
                for _ in range(count):
                    sub = {}
                    self._dec(cur, n[2], sub, dict(env), assume, unresolved)   # loop scope sees outer fields
                    items.append(sub)
                rec[f'repeat[{n[1]}]'] = items
            elif k == 'if':
                branch = n[2] if self._cond(n[1], env, assume, unresolved) else n[3]
                self._dec(cur, branch, rec, env, assume, unresolved)
                env.update({kk: vv for kk, vv in rec.items() if not kk.startswith('repeat[')})
            elif k == 'stop':
                raise _Stop()

    # encode ---------------------------------------------------------------
    def encode(self, rec, assume=None, unresolved=None):
        """Build a payload from `rec`. Client-state conditions (those that can't be
        evaluated from packet fields) take their value from `assume` or from
        rec['__assume__'] ({condition text: bool}); unspecified ones are False and
        are appended to `unresolved` (a list) when given. Server code should build
        through packets.build(), which refuses unstated conditions that change the
        bytes; list them here with client_state_exprs()."""
        assume = {**(rec.get('__assume__') or {}), **(assume or {})}
        out = bytearray()
        try:
            self._enc(self.nodes, rec, dict(rec), assume, out, unresolved)
        except _Stop:
            pass
        return bytes(out)

    def client_state_exprs(self):
        """Every if/repeat/size expression that references something other than
        packet fields - the caller must supply these via `assume` when encoding."""
        found, fields = [], set()

        def walk(nodes):
            for n in nodes:
                if n[0] in ('scalar', 'str', 'bytes'):
                    fields.add(n[2])
                    exprs = [n[1]] if n[0] != 'scalar' else []
                elif n[0] == 'repeat':
                    exprs = [n[1]]
                elif n[0] == 'if':
                    exprs = [n[1]]
                else:
                    exprs = []
                for e in exprs:
                    names = set(re.findall(r'[A-Za-z_]\w*', _pyexpr(e))) - {'and', 'or', 'not'}
                    if (names - fields or not _ALLOWED.match(_pyexpr(e))) and e not in found:
                        found.append(e)
                if n[0] == 'repeat':
                    walk(n[2])
                elif n[0] == 'if':
                    walk(n[2]); walk(n[3])
        walk(self.nodes)
        return found

    def _enc(self, nodes, rec, env, assume, out, unresolved=None):
        for n in nodes:
            k = n[0]
            if k == 'scalar':
                fmt = SCALARS[n[1]]
                if fmt[1] in 'fd':
                    # f32/f64 positions (C2S 0x0D, S2C 0x04/0x05/0x07/0x1A/0x1B/0x2A) keep
                    # their fraction: int() here wrote 123.75 as 123.0.
                    v = float(rec.get(n[2], 0.0))
                else:
                    v = int(rec.get(n[2], 0))
                    if fmt[1] in 'BHIQ':                     # unsigned: wrap like C
                        v &= (1 << (8 * struct.calcsize(fmt))) - 1
                out += struct.pack(fmt, v)
                env[n[2]] = v
            elif k in ('str', 'bytes'):
                size = int(_eval(n[1], env, assume))
                v = rec.get(n[2], b'' if k == 'bytes' else '')
                raw = v.encode('latin-1') if isinstance(v, str) else bytes(v)
                if k == 'str' and size > 0 and n[1].isdigit():
                    # Fixed char arrays (str[17] names, str[25] titles, str[93] memo
                    # text) are strcpy'd / drawn as C strings by the client, so keep a
                    # NUL inside them (roadmap S1-13). Length-prefixed str[len] text
                    # (0x01/0x15/0x16/0x90/0x91) is read into a pre-zeroed buffer with
                    # "no NUL needed on the wire": forcing one there would eat the last
                    # character, so those carry exactly `len` bytes and the length
                    # clamps in packets.py keep them inside the client buffer (S1-14).
                    raw = raw.split(b'\x00', 1)[0][:size - 1]
                out += raw[:size].ljust(size, b'\x00')
                env[n[2]] = v
            elif k == 'repeat':
                count = int(_eval(n[1], env, assume))
                items = rec.get(f'repeat[{n[1]}]', [])
                for idx in range(count):
                    sub = items[idx] if idx < len(items) else {}
                    self._enc(n[2], sub, {**env, **sub}, assume, out, unresolved)
            elif k == 'if':
                try:
                    take = bool(_eval(n[1], env, assume))
                except ClientStateCondition:
                    take = False
                    self.client_state_conditions.add(n[1])
                    _note(unresolved, n[1])
                self._enc(n[2] if take else n[3], rec, env, assume, out, unresolved)
            elif k == 'stop':
                raise _Stop()


def hexbytes(s):
    return bytes(int(x, 16) for x in s.split())


if __name__ == '__main__':
    import sys, json
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(0)
    g = Grammar(open(sys.argv[1], encoding='utf-8').read())
    rec = g.decode(hexbytes(' '.join(sys.argv[2:])))
    print(json.dumps(rec, indent=1, default=lambda b: b.hex(' ')))
