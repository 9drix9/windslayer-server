#!/usr/bin/env python3
"""
gm.py - GM identity, the C2S 0x06 sub-command table and the '!' dev-command registry
====================================================================================
chat_mail_gm.md 1.5 / F10 / F11 (roadmap P1: chat_mail_gm-gm-flag-manner, -gm-dispatch,
-dev-commands). Three things live here, away from GameServer:

    gm.SUBCOMMANDS[payload[0]]           # which grammar decodes this C2S 0x06 (1.5)
    gm.notice_text(payload)              # sub 0x01 text, after the client's "/not" cut
    gm.register('job', gm.DevCommand(..))# other groups add their own '!' commands (F11)
    gm.audit(path, actor, 'manner', ..)  # append-only server/gm_audit.log (F10.0 step 5)

Why a sub-command table and not packets.parse(0x06)
---------------------------------------------------
The six C2S 0x06 send sites share the opcode byte and several share a length, so
`packets.parse(0x06, ...)` can only offer candidates: a 22-byte notice decodes as a
`/manner` record just as exactly. The client picks its body from the sub-command it wrote
into byte 0, so the server must do the same (chat_mail_gm.md 3.5: "pick the grammar by
payload[0]") and decode with that one key.

The '!' dev commands
--------------------
A GM's C2S 0x03 chat line starting with '!' is a server command, never broadcast (F1 step
2.3, F11). The table maps the word after '!' to a GameServer method, exactly as
registry.ROUTES maps an opcode to a handler, so the router stays one lookup and other
groups can add commands (login_character's `!job`, premium_cash's `!mall` / `!cash`)
without touching GameServer.DEV_COMMANDS:

    gm.register('mall', gm.DevCommand('_dev_mall', '!mall', 'open the cash shop',
                                      owner='premium_cash-...'))

Every handler has the signature `fn(self, session, args: str) -> None` and answers through
`GameServer._notice` (S2C 0x15). A non-GM '!' line stays ordinary chat.
"""
import logging
import os
import time
from dataclasses import dataclass, field

log = logging.getLogger('WS')

# The one gm_level the client's GM parser accepts (handler gate 0x44F093) and what the
# 0x07 record carries for a GM (chat_mail_gm.md 1.5; records.player_record).
GM_LEVEL_ON = 1
# S2C 0x15 announce text budget: the client buffer holds 88 bytes including the
# "[Announce] " prefix (chat_mail_gm.md 3.5 table).
ANNOUNCE_TEXT_MAX = 77
# Manner thresholds the client applies to the value in 0x02/0x07 (live verify 0x97):
# red name tag at <= -10, whisper blocked at <= -20, chat blocked at <= -40.
MANNER_CHAT_LIMIT = -40
MANNER_WHISPER_LIMIT = -20
# F10 sub 0x0C: the client closes about 5 s after S2C 0x5D, so the socket is dropped a
# second later rather than under the client's feet.
KICK_CLOSE_SECS = 6.0
# S2C 0x5D reason of a GM / admin kick, per client build (P6 stage 4, chat_mail_gm-gm-stop-kick):
# 2008 has no admin text, so reason 0 = "Connection to Server got disconnected" (any value
# but 1/2/3/99, live quest_cards_misc#17); the 2009 handler FUN_00451800 added reason 4 =
# "The game client will be shut down as requested by administrator." (spec_2009 0x5D).
KICK_REASON = {'2008': 0, '2009': 4}
# quest_cards_misc-admin-kick-maintenance: the admin port's kick saves first and closes the
# socket after 1 s. The 0x5D already set the client's "disconnect handled" flag (+0x1528 /
# 2009 gs+0x16EC), so the close shows no second dialog and the client's own 5 s timer still
# exits it with the kick dialog on screen.
ADMIN_KICK_CLOSE_SECS = 1.0
# F10 sub 0x07 /stop N and the admin shutdown: S2C 0x3A makes every client print "[Announcement]
# Server will be down in 3 minutes for the maintenance." and close itself ~170 s later (C26,
# live chat_mail_gm#20 / quest_cards_misc#19: not 180 + 5 s). The server saves every world
# state before that exit and drops whatever is still connected afterwards; the maintenance
# login lock (S2C 0x02 result 0x0E, version_code 0xEA61) stays on until an admin lifts it.
MAINTENANCE_NOTICE = 'Server maintenance in 3 minutes.'
MAINTENANCE_SAVE_SECS = 160.0
MAINTENANCE_CLOSE_SECS = 180.0
# S2C 0x17 (SuppressDisconnectNotice) is a PERMANENT client latch (live quest_cards_misc#18: after
# it a 0x5D shows nothing and the client never exits; only a restart clears it). It is kept
# as a spec builder for a future server-transfer flow and must never precede a kick.
TRANSFER_LATCH_KEY = '0x17'
# F10 sub 0x08 /go: the GM lands this far to the side of the target, GO_RISE_PX above the floor
# there (a local 0x07 falls onto the floor under it, like a portal arrival).
GO_OFFSET_PX = 40.0
GO_RISE_PX = 30.0


def kick_reason(client_build=None):
    """The S2C 0x5D reason a GM / admin kick sends to a client of `client_build`."""
    return KICK_REASON.get(str(client_build or '2008'), 0)
AUDIT_FILENAME = 'gm_audit.log'
# Client text buffers of the packets the '!' test aids fill (chat_mail_gm.md 3.5 table):
# S2C 0x78 memo text and S2C 0x6D gift message.
MEMO_TEXT_MAX = 92
GIFT_MESSAGE_MAX = 90


class DevCommandError(ValueError):
    """A '!' command was called with arguments it cannot use. The router turns it into one
    S2C 0x15 warning line plus the command's usage, so a GM never types into silence."""


# ------------------------------------------------------- C2S 0x06 sub-commands ---
@dataclass(frozen=True)
class SubCommand:
    """One C2S 0x06 sub-command: what the GM typed, the send-site spec key whose grammar
    decodes its body, and the roadmap item that implements the server side.

    allow_trailing: the body is longer than the grammar consumes. Only /not does that: its
    u8 length byte wraps for a 260+ byte line, so the text is read from the payload and the
    decode must not refuse the packet over the leftover bytes (F10 sub 0x01)."""
    name: str
    key: str
    typed: str
    owner: str
    allow_trailing: bool = False


# payload[0] -> the send site (chat_mail_gm.md 1.5 table). 0x00/0x02/0x07/0x0C share the
# "simple command" grammar, which reads one argument byte only for 0x07 and 0x0C.
SUBCOMMANDS = {
    0x00: SubCommand('update', '0x444C91/0x06', '/업데이트', 'chat_mail_gm-gm-dispatch'),
    0x01: SubCommand('notice', '0x444CFD/0x06', '/not...', 'chat_mail_gm-gm-dispatch',
                     allow_trailing=True),
    0x02: SubCommand('shadow', '0x444C91/0x06', '/shadow', 'chat_mail_gm-gm-shadow-reset-proom'),
    0x03: SubCommand('reset', '0x444F22/0x06', '/reset <tok> <tok>', 'chat_mail_gm-gm-shadow-reset-proom'),
    0x07: SubCommand('stop', '0x444C91/0x06', '/stop N', 'chat_mail_gm-gm-stop-kick'),
    0x08: SubCommand('go', '0x444FC6/0x06', '/go <name>', 'chat_mail_gm-gm-go'),
    0x0A: SubCommand('manner', '0x444C52/0x06', '/manner <name> <n>', 'chat_mail_gm-gm-manner'),
    0x0B: SubCommand('proom', '0x444DE2/0x06', '/proom <room> <pts>', 'chat_mail_gm-gm-shadow-reset-proom'),
    0x0C: SubCommand('kick', '0x444C91/0x06', '/kick N', 'chat_mail_gm-gm-stop-kick'),
}

# EN 2009 (client-2009-world): the same sub bytes at the 2009 send sites (spec_2009 C2S
# 0x06 keys; the simple-command site 0x44780D serves 0x00/0x02/0x07/0x0C again), /reset
# without its trailing str[3] (0x44670C, 130 B) and three new commands: /mony (5) and
# /expexp (6) at 0x446819 {u8, i32 amount}, /additem (9) at 0x44691E {u8, u16 id, u16 n}.
# /mony and /expexp have no server handler yet: _handle_gm audits them and answers "not
# implemented" like any other sub-command without a _gm_sub_<name> method. /additem is
# GameServer._gm_sub_additem (P4 stage 4): an S2C 0x99 sub 9 grant that leaves gold alone.
SUBCOMMANDS_2009 = {
    0x00: SubCommand('update', '0x44780D/0x06', '/업데이트', 'chat_mail_gm-gm-dispatch'),
    0x01: SubCommand('notice', '0x446548/0x06', '/not...', 'chat_mail_gm-gm-dispatch',
                     allow_trailing=True),
    0x02: SubCommand('shadow', '0x44780D/0x06', '/shadow', 'chat_mail_gm-gm-shadow-reset-proom'),
    0x03: SubCommand('reset', '0x44670C/0x06', '/reset <target>', 'chat_mail_gm-gm-shadow-reset-proom'),
    0x05: SubCommand('mony', '0x446819/0x06', '/mony <n>', 'chat_mail_gm (2009 GM gold grant)'),
    0x06: SubCommand('expexp', '0x446819/0x06', '/expexp <n>', 'chat_mail_gm (2009 GM exp grant)'),
    0x07: SubCommand('stop', '0x44780D/0x06', '/stop N', 'chat_mail_gm-gm-stop-kick'),
    0x08: SubCommand('go', '0x4467B6/0x06', '/go <name>', 'chat_mail_gm-gm-go'),
    0x09: SubCommand('additem', '0x44691E/0x06', '/additem <id> <n>', 'quest_cards_misc-system-notice-0x99'),
    0x0A: SubCommand('manner', '0x4464B2/0x06', '/manner <name> <n>', 'chat_mail_gm-gm-manner'),
    0x0B: SubCommand('proom', '0x44662C/0x06', '/proom <room> <pts>', 'chat_mail_gm-gm-shadow-reset-proom'),
    0x0C: SubCommand('kick', '0x44780D/0x06', '/kick N', 'chat_mail_gm-gm-stop-kick'),
}


def subcommands(client_build=None):
    """payload[0] -> SubCommand for a client build ('2008' default, '2009')."""
    return SUBCOMMANDS_2009 if str(client_build) == '2009' else SUBCOMMANDS


def notice_text(payload):
    """The /not text of a C2S 0x06 sub 0x01, as the client sent it.

    The client writes `u8 (len - 4), bytes typed_line[4:]`, so `/notice hi` arrives as
    "ice hi" and `/not hi` as " hi" (chat_mail_gm.md 1.5). The length byte wraps for a line
    of 260+ bytes, so the text is taken from the payload itself (F10 sub 0x01) and the
    leading "ice " of a typed `/notice` is removed.
    """
    text = bytes(payload)[2:]
    if text.startswith(b'ice '):
        text = text[4:]
    return text.strip(b' ')


# ---------------------------------------------------------- '!' dev commands ---
@dataclass(frozen=True)
class DevCommand:
    """One '!' command (F11).

    handler: GameServer method name, called as fn(session, args) with the raw argument
             text (everything after the command word, already stripped).
    usage:   one line shown by `!help` and on a bad argument list.
    help:    what it does, for `!help`.
    owner:   the roadmap item that owns the command.
    aliases: extra names that reach the same handler.
    """
    handler: str
    usage: str
    help: str = ''
    owner: str = 'chat_mail_gm-dev-commands'
    aliases: tuple = field(default_factory=tuple)


# Commands registered by other groups (F11: "exposed for other groups"). GameServer looks
# in its own DEV_COMMANDS table first, so a group can add `!job` / `!mall` / `!cash` from
# its own module without editing the class.
COMMANDS = {}


def names_of(command, name=''):
    """Every word that reaches `command`: its table name plus its aliases, lower case."""
    return tuple(str(n).lower() for n in ((name,) if name else ()) + tuple(command.aliases))


def register(name, command):
    """Add a '!' command to the shared registry. A name or alias another command already
    answers to is refused, so two groups cannot silently claim the same word."""
    taken = {word: cmd for key, cmd in COMMANDS.items() for word in names_of(cmd, key)}
    for word in names_of(command, name):
        if word in taken:
            raise ValueError(f'dev command {word!r} is already registered by {taken[word].owner}')
    COMMANDS[str(name).lower()] = command
    return command


def unregister(name):
    """Drop a registered command (tests, hot reload)."""
    return COMMANDS.pop(str(name).lower(), None)


def lookup(name, *tables):
    """The DevCommand `name` runs, searched by table name first and then by alias, in the
    order the tables are given (GameServer.DEV_COMMANDS before gm.COMMANDS). None when no
    command answers to that word."""
    name = str(name).lower()
    for table in tables:
        cmd = table.get(name)
        if cmd is not None:
            return cmd
    for table in tables:
        for key, cmd in table.items():
            if name in names_of(cmd, key)[1:]:
                return cmd
    return None


def split(text):
    """('who', '') for b'!who', ('manner', 'test -50') for b'!manner test -50'.

    `text` is the raw chat line (cp949 bytes on the wire). The command word and its
    arguments are decoded for parsing only; names are re-checked by the store, which is
    ASCII-only (names.py).
    """
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode('cp949', 'replace')
    text = text.strip()
    if text.startswith('!'):
        text = text[1:]
    word, _, args = text.partition(' ')
    return word.lower(), args.strip()


def args(text, count, usage):
    """The first `count - 1` whitespace-separated tokens plus the rest as one string
    (`!mail Nova hello there` -> ('Nova', 'hello there')). Raises DevCommandError with the
    usage line when a token is missing."""
    parts = str(text).split(None, count - 1) if count > 1 else [str(text)]
    if len(parts) < count or not all(p.strip() for p in parts):
        raise DevCommandError(f'needs {count} argument(s)')
    return tuple(p.strip() for p in parts)


def parse_int(text, what='value', lo=None, hi=None):
    try:
        value = int(str(text).strip(), 0)
    except ValueError:
        raise DevCommandError(f'{what}: {str(text).strip()!r} is not a number') from None
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise DevCommandError(f'{what}: {value} is outside {lo}..{hi}')
    return value


def parse_float(text, what='value'):
    try:
        return float(str(text).strip())
    except ValueError:
        raise DevCommandError(f'{what}: {str(text).strip()!r} is not a number') from None


def wrap_list(items, prefix='', limit=80):
    """`prefix` + the items as space-separated lines that fit one S2C 0x15 (88 bytes with
    its "[Warning] "-style prefix; 80 keeps a margin)."""
    lines, line = [], prefix
    for item in items:
        candidate = f'{line}{item} '
        if len(candidate) > limit and line.strip():
            lines.append(line.rstrip())
            line = f'{item} '
        else:
            line = candidate
    if line.strip():
        lines.append(line.rstrip())
    return lines or [prefix.rstrip()]


def check_commands(table, cls, *others):
    """Problems in a dev-command table: handler names `cls` does not have and words two
    commands answer to (mirrors registry.check_routes, so a typo is caught by a test and
    not by a GM). `others` are the tables it shares the '!' namespace with."""
    problems = []
    seen = {}
    for source in (table,) + tuple(others):
        for name, cmd in source.items():
            if name != name.lower() or not name:
                problems.append(f'{name!r}: command names are lower case and non-empty')
            if source is table and not callable(getattr(cls, cmd.handler, None)):
                problems.append(f'!{name}: {cls.__name__} has no handler {cmd.handler!r}')
            for word in names_of(cmd, name):
                if word in seen and seen[word] is not cmd:
                    problems.append(f'!{word}: claimed by {seen[word].owner} and {cmd.owner}')
                seen[word] = cmd
    return problems


# ----------------------------------------------------------------- audit log ---
def audit_path(accounts_path):
    """server/gm_audit.log next to the accounts file (F10.0 step 5). Tests run against a
    temp accounts.json, so their audit lines never touch the live log."""
    return os.path.join(os.path.dirname(os.path.abspath(accounts_path)), AUDIT_FILENAME)


def audit(path, actor, action, detail='', when=None):
    """Append one accepted GM action. Append-only and best effort: a GM command must not
    fail because the log file cannot be written."""
    stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(when if when is not None else time.time()))
    line = '\t'.join((stamp, str(actor), str(action), str(detail).replace('\n', ' '))) + '\n'
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line)
    except OSError as e:                                  # noqa: BLE001 - never break a command
        log.warning(f'[GM] cannot append to {path}: {e}')
    return line
