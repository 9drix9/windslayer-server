#!/usr/bin/env python3
"""
events.py - scheduled server events, both client builds (P13 stages 1-2)
=========================================================================
Roadmap items (re_tools/docs/ROADMAP_2009_ADDENDUM.md P13 + 3.0; design
re_tools/docs/systems_2009/events_bosses.md A3-A8, C, E1-E5):

    ev-e1               the UTC schedule (EVENTS_FILE) and "[Announcement]" notices: S2C 0x15
                        msg_type 2, <= 88 bytes, on world entry and every N minutes
    arch09-exp-pipeline the EVENT stage of the one exp path (GameServer.award_exp: card bonus
                        -> the P8 cash EXP item -> event multiplier -> S2C 0x21; the P14
                        guild tail is a hook only)
    ev-e2               the multiplier itself: after the card bonus and the EXP item, at least 1
    ev-e3               the once-only login gift, S2C 0x99 sub 9 (item Types 0/1/2), claimed
                        per character and event in the record (`event_gifts_claimed`)
    arch09-window-open  S2C 0x80 {1, id} as a window opener, separate from the password gate,
                        allowlist {0x3FB}
    ev-e4               the Event News popup `80 01 FB 03 00 00`, once per session and event,
                        after the C2S 0x63 reply
    ev-e5 (stage 2)     event quests: S2C 0x26 of `push_quests` on world entry, the Nicolas
                        turn-in C2S 0x17 -> 0x27 + 0x21 [+ 0x3F] (GameServer's quest loop),
                        then 0x26 of the hqi NextQuest (158 -> 159 -> 160); see "Event quests"

Why the server builds all of it from old opcodes
------------------------------------------------
Build 14 has no event system in its protocol (evb 0.1, VERIFIED): no opcode carries an event
flag, a multiplier or an event time, and the 2009 S2C 0x03 ends at the etc bag list (the
newer KR "Event time" u32 is not read). The EN 2008 client is the same. So an event is:
  - a text line: S2C 0x15 msg_type 2 whose text starts "[Ann" is drawn in 0xFF00E4FF (teal)
    and, in 2009 only, also as a centre-screen notice (0x456743; 2008 compares 6 characters,
    "[Annou", at 0x4519C9 - "[Announcement]" satisfies both);
  - a bigger 0x21 delta: the client adds whatever it gets and prints it (A6, 0x459063);
  - a bag grant: 0x99 sub 9 (A5 G1, 0x45D50F; 2008 0x457FE1), the only path for a Cash-0
    item like Love Potion 1282;
  - one reused window: 0x80 result 1 opens ANY id without a special handler through
    FUN_00497c70 (2009 0x4529BF..0x4529CA; 2008 FUN_00483430 at 0x44E368), exactly the call
    Eventina's own click makes (A2). Window 1019 = 0x3FB "Pop-Up (Event News)" exists in
    both installs' windslayer.hui (sprite 362, the close-beta art).

Once per login, not per map load
--------------------------------
The client sends C2S 0x63 at the end of EVERY S2C 0x03 (2008 0x44EF67, 2009 0x453557), so
GameServer._handle_card_deck_list calls `after_resync` there, after its 0x8A / 0x59 / 0x99
sub 8 (the arch09-resync-bundle slot for "optional Event News popup"). The entry effects of
an event run once per CONNECTION (session['events_entry']), so a portal, a warp, a revive or
a village transfer repeats nothing. A relogin is a new connection, so:
  - the gift is persisted (a claimed event never grants again);
  - the entry announcement and the popup are kept out for EVENT_RELOGIN_QUIET_SECS per
    character and event (server memory, `ledger`): a relog, a crash-reconnect and the 2009
    channel change (a disconnect plus a relogin, evb E3/E4) repeat nothing. P12's
    arch09-session-continuity owns real channel-hop detection; this window is the cheap
    stand-in the P13 exit criterion 2 needs ("a relog repeats none of them").
An event that starts while players are in the world (schedule or `!event start`) reaches
them on the next tick (TICK_SECS), but only after their latest map load's C2S 0x63
(session['events_ready']: cleared by a before_server_map_load hook), so no event packet ever
lands between a map load's 0x03 and the client's own resync. The readiness check and the
effect it allows run under the session's combat lock, which the hook also takes to clear the
flag: a map load that begins meanwhile either waits for the effect (its lead goes out after
it) or makes the tick see "not ready". A gift or quest push bumps session[GRANTS_KEY]
(grants()) under that lock, so GameServer._map_transfer - which builds its 0x03 before it
fires the hook - can tell that the bag / quest log changed under that 0x03 and rebuild it,
as it does for a trade commit (trade.commits).
A 0x63 re-arms only when it is the LATEST map load's: the hook counts the loads begun
(session[LOADS_KEY]) and after_resync the 0x63s seen (session[RESYNCS_KEY], never more than
the loads, so a stray 0x63 banks nothing), both under the combat lock. The 0x63 of an earlier
load handled after a transfer started on another thread already began the next one (its hook
cleared the flag) therefore leaves the flag cleared until that load's own 0x63. Every caller
moves its own session on its receive thread today, which serializes it with the 0x63s, so
this only makes the rule independent of that. Every hook fire is followed by its 0x03 (the hook runs after _map_transfer's resolve
and build, and nothing after it refuses the load), and every 0x03 by one 0x63; a load that
raised in between would leave the connection without events until a relog, and that
connection is broken anyway (in_world False, off its map). The mall entry (Events.suspend)
clears the flag without counting a load: it sends no 0x03, and the exit is a map load.

The schedule
------------
EVENTS_FILE (config.py; '' = no events) names a JSON file next to config.json:

    {"events": [{
      "id": "valentine-2009",                      # [A-Za-z0-9_.-]{1,32}, unique
      "enabled": true,                             # false: only `!event start` runs it
      "start": "2009-02-13T00:00:00Z",             # UTC (a time without an offset is UTC);
      "end":   "2009-02-16T00:00:00Z",             #   either may be null = open-ended
      "exp_mult": 2.0,                             # 0 < x <= 100; 1 = none
      "announce": {"text": "EXP x2 weekend!", "every_min": 30, "on_enter": true},
                                                   #   every_min 0 = entry only, else 1..1440
      "popup_event_news": true,
      "login_gift": [[1282, 5]],                   # [item, count]: EN item, Type 0/1/2
      "push_quests": [158],                        # ev-e5: chain heads (<= 5 quest ids)
      "cash_gift": []                              # ev-e6 (after P8)
    }]}

A malformed file refuses the server start (EventError is a config.ConfigError: nothing is
bound yet); `!event reload` keeps the running schedule when the new file is bad. Several
active events never stack their multipliers: the largest one applies (`!expmult` overrides).

Event quests (ev-e5)
--------------------
Nicolas (hni 76) is the ENPC of 27 hqi quests and the SNPC of none; 26 of them have SNPC 0,
and the 2009 offer list skips SNPC 0 (FUN_0048ac00), so only a server push starts one (evb A3,
VERIFIED). The push is the P2 accept without an NPC: S2C 0x26 {quest} (the client writes its
first empty slot, grants the Send items itself and shows the Intro) + 0x59 {slot, 0}, mirrored
into the character's quest store (quests.QuestState: active slot, and later the completed list
with its Repeat count) - that store IS the per-character persistence, no new record key:
  - on the connection's first C2S 0x63 of an active event (the stage-1 entry effects, after
    the gift, before the popup), each `push_quests` head is walked along NextQuest to its
    first quest not yet exhausted; that one is pushed unless it is already held. So a relog
    or a portal pushes nothing again (the quest is held, or done), and a chain interrupted
    by the end of an event, a full log or an abandon resumes at the next login;
  - the turn-in is the P2 quest loop (GameServer._handle_turn_in_quest): C2S 0x17 is sent by
    the client only when its own gates pass (FUN_0048a510: Demand items in the bag, gold,
    ReqPro) -> S2C 0x27 (the client pays Money, removes the Demand items, grants the Reward
    card, prints "[%s] Quest Completed.") + 0x21 Exp through award_exp (source 'quest') +
    0x3F gold. Then `after_turn_in` pushes the NextQuest while an event whose chain holds the
    quest is active. An event quest's refused 0x17 shows NOTHING (evb A8: "0x27, or nothing
    on refusal"; P13 exit criterion 4), unlike the P2 quests' [Warning] lines;
  - a pushed quest must exist in the build's hqi, have an ENPC (quest 63 has ENPC 0: no NPC
    can ever turn it in), and have plain-ASCII quest-log texts (EVENT_QUEST_KOREAN_TEXT):
    only the 2009 chain 158-160 passes by default. The 2008 hqi holds other quests under
    those ids (Korean Thanksgiving quests with ENPC 0), so a 2008 server pushes none of the
    shipped event's quests and says so at start;
  - the per-character gates of the P2 accept: not held, not exhausted (Repeat), level range,
    job flag, PrevQuest done, a free slot of the server's 3 (2009 slots 4 and 5 stay empty in
    the 0x03, so the client's "first empty of 5" is the same slot), Send items fit the bag.
C2S 0x16 for a quest of a running event's chain (open question Q4: what the Accept button of
quest 158's Finish window 1195 sends) is logged and handled as a push, so it is either a
no-op (already held / done) or the push itself - never the "SNPC 0" refusal of the P2 path.
"""
import datetime
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass

import config as cfgmod
import en_content as EC
import gm
import progression
import quests as questmod
import world as worldmod

log = logging.getLogger('WS')

# How often the event tick runs (GameServer.start): periodic announcements, events that
# begin while players are in the world, expired `!event start` windows.
TICK_SECS = 5.0

# ---- S2C 0x15 (evb A8 "S2C 0x15 (announcement)", spec 0x15 / spec_2009 0x15) ----
ANNOUNCE_PREFIX = '[Announcement] '
# The prefix the colour test needs: 2008 strncmp 6 "[Annou" (0x4519C9), 2009 strncmp 4
# "[Ann" (0x456743). A text starting with the 6 bytes is teal in both builds.
ANNOUNCE_TEST = b'[Annou'
ANNOUNCE_MSG_TYPE = 2
# The 89-byte pre-zeroed stack buffer: 88 text bytes leave its NUL (spec 0x15 hazard).
ANNOUNCE_MAX_BYTES = 88

# ---- S2C 0x80 as a window opener (arch09-window-open, evb A4/A8, RM 4.2, X2) ----
WINDOW_EVENT_NEWS = 0x3FB                 # hui 1019 "Pop-Up (Event News)", both installs
# The only ids this builder may open. The password gate keeps its own allowlist
# (GameServer.PASSWORD_WINDOWS = {0x1A7, 0x1F9, 0x235}); the two never share an id.
WINDOW_OPEN_ALLOWLIST = frozenset({WINDOW_EVENT_NEWS})
# Never through this builder, whatever the allowlist grows to: the password-gate ids and the
# 2009 special handlers of the 0x80 result-1 path (0x1F9 FUN_004640E0, 0x1FA FUN_00464890,
# 0x4B8 FUN_00464400, 0x4CF FUN_00465890: cash-shop continuations, P8).
WINDOW_OPEN_NEVER = frozenset({0x1A7, 0x1F9, 0x1FA, 0x235, 0x4B8, 0x4CF})

# ---- 0x99 sub 9 (evb A5 G1): the sub-9 handler adds Types 0/1/2 only; 3/4/5 are ignored ----
GIFT_ITEM_TYPES = (0, 1, 2)

# ---- the exp pipeline's event stage (arch09-exp-pipeline, ev-e2) ----
EXP_MULT_MAX = 100.0
# Which grants the event multiplier scales (GameServer.award_exp `source`):
#   kill   - the killer's part of a kill (after the card bonus, the party split and the
#            killer's EXP item)
#   party  - a party member's part of a kill (party.split_exp of the pre-event total, after
#            that member's EXP item)
#   quest  - a quest's hqi Exp (0x27 applies none itself): only with EVENT_EXP_QUESTS
#   mentor - a mentor's 0x7F share: a percentage of what the mentee RECEIVED, which the
#            event already scaled - scaling it again would count the event twice
EXP_SOURCES = ('kill', 'party', 'quest', 'mentor')
EXP_UNSCALED_SOURCES = frozenset({'mentor'})

# The persisted claim record (evb C "Per character"): {event id: UTC time of the grant}.
CLAIMS_KEY = 'event_gifts_claimed'
# session: the login gifts and quest pushes that changed this connection's bag / quest log.
GRANTS_KEY = 'events_grants'
# session: the map loads begun (the before_server_map_load hook) and the C2S 0x63s seen, capped
# at the loads - only the latest load's 0x63 re-arms events_ready (after_resync).
LOADS_KEY = 'events_loads'
RESYNCS_KEY = 'events_resyncs'

ID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')
EVENT_KEYS = frozenset({'id', 'enabled', 'start', 'end', 'exp_mult', 'announce', 'popup_event_news',
                        'login_gift', 'push_quests', 'cash_gift'})
ANNOUNCE_KEYS = frozenset({'text', 'every_min', 'on_enter'})
# The quest log has 5 slots in 2009 (char+0x260..+0x268), 3 in 2008 (quests.MAX_ACTIVE).
PUSH_QUESTS_MAX = 5

# ---- event quests (ev-e5; evb A3, A8 "S2C 0x26 / C2S 0x17 / S2C 0x27", E5) ----
NICOLAS_NPC = 76                          # hni 76 "Nicolas" (NPCLngKo 143), ENPC of 27 quests
# The only fully English event chain (2009 hqi, evb A3): 158 "*Monster Card Challenge" ->
# 159 -> 160, SNPC 0 / ENPC 76, Lv 1-99, Repeat 0; Demand Blue Mushroom 3 x20 / Pork 140 x30
# / Gray Scrap Iron 181 x50; Reward Card <Koring> 2031 / <Dumpling Pig> 2038 / <Iron Ball>
# 2043, Exp 50 / 100 / 200, Money 500 / 1000 / 2000. The shipped p13-exit event pushes 158.
ENGLISH_EVENT_CHAIN = (158, 159, 160)
# The quest-log texts (hqi Title / Explain / Intro ids) live in this plaintext table.
QUEST_TEXT_LNG = 'QSTLngKo.lng'
# A NextQuest walk stops after this many quests (the longest hqi chain is far shorter; it is
# only the guard against a data cycle).
QUEST_CHAIN_MAX = 16


class EventError(cfgmod.ConfigError):
    """A malformed event schedule. A ConfigError, so a bad file stops the server start
    before a port is bound, like any other configuration error."""


class WindowOpenRefused(ValueError):
    """arch09-window-open: an id outside WINDOW_OPEN_ALLOWLIST (or one of WINDOW_OPEN_NEVER)."""


# ================================================================== model ===
@dataclass(frozen=True)
class Event:
    """One validated schedule entry (parse_event). Times are UTC epoch seconds."""
    id: str
    enabled: bool = True
    start: float = None
    end: float = None
    exp_mult: float = 1.0
    announce: bytes = b''              # the whole 0x15 text, prefix included (<= 88 B)
    every_min: float = 0.0             # 0: the entry line only
    on_enter: bool = True
    popup: bool = False
    login_gift: tuple = ()             # ((item, count), ...)
    push_quests: tuple = ()            # ev-e5
    cash_gift: tuple = ()              # ev-e6 (P8)

    def scheduled(self, now):
        """In its own window (and enabled), ignoring any GM override."""
        return (self.enabled and (self.start is None or now >= self.start)
                and (self.end is None or now < self.end))


def parse_time(value, what):
    """ISO 8601 -> UTC epoch seconds; None stays None (open-ended). "Z" and offsets are
    honoured; a time without an offset is UTC (the schedule is UTC, evb E1)."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EventError(f'{what}: {value!r} is not an ISO 8601 time (e.g. "2009-02-13T00:00:00Z") or null')
    text = value.strip()
    if text[-1] in 'Zz':
        text = text[:-1] + '+00:00'
    try:
        when = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise EventError(f'{what}: {value!r} is not an ISO 8601 time (e.g. "2009-02-13T00:00:00Z")') from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.timestamp()


def fmt_time(epoch):
    """'2009-02-13 00:00Z' (UTC), '-' for None: the `!event` lines."""
    if epoch is None:
        return '-'
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime('%Y-%m-%d %H:%MZ')


def iso_now(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def announcement_text(text, what='announce.text'):
    """The 0x15 text of an announcement as wire bytes: "[Announcement] " is put in front
    unless the text already starts with "[Annou" (the 2008 colour test; see ANNOUNCE_TEST),
    and the result must fit the client's 88-byte buffer (ev-e1: longer text is refused, never
    cut). Encoded like the S2C 0x01 notice: latin-1, else the client's cp949."""
    if isinstance(text, (bytes, bytearray)):
        raw = bytes(text)
    elif isinstance(text, str):
        raw = cfgmod.notice_bytes(text)
    else:
        raise EventError(f'{what}: expected a string, got {text!r}')
    raw = raw.strip()
    if not raw:
        raise EventError(f'{what}: empty')
    if b'\0' in raw:
        raise EventError(f'{what}: contains a NUL byte')
    if not raw.startswith(ANNOUNCE_TEST):
        raw = ANNOUNCE_PREFIX.encode('ascii') + raw
    if len(raw) > ANNOUNCE_MAX_BYTES:
        raise EventError(f'{what}: {len(raw)} bytes with the "{ANNOUNCE_PREFIX.strip()}" prefix; the '
                         f'client buffer holds {ANNOUNCE_MAX_BYTES} (S2C 0x15)')
    return raw


def announcement_fields(text):
    """S2C 0x15 fields of one announcement line (msg_type 2: teal "[Ann..." line, and in 2009
    the centre notice too). packets.build fills text_len."""
    return {'msg_type': ANNOUNCE_MSG_TYPE, 'text': announcement_text(text)}


def window_open_fields(window_id):
    """arch09-window-open: S2C 0x80 fields that OPEN client window `window_id` - {1, id}, the
    bytes `80 01 FB 03 00 00` for Event News. Separate from the password gate on purpose
    (X2): that one answers a C2S 0x51 and owns {0x1A7, 0x1F9, 0x235}; this one is sent on the
    server's own initiative and may only open WINDOW_OPEN_ALLOWLIST. Result 1 is fixed: any
    other result shows "Invalid password." (0x452A11)."""
    wid = int(window_id)
    if wid in WINDOW_OPEN_NEVER or wid not in WINDOW_OPEN_ALLOWLIST:
        raise WindowOpenRefused(f'window 0x{wid:X} is not openable through S2C 0x80 '
                                f'(allowlist {sorted(hex(w) for w in WINDOW_OPEN_ALLOWLIST)})')
    return {'result': 1, 'target_window_id': wid}


def open_window(server, session, window_id, why=''):
    """Send the arch09-window-open 0x80 to one in-world session. False when the session has
    no socket; WindowOpenRefused for an id outside the allowlist (a programming error, never
    a player's input). The handler has no state gate (spec 0x80), but a window needs the
    map UI, so only in world."""
    fields = window_open_fields(window_id)
    if not session.get('in_world'):
        return False
    ok = server._push(session, '0x80', fields, 'EVENT')
    if ok:
        log.info(f'[EVENT] 0x80 {{1, 0x{int(window_id):X}}} opens window {int(window_id)} for '
                 f'{session.get("char_name")!r}' + (f' ({why})' if why else ''))
    return ok


def scale_exp(amount, mult):
    """The event stage of arch09-exp-pipeline (ev-e2): int(amount * mult), at least 1 - so a
    1-exp kill still gains under a 0.5 event - applied to positive grants only (a loss is
    never scaled). mult 1 returns the amount unchanged, which is what keeps every exp number
    identical while no event runs. The product is rounded to 6 places before the truncation
    so a float like 0.29 * 100 cannot lose a whole point."""
    amount = int(amount)
    if amount <= 0 or mult == 1.0:
        return amount
    return max(1, int(round(amount * float(mult), 6)))


def ensure(char):
    """Create / normalize char['event_gifts_claimed'] in place (idempotent; store migration
    and new characters). Keys are event ids, values the UTC time of the grant. A malformed
    key is dropped; a claim with an odd value is KEPT (as text): dropping it would grant that
    gift a second time."""
    value = char.get(CLAIMS_KEY)
    fixed = {}
    if isinstance(value, dict):
        for key, when in value.items():
            if isinstance(key, str) and ID_RE.match(key) and when not in (None, '', False):
                fixed[key] = when if isinstance(when, str) else str(when)
    if value != fixed:
        char[CLAIMS_KEY] = fixed
    return char[CLAIMS_KEY]


def grants(session):
    """How many login gifts and event-quest pushes changed `session`'s bag / quest log (bumped
    under its combat lock). GameServer._map_transfer reads it before it builds the 0x03 and
    again after the map-load hooks (whose before_server_map_load hook clears events_ready under
    the same lock): a different value means a grant landed in between, so that 0x03 is stale."""
    return int((session or {}).get(GRANTS_KEY) or 0)


def _count_grant(session):
    """(Caller holds the session's combat lock.) One more grant for grants()."""
    session[GRANTS_KEY] = grants(session) + 1


# ============================================================ event quests ===
def quest_text_refusal(q):
    """None when every quest-log text of hqi quest `q` (Title, Explain, Intro: QSTLngKo.lng
    ids) is plain ASCII, else why not. The EN font draws ASCII only, and the table is read as
    cp949, so a Korean title comes back as Hangul (evb A3 notes, open question Q12: the
    Korean-text event quests 232-291 would show as CP949 bytes). A text id the table lacks
    cannot be vouched for and refuses too; id 0 is "no text"."""
    table = EC.lng(QUEST_TEXT_LNG)
    for what, text_id in (('title', q.title_text), ('explain', q.explain), ('intro', q.intro)):
        if not text_id:
            continue
        text = table.get(text_id)
        if text is None:
            return f'{what} text {text_id} is not in {QUEST_TEXT_LNG}'
        if any(ord(ch) > 0x7F for ch in text):
            # !a: the reason goes into log lines, and a console in cp1252 cannot print Hangul
            return f'Korean {what} text ({QUEST_TEXT_LNG} {text_id}: {text[:16]!a})'
    return None


def quest_refusal(quest_id, allow_korean=False):
    """The static push gates of ev-e5, from the ACTIVE build's hqi only: None when
    `quest_id` can be pushed as an event quest on this client build, else why not.
      - it must be in the hqi (the client drops a 0x26 whose record is missing);
      - it must have an ENPC: FUN_0048a510 (2009) sends the 0x17 only when the clicked NPC's
        template equals the quest's ENPC, and no template is 0 - quest 63 "Let's Gather
        Chocolate" (the Love Potion x5 reward) has ENPC 0 and could never be turned in;
      - its quest-log texts must be plain ASCII (quest_text_refusal) unless allow_korean
        (config EVENT_QUEST_KOREAN_TEXT)."""
    q = EC.quests().get(int(quest_id))
    if q is None:
        return f"not in this client build's hqi ({EC.client_build()})"
    if not q.enpc:
        return 'ENPC 0: no NPC can turn it in (FUN_0048a510 compares ENPC with the clicked NPC)'
    if not allow_korean:
        why = quest_text_refusal(q)
        if why is not None:
            return f'{why}; config EVENT_QUEST_KOREAN_TEXT pushes it anyway'
    return None


def quest_chain(head, catalog=None):
    """[head, its NextQuest, that one's NextQuest, ...] in the build's hqi (158 -> [158, 159,
    160] in 2009). Stops at NextQuest 0, an id the hqi lacks, a repeat or QUEST_CHAIN_MAX.
    An unknown head gives []."""
    catalog = EC.quests() if catalog is None else catalog
    chain, quest_id = [], int(head)
    while quest_id and quest_id not in chain and len(chain) < QUEST_CHAIN_MAX:
        q = catalog.get(quest_id)
        if q is None:
            break
        chain.append(quest_id)
        quest_id = q.next_q
    return chain


def check_quests(events, allow_korean=False):
    """Log, at start and on `!event reload`, every push quest this build will never push
    (quest_refusal). A warning, not an EventError: the same schedule serves both builds, and
    the 2008 hqi has no English event quest at all (its 158-161 are Korean with ENPC 0)."""
    for ev in events:
        for head in ev.push_quests:
            for quest_id in quest_chain(head) or [head]:
                why = quest_refusal(quest_id, allow_korean)
                if why is not None:
                    log.warning(f'[EVENT] {ev.id}: quest {quest_id} is never pushed on the '
                                f'{EC.client_build()} client ({why})')


# ============================================================== validation ===
def _number(raw, key, default, what):
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventError(f'{what}.{key}: expected a number, got {value!r}')
    return float(value)


def _flag(raw, key, default, what):
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise EventError(f'{what}.{key}: expected true/false, got {value!r}')
    return value


def _u16(value, what, lo=1):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= 0xFFFF:
        raise EventError(f'{what}: {value!r} is not an id/count {lo}..65535')
    return value


def _gift_rows(value, what):
    if value is None:
        return ()
    if not isinstance(value, list):
        raise EventError(f'{what}: expected a list of [item, count] pairs, got {value!r}')
    rows = []
    for i, row in enumerate(value):
        if isinstance(row, dict):
            row = [row.get('item'), row.get('count', 1)]
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise EventError(f'{what}[{i}]: expected [item, count], got {row!r}')
        rows.append((_u16(row[0], f'{what}[{i}] item'), _u16(row[1], f'{what}[{i}] count')))
    return tuple(rows)


def parse_event(raw, where='event'):
    """One schedule entry -> Event, or EventError naming the bad field. Unknown keys are
    logged and ignored and '_' keys are comments, like config.json."""
    if not isinstance(raw, dict):
        raise EventError(f'{where}: an event is an object, got {raw!r}')
    eid = raw.get('id')
    if not isinstance(eid, str) or not ID_RE.match(eid):
        raise EventError(f'{where}.id: {eid!r} must match [A-Za-z0-9_.-]{{1,32}}')
    what = f'event {eid!r}'
    for key in raw:
        if not str(key).startswith('_') and key not in EVENT_KEYS:
            log.warning(f'[EVENT] {what}: unknown key {key!r} ignored')
    start = parse_time(raw.get('start'), f'{what}.start')
    end = parse_time(raw.get('end'), f'{what}.end')
    if start is not None and end is not None and end <= start:
        raise EventError(f'{what}: end {raw.get("end")!r} is not after start {raw.get("start")!r}')
    exp_mult = _number(raw, 'exp_mult', 1.0, what)
    if not 0 < exp_mult <= EXP_MULT_MAX:
        raise EventError(f'{what}.exp_mult: {exp_mult:g} must be above 0 and at most {EXP_MULT_MAX:g}')
    text, every, on_enter = b'', 0.0, True
    announce = raw.get('announce')
    if isinstance(announce, str):
        text = announcement_text(announce, f'{what}.announce')
    elif isinstance(announce, dict):
        for key in announce:
            if not str(key).startswith('_') and key not in ANNOUNCE_KEYS:
                log.warning(f'[EVENT] {what}.announce: unknown key {key!r} ignored')
        text = announcement_text(announce.get('text'), f'{what}.announce.text')
        every = _number(announce, 'every_min', 0.0, f'{what}.announce')
        # At least a minute between two lines: a fraction would put one in every chat window
        # at each TICK_SECS tick.
        if every and not 1 <= every <= 24 * 60:
            raise EventError(f'{what}.announce.every_min: {every:g} must be 0 (entry only) or 1 .. 1440')
        on_enter = _flag(announce, 'on_enter', True, f'{what}.announce')
        if not on_enter and not every:
            raise EventError(f'{what}.announce: on_enter false and every_min 0 would never announce')
    elif announce is not None:
        raise EventError(f'{what}.announce: expected a text or {{"text", "every_min", "on_enter"}}')
    quests = raw.get('push_quests', [])
    if not isinstance(quests, list) or len(quests) > PUSH_QUESTS_MAX:
        raise EventError(f'{what}.push_quests: at most {PUSH_QUESTS_MAX} quest ids, got {quests!r}')
    quests = tuple(_u16(q, f'{what}.push_quests') for q in quests)
    cash = raw.get('cash_gift', [])
    if not isinstance(cash, list):
        raise EventError(f'{what}.cash_gift: expected a list, got {cash!r}')
    if cash:
        log.warning(f'[EVENT] {what}: cash_gift is ev-e6 (after P8, S2C 0x6C origin 3); ignored')
    return Event(id=eid, enabled=_flag(raw, 'enabled', True, what), start=start, end=end,
                 exp_mult=exp_mult, announce=text, every_min=every, on_enter=on_enter,
                 popup=_flag(raw, 'popup_event_news', False, what),
                 login_gift=_gift_rows(raw.get('login_gift'), f'{what}.login_gift'),
                 push_quests=quests, cash_gift=tuple(cash))


def parse_schedule(data, where='events'):
    """{"events": [...]} (or a bare list) -> [Event] with unique ids."""
    if isinstance(data, dict):
        data = data.get('events', [])
    if not isinstance(data, list):
        raise EventError(f'{where}: expected {{"events": [...]}}, got {type(data).__name__}')
    events, seen = [], set()
    for i, raw in enumerate(data):
        ev = parse_event(raw, f'{where}[{i}]')
        if ev.id in seen:
            raise EventError(f'{where}: event id {ev.id!r} is used twice')
        seen.add(ev.id)
        events.append(ev)
    return events


def check_catalog(events):
    """Every login-gift item must be an EN item of Type 0/1/2 in the ACTIVE build's catalog
    (en_content is configured per build): the sub-9 handler ignores Types 3/4/5 and unknown
    ids silently (evb A8), so a bad gift would claim without granting. A catalog that cannot
    be read (no client install) only warns: grant_item refuses the item again at run time.

    Nor a cash item (Cash != 0: the costumes, Types 0/1/2 with the flag). The gift lands after
    the map load's owned cash list (S2C 0x6F after 0x28 / 0x44, the 0x99 sub 9 after C2S 0x63:
    arch09-resync-bundle), but the NEXT 0x6F - any later map load, a mall entry, a cash grant -
    purges every cash item from every bag client-side first (P8, GameServer._send_owned_cash)
    while the bag model kept it. A cash gift is ev-e6's (cash_gift), never a bag grant."""
    try:
        items = EC.items()
    except Exception as e:                                  # noqa: BLE001 - no install: warn only
        log.warning(f'[EVENT] item catalog unavailable ({e}); login gifts not checked')
        return
    for ev in events:
        for item, _count in ev.login_gift:
            if not items.exists(item):
                raise EventError(f'event {ev.id!r}.login_gift: item {item} is not in the EN client catalog')
            kind = items.type_of(item)
            if kind not in GIFT_ITEM_TYPES:
                raise EventError(f'event {ev.id!r}.login_gift: {EC.item_name(item)} ({item}) has Type {kind}; '
                                 f'S2C 0x99 sub 9 only adds Types 0/1/2')
            if getattr(items.get(item), 'is_cash', False):
                raise EventError(f'event {ev.id!r}.login_gift: {EC.item_name(item)} ({item}) is a cash item; '
                                 f'the next S2C 0x6F would purge it from the bag (a cash gift is ev-e6)')


def load_file(path):
    """[Event] from a schedule file. A missing file is no events (logged); a malformed one
    raises EventError."""
    if not os.path.exists(path):
        log.warning(f'[EVENT] schedule {path} not found: no events')
        return []
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except ValueError as e:
        raise EventError(f'{path}: not valid JSON: {e}') from None
    return parse_schedule(data, os.path.basename(path))


# =================================================================== runtime ===
class Events:
    """The event system of one GameServer (server.events).

    Locks: `lock` is a leaf - it guards the schedule, the GM overrides, the ledger and the
    per-session marks, and is never held while a packet is sent, the store is locked or a
    grant runs. The tick runs under world_lock (ticks.Scheduler); each session's effects run
    under its combat lock, taken after it (the world.py ladder: world_lock -> combat_lock ->
    db_lock -> send_lock), with the readiness re-checked inside it (module docstring)."""

    def __init__(self, server, clock=None, mono=None):
        self.server = server
        self.clock = clock or time.time            # UTC wall clock: the schedule
        self.mono = mono or time.monotonic          # the ledger's intervals
        self.lock = threading.Lock()
        self.events = []
        self.path = None
        # `!event start/stop`: {event id: (on, until epoch or None)}; server memory only.
        self.overrides = {}
        # `!expmult`: a multiplier that replaces the events' own while set.
        self.exp_override = None
        # (character key, event id, 'announce' | 'popup') -> mono time it was last shown.
        self.ledger = {}
        server.world.hooks.register(worldmod.BEFORE_SERVER_MAP_LOAD, self._before_map_load)
        self.events, self.path = self._read()

    # ------------------------------------------------------------ schedule ---
    def _read(self):
        cfg = self.server.config
        name = str(cfg.get('EVENTS_FILE', '') or '')
        if not name:
            return [], None
        path = cfg.resolve(name) if hasattr(cfg, 'resolve') else name
        events = load_file(path)
        check_catalog(events)
        check_quests(events, bool(cfg.get('EVENT_QUEST_KOREAN_TEXT', False)))
        if events:
            log.info(f'[EVENT] {len(events)} event(s) from {path}: '
                     + ', '.join(f'{ev.id} ({fmt_time(ev.start)} .. {fmt_time(ev.end)}'
                                 f'{"" if ev.enabled else ", disabled"})' for ev in events))
        return events, path

    def reload(self):
        """Re-read EVENTS_FILE. A bad file keeps the running schedule. -> (ok, message)"""
        try:
            events, path = self._read()
        except EventError as e:
            log.warning(f'[EVENT] reload refused, schedule kept: {e}')
            return False, str(e)
        with self.lock:
            self.events = events
            ids = {ev.id for ev in events}
            self.overrides = {k: v for k, v in self.overrides.items() if k in ids}
            self.path = path
        return True, f'{len(events)} event(s) loaded'

    def get(self, event_id):
        with self.lock:
            return next((ev for ev in self.events if ev.id.lower() == str(event_id).lower()), None)

    def _active_locked(self, ev, now):
        override = self.overrides.get(ev.id)
        if override is not None:
            on, until = override
            if until is None or now < until:
                return on
            del self.overrides[ev.id]                   # a timed override ran out
        return ev.scheduled(now)

    def active(self, now=None):
        """The events running now (their schedule, or a GM override)."""
        now = self.clock() if now is None else now
        with self.lock:
            return [ev for ev in self.events if self._active_locked(ev, now)]

    def state(self, ev, now=None):
        """One word for `!event`: ACTIVE, off, scheduled, ended or disabled (+ the GM mark)."""
        now = self.clock() if now is None else now
        with self.lock:
            on = self._active_locked(ev, now)
            override = self.overrides.get(ev.id)
        gm_mark = ''
        if override is not None:
            gm_mark = ' (GM' + (f' until {fmt_time(override[1])}' if override[1] is not None else '') + ')'
        if on:
            return 'ACTIVE' + gm_mark
        if override is not None:
            return 'off' + gm_mark
        if not ev.enabled:
            return 'disabled'
        if ev.start is not None and now < ev.start:
            return 'scheduled'
        return 'ended'

    # ------------------------------------------------------- exp pipeline ---
    def exp_multiplier(self, now=None):
        """(multiplier, why): the `!expmult` override, else the largest exp_mult of the active
        events (they never stack), else (1.0, None)."""
        with self.lock:
            override = self.exp_override
        if override is not None:
            return override, '!expmult'
        running = [ev for ev in self.active(now) if ev.exp_mult != 1.0]
        if not running:
            return 1.0, None
        best = max(running, key=lambda ev: ev.exp_mult)
        return best.exp_mult, f'event {best.id}'

    def scale_exp(self, amount, source):
        """Stage 2 of arch09-exp-pipeline for one grant of `source` (EXP_SOURCES). Returns the
        amount stage 3 (GameServer.grant_exp: S2C 0x21 / 0x7F) sends."""
        if source not in EXP_SOURCES:
            raise ValueError(f'exp source {source!r} is not one of {EXP_SOURCES}')
        amount = int(amount)
        if amount <= 0 or source in EXP_UNSCALED_SOURCES:
            return amount
        if source == 'quest' and not self.server.config.get('EVENT_EXP_QUESTS', False):
            return amount
        mult, why = self.exp_multiplier()
        scaled = scale_exp(amount, mult)
        if scaled != amount:
            log.info(f'[EVENT] {source} exp {amount} x{mult:g} ({why}) -> {scaled}')
        return scaled

    # ------------------------------------------------------ entry effects ---
    @staticmethod
    def _char_key(session):
        return str(session.get('char_name') or session.get('username') or '').lower()

    def _before_map_load(self, server, session, **_):
        """before_server_map_load: no event packet until this map load's C2S 0x63. Under the
        combat lock, so an effect the tick is delivering finishes first (and its grant is
        counted before _map_transfer compares grants()); the load is counted (LOADS_KEY)."""
        self.suspend(session, load=True)

    def suspend(self, session, load=False):
        """No event packet for `session` until its next map load's C2S 0x63 (after_resync). The
        hook body above (`load`: one more 0x03 on its way, whose 0x63 must be seen), and the
        P8 mall entry (mall.Mall.enter), which leaves the map without a map load: its S2C 0x6F
        / 0x6A must not be followed by an announcement, a 0x99 sub 9 gift or a quest push the
        tick was delivering. Under the combat lock, so such an effect finishes before the
        caller goes on, and none starts again until the mall exit's map load's 0x63."""
        with self.server._combat_lock(session):
            session['events_ready'] = False
            if load:
                session[LOADS_KEY] = int(session.get(LOADS_KEY) or 0) + 1

    def after_resync(self, sock, session):
        """GameServer._handle_card_deck_list, after its 0x8A / 0x59 / 0x99 sub 8: the client
        has digested the whole map load (C2S 0x63 is the end of its 0x03 handler). The first
        one of a connection runs the entry effects of every active event; later ones (every
        portal) only re-arm the tick. Only the LATEST map load's 0x63 re-arms (module
        docstring "Once per login"): an earlier load's, handled after the next load's hook
        ran, leaves the effects off until that load's own. Returns the number of effects
        delivered."""
        with self.server._combat_lock(session):
            loads = int(session.get(LOADS_KEY) or 0)
            seen = session[RESYNCS_KEY] = min(loads, int(session.get(RESYNCS_KEY) or 0) + 1)
            latest = seen >= loads
            if latest:
                session['events_ready'] = True
        if not latest:
            log.info(f'[EVENT] {session.get("char_name")!r}: the C2S 0x63 of an earlier map load ({seen} of '
                     f'{loads}); the events wait for the latest one\'s')
            return 0
        if not session.get('in_world') or self.server._session_char(session) is None:
            return 0
        return self._run_entries(session, self.active(), 'world entry')

    def _run_entries(self, session, events, why):
        """The once-per-connection effects of `events` for one session, in the resync order:
        announcement (0x15), login gift (0x99 sub 9), ev-e5 quest push (0x26 + 0x59), then ONE
        Event News popup (0x80) however many events asked for it (they share window 1019).
        Returns the number of effects delivered (a line, a gift, a pushed quest, a popup: 1
        each)."""
        sent = 0
        popup_for = []
        lock = self.server._combat_lock(session)
        for ev in events:
            # One event's effects under the combat lock, readiness re-checked inside it: a map
            # load that begins meanwhile (the tick races it) can neither interleave with them
            # nor leave the event marked done with its effects skipped.
            with lock:
                if not self._ready(session):
                    break
                with self.lock:
                    done = session.setdefault('events_entry', set())
                    if ev.id in done:
                        continue
                    done.add(ev.id)
                log.info(f'[EVENT] {ev.id}: entry effects for {session.get("char_name")!r} ({why})')
                if ev.announce:
                    sent += self._announce(session, ev, entry=True)
                if ev.login_gift:
                    sent += self._gift(session, ev)
                if ev.push_quests:
                    # ev-e5: after the gift, before the popup (evb E5); held / done: nothing.
                    sent += self._push_event_quests(session, ev)
                if ev.popup and self._popup_due(session, ev):
                    popup_for.append(ev)
        if popup_for:
            with lock:
                if self._ready(session) and open_window(
                        self.server, session, WINDOW_EVENT_NEWS,
                        f'Event News for {", ".join(ev.id for ev in popup_for)}'):
                    # the ledger only once the window really went out: a send that failed
                    # leaves the popup due at the next login
                    self._popup_shown(session, popup_for)
                    sent += 1
        return sent

    def _announce(self, session, ev, entry=False):
        """One announcement line, if due for this character: at entry when on_enter (unless
        this character saw it within every_min, or EVENT_RELOGIN_QUIET_SECS for an entry-only
        line - a relog), else every every_min minutes. Returns 1 when sent."""
        with self.server._combat_lock(session):             # readiness and the send together
            if not self._ready(session):
                return 0
            key = (self._char_key(session), ev.id, 'announce')
            now = self.mono()
            with self.lock:
                last = self.ledger.get(key)
                if entry:
                    window = ev.every_min * 60.0 if ev.every_min else self._quiet_secs()
                    if not ev.on_enter:
                        # the first periodic line comes every_min after the entry, not at once
                        if last is None:
                            self.ledger[key] = now
                        return 0
                else:
                    window = ev.every_min * 60.0
                if last is not None and now - last < window:
                    return 0
            if not self.server._push(session, '0x15', announcement_fields(ev.announce), 'EVENT'):
                return 0
            with self.lock:
                self.ledger[key] = now                      # only once the line went out
        log.info(f'[EVENT] {ev.id}: 0x15 "{ev.announce.decode("latin-1")}" to {session.get("char_name")!r}'
                 f'{" (entry)" if entry else " (every %g min)" % ev.every_min}')
        return 1

    @staticmethod
    def _ready(session):
        """Past its latest map load's C2S 0x63 and still reachable: re-checked right before
        each effect, because the tick thread races a map load that starts meanwhile (the
        before_server_map_load hook clears events_ready before the lead goes out)."""
        return bool(session.get('events_ready')) and worldmod.reachable(session)

    def _quiet_secs(self):
        return float(self.server.config.get('EVENT_RELOGIN_QUIET_SECS', 1800.0))

    def _popup_due(self, session, ev):
        """Once per session and event, and not again for EVENT_RELOGIN_QUIET_SECS for this
        character (a relog / channel change: evb E4, P13 exit criterion 2). Only the check:
        _popup_shown writes the ledger once the window went out."""
        key = (self._char_key(session), ev.id, 'popup')
        now = self.mono()
        with self.lock:
            last = self.ledger.get(key)
        if last is not None and now - last < self._quiet_secs():
            log.info(f'[EVENT] {ev.id}: no Event News for {session.get("char_name")!r} '
                     f'(shown {now - last:.0f} s ago)')
            return False
        return True

    def _popup_shown(self, session, events):
        """The Event News window for `events` went out to this character just now."""
        now = self.mono()
        key = self._char_key(session)
        with self.lock:
            for ev in events:
                self.ledger[(key, ev.id, 'popup')] = now

    def _gift(self, session, ev):
        """ev-e3: the login gift through S2C 0x99 sub 9 (GameServer.grant_item: the bag model
        is checked and mirrored, stacks 999 / 99 / 1 per packet), once per character and event
        ever. The claim is written only when something was granted; a bag that cannot take the
        WHOLE gift gets nothing, a warning, and the claim stays open for the next login (evb
        E3: the sub-9 handler fails silently on a full tab). Returns 1 when something was
        granted."""
        server = self.server
        char = server._session_char(session)
        if char is None:
            return 0
        who = session.get('char_name')
        # The bag check, the grants and the claim under the combat lock: the bag model changes
        # only under it (a trade commit relies on that), the scratch copy _quest_bag_space
        # takes walks the live bag, and the readiness re-check keeps the grant out of a map
        # load that began meanwhile (module docstring; counted for GameServer._map_transfer).
        with server._combat_lock(session):
            if not self._ready(session):
                return 0
            with self.lock:
                tried = session.setdefault('events_gift_tried', set())
                if ev.id in tried:
                    return 0
                tried.add(ev.id)
            with server.store.lock:
                claims = ensure(char)
                claimed = claims.get(ev.id)
            if claimed:
                log.info(f'[EVENT] {ev.id}: {who!r} claimed the login gift at {claimed}; nothing granted')
                return 0
            why = server._quest_bag_space(session, list(ev.login_gift))
            if why is not None:
                log.warning(f'[EVENT] {ev.id}: login gift {list(ev.login_gift)} does not fit {who!r}\'s bag '
                            f'({why}); nothing granted, the claim stays open for the next login')
                return 0
            granted = []
            for item, count in ev.login_gift:
                ok, err = server.grant_item(session, item, count, f'event {ev.id} login gift')
                if ok:
                    granted.append((item, count))
                else:
                    log.warning(f'[EVENT] {ev.id}: login gift {item} x{count} for {who!r} refused: {err}')
            if not granted:
                return 0
            _count_grant(session)
            with server.store.lock:
                ensure(char)[ev.id] = iso_now(self.clock())
        server.store.mark_dirty(f'event {ev.id} gift claimed by {who}')
        log.info(f'[EVENT] {ev.id}: login gift {granted} granted to {who!r} (claim recorded)')
        return 1

    # ------------------------------------------------------ event quests ---
    def _allow_korean(self):
        return bool(self.server.config.get('EVENT_QUEST_KOREAN_TEXT', False))

    def _chain_owner(self, quest_id, events):
        """The first of `events` whose push_quests chain (quest_chain of each head) holds
        `quest_id`, or None."""
        for ev in events:
            if any(quest_id in quest_chain(head) for head in ev.push_quests):
                return ev
        return None

    def is_event_quest(self, q):
        """Does a refused C2S 0x17 for hqi quest `q` stay silent (evb A8: "0x27, or nothing on
        refusal"; P13 exit criterion 4)? True for a quest no NPC offers but one turns in (SNPC
        0, ENPC set: only a push starts it - the 26 Nicolas quests, 158-160 in 2009) and for any
        quest of a scheduled event's chain, running or not (a held quest outlives its event)."""
        if q is None:
            return False
        if not q.snpc and q.enpc:
            return True
        with self.lock:
            events = list(self.events)
        return self._chain_owner(q.idx, events) is not None

    def _push_event_quests(self, session, ev):
        """ev-e5 entry push: each push_quests head, walked along NextQuest to its first quest
        not yet exhausted, is pushed unless that quest is already held (a relog, a portal, a
        chain in progress: nothing). Returns the number of quests pushed."""
        char = self.server._session_char(session)
        if char is None:
            return 0
        catalog = EC.quests()
        pushed = 0
        for head in ev.push_quests:
            if not self._ready(session):
                break
            state = questmod.QuestState(char)
            target = None
            for quest_id in quest_chain(head, catalog):
                if state.is_active(quest_id):
                    break                       # this chain is under way: nothing to push
                if not state.exhausted(quest_id, catalog.get(quest_id)):
                    target = quest_id
                    break
            if target is None:
                log.info(f'[EVENT] {ev.id}: quest chain {quest_chain(head, catalog) or [head]} for '
                         f'{session.get("char_name")!r}: held or done, nothing to push')
                continue
            pushed += self.push_quest(session, target, f'event {ev.id}')
        return pushed

    def push_quest(self, session, quest_id, why):
        """ev-e5: put one event quest into a character's quest log from the server - the P2
        accept (GameServer._handle_accept_quest) without its NPC offer: S2C 0x26 {quest_id}
        (the client writes its first empty slot, grants the Send items itself and shows the
        Intro) + 0x59 {slot, 0} (0x26 does not reset the slot's progress byte, and the 0x59
        re-runs the client's readiness check in case the Demand items are already in the bag),
        mirrored into the persistent quest store (quests.QuestState) and the bag model (Send
        items, no 0x18). The gates are quest_refusal plus the P2 accept's own: not held, not
        exhausted (Repeat), level range, job flag, PrevQuest completed, a free slot, room for
        the Send items. Any refusal sends nothing and logs why. True when pushed."""
        server = self.server
        quest_id = int(quest_id)
        who = session.get('char_name')

        def refuse(reason, level=logging.INFO):
            log.log(level, f'[EVENT] quest {quest_id} not pushed to {who!r} ({why}): {reason}')
            return False

        reason = quest_refusal(quest_id, self._allow_korean())
        if reason is not None:
            return refuse(reason)
        q = EC.quests().get(quest_id)
        char = server._session_char(session)
        if char is None or not worldmod.reachable(session):
            return refuse('not in the world')
        # The session's combat lock makes the check-and-take atomic against a second push of
        # the same connection from the tick thread (world_lock -> combat_lock -> db: the
        # world.py ladder), and the Send mirror and the packets run under it too: the bag
        # changes only under it (the trade commit relies on that, as for the P2 accept), and a
        # map load that begins meanwhile is either waited for by its hook or seen here as
        # "not ready" (module docstring) - never a 0x26 between its lead and the resync.
        with server._combat_lock(session):
            if not self._ready(session):
                return refuse('a map load is under way (no C2S 0x63 since it began)')
            state = questmod.QuestState(char)
            if state.is_active(quest_id):
                return refuse('already in the quest log')
            if state.exhausted(quest_id, q):
                return refuse(f'already completed {state.times(quest_id)}x (Repeat {q.repeat})')
            level = int(session.get('level') or progression.level_for_exp(int(char.get('exp', 0) or 0)))
            if not q.start_lev <= level <= q.end_lev:
                return refuse(f'level {level} outside {q.start_lev}..{q.end_lev}')
            job_class, job_tier = int(char.get('class') or 0), int(char.get('job2') or 0)
            if not q.job_allows(job_class, job_tier):
                return refuse(f'job flag {q.job!r} refuses class {job_class} tier {job_tier}')
            if q.prev_q and not state.times(q.prev_q):
                return refuse(f'PrevQuest {q.prev_q} not completed')
            if q.needs_slot and state.first_free_slot() is None:
                # The server's model has 3 slots in both builds (the 2009 0x03 sends slots 4-5
                # empty), and a 0x26 the model cannot hold would desync every later 0x59.
                return refuse(f'all {questmod.MAX_ACTIVE} quest slots are used', logging.WARNING)
            full = server._quest_bag_space(session, q.send)
            if full is not None:
                return refuse(f'no bag space for the Send items: {full}', logging.WARNING)
            with server.store.lock:
                slot = state.accept(quest_id, needs_slot=q.needs_slot)
            if slot is None:
                return refuse('the quest log filled up meanwhile')
            _count_grant(session)
            server._quest_mirror_items(session, q.send, f'event quest {quest_id} send')
            server.store.mark_dirty(f'event quest {quest_id} pushed to {who}')
            server._push(session, '0x26', {'quest_id': quest_id}, 'EVENT')
            if slot >= 0:
                server._push(session, '0x59', {'slot': slot + 1, 'progress': 0}, 'EVENT')
            elif q.exp:
                # A talk-only quest (no Demand, ReqPro 0, Money >= 0): the client files it
                # under Completed at once and applies no exp (the P2 accept's F2 branch), so its
                # 0x21 follows the 0x26 - still under this lock (award_exp -> grant_exp takes it
                # again: an RLock), so the readiness checked above covers the exp too and a map
                # load that begins meanwhile cannot rebuild its 0x03 before the exp is applied
                # nor get the 0x21 between its lead and its resync (P13 post-merge review).
                server.award_exp(session, q.exp, 'quest')
        if slot >= 0:
            log.info(f'[EVENT] 0x26 quest {quest_id} pushed to {who!r} ({why}) into slot {slot + 1}: '
                     f'demand {q.demand}, reward {q.reward}, exp {q.exp}, money {q.money}')
        else:
            log.info(f'[EVENT] 0x26 talk-only quest {quest_id} pushed to {who!r} ({why}): completed'
                     f'{f", exp {q.exp}" if q.exp else ""}')
        return True

    def after_turn_in(self, session, q):
        """GameServer._handle_turn_in_quest, after a successful turn-in (0x27 + 0x21 [+ 0x3F]
        are out): the NextQuest of a quest that belongs to a RUNNING event's chain is pushed
        (158 -> 159 -> 160, P13 exit criterion 4). Nothing for any other quest (a story chain's
        NextQuest is offered by its own NPC) or once the event is over (the chain resumes at the
        next login of a later event). True when a quest was pushed."""
        if q is None or not q.next_q:
            return False
        ev = self._chain_owner(q.idx, [e for e in self.active() if e.push_quests])
        if ev is None:
            return False
        return self.push_quest(session, q.next_q, f'event {ev.id}: next after {q.idx}')

    def accept_request(self, session, q):
        """GameServer._handle_accept_quest, for a C2S 0x16 whose quest no NPC offers (SNPC 0):
        when it is a quest of a running event's chain, the request is logged (open question
        Q4: which id the Accept button of quest 158's Finish window 1195 sends) and handled as
        a push - a no-op when the quest is already held or done, never a [Warning]. False
        (the P2 refusal follows) for any other quest."""
        ev = self._chain_owner(q.idx, [e for e in self.active() if e.push_quests])
        if ev is None:
            return False
        log.info(f'[EVENT] C2S 0x16 {q.idx} from {session.get("char_name")!r}: a quest of event '
                 f'{ev.id} (evb Q4: the Finish-chain Accept?) - handled as a push')
        self.push_quest(session, q.idx, f'event {ev.id}: C2S 0x16')
        return True

    # ---------------------------------------------------------------- tick ---
    def tick(self):
        """Every TICK_SECS (GameServer.start, under world_lock): events that began while a
        player was in the world get their entry effects, and every active announcement with
        every_min repeats per character. Only sessions past their latest map load's C2S 0x63.
        Returns the number of effects delivered."""
        running = self.active()
        if not running:
            return 0
        sent = 0
        for session in self.server.world.in_world_sessions():
            if not session.get('events_ready') or self.server._session_char(session) is None:
                continue
            with self.lock:
                done = set(session.get('events_entry') or ())
            fresh = [ev for ev in running if ev.id not in done]
            if fresh:
                sent += self._run_entries(session, fresh, 'event started')
            for ev in running:
                if ev.announce and ev.every_min and ev.id in done:
                    sent += self._announce(session, ev)
        return sent

    # ---------------------------------------------------------- GM commands ---
    def reset(self, session):
        """`!event reset`: forget a character's gift claims and once-per-login marks, so the
        next tick (in world) or login repeats every entry effect (live re-tests)."""
        char = self.server._session_char(session)
        removed = []
        if char is not None:
            with self.server.store.lock:
                claims = ensure(char)
                removed = sorted(claims)
                claims.clear()
            if removed:
                self.server.store.mark_dirty(f'event claims reset for {session.get("char_name")}')
        key = self._char_key(session)
        with self.lock:
            for k in [k for k in self.ledger if k[0] == key]:
                del self.ledger[k]
            for mark in ('events_entry', 'events_gift_tried'):
                session.pop(mark, None)
        return removed

    def dev_event(self, session, args):
        """`!event [list] | start <id> [minutes] | stop <id> | auto <id> | reload | reset [name]
        | popup`."""
        server = self.server
        words = str(args or '').split()
        sub = words[0].lower() if words else 'list'
        rest = words[1:]
        if sub in ('list', 'ls'):
            for line in self.describe_lines():
                server._gm_reply(session, line)
            return
        if sub in ('start', 'on', 'stop', 'off', 'auto'):
            if not rest:
                raise gm.DevCommandError('needs an event id')
            ev = self.get(rest[0])
            if ev is None:
                raise gm.DevCommandError(f'no event {rest[0]!r} in the schedule (!event list)')
            until = None
            if sub in ('start', 'on') and len(rest) > 1:
                minutes = gm.parse_float(rest[1], 'minutes')
                if not 0 < minutes <= 60 * 24 * 366:
                    raise gm.DevCommandError(f'minutes: {minutes:g} is outside 0..527040')
                until = self.clock() + minutes * 60.0
            with self.lock:
                if sub == 'auto':
                    self.overrides.pop(ev.id, None)
                else:
                    self.overrides[ev.id] = (sub in ('start', 'on'), until)
            server._gm_reply(session, f'{ev.id}: {self.state(ev)}')
            log.info(f'[EVENT] !event {sub} {ev.id} by {session.get("char_name")!r} -> {self.state(ev)}')
            if sub in ('start', 'on', 'auto'):
                # players in the world get the entry effects at once, not at the next tick
                server.ticks.call_later(0, self.tick, name='event-start')
            return
        if sub == 'reload':
            ok, message = self.reload()
            server._gm_reply(session, f'Reloaded: {message}.' if ok else f'Reload refused: {message}'[:80],
                             'info' if ok else 'warn')
            return
        if sub == 'reset':
            target = session
            if rest:
                target = server.world.find(rest[0])
                if target is None:
                    raise gm.DevCommandError(f'{rest[0]!r} is not in the world')
            removed = self.reset(target)
            server._gm_reply(session, f'{target.get("char_name")}: {len(removed)} gift claim(s) and the '
                                      f'once-per-login marks cleared.')
            server.ticks.call_later(0, self.tick, name='event-reset')
            return
        if sub == 'popup':
            open_window(server, session, WINDOW_EVENT_NEWS, '!event popup')
            return
        raise gm.DevCommandError(f'unknown sub-command {sub!r}')

    def describe_lines(self, now=None):
        """The `!event` listing, one S2C 0x15 line (<= 80 bytes) per row."""
        now = self.clock() if now is None else now
        with self.lock:
            events = list(self.events)
        mult, why = self.exp_multiplier(now)
        lines = [f'EXP x{mult:g}' + (f' ({why})' if why else ' (no event)')
                 + f', {len(events)} event(s) in the schedule']
        if not events:
            lines.append('No events (config EVENTS_FILE).')
        for ev in events:
            extras = []
            if ev.exp_mult != 1.0:
                extras.append(f'x{ev.exp_mult:g}')
            if ev.announce:
                extras.append(f'notice/{ev.every_min:g}m' if ev.every_min else 'notice')
            if ev.login_gift:
                extras.append('gift ' + '+'.join(f'{i}x{n}' for i, n in ev.login_gift))
            if ev.popup:
                extras.append('popup')
            if ev.push_quests:
                extras.append('quests ' + ','.join(str(q) for q in ev.push_quests))
            lines.append(f'{ev.id}: {self.state(ev, now)} {" ".join(extras)}'[:80])
            lines.append(f'  {fmt_time(ev.start)} .. {fmt_time(ev.end)}')
        return lines

    def dev_expmult(self, session, args):
        """`!expmult` shows the multiplier kills use now; `!expmult <x>` overrides every
        event's (0 < x <= 100, server memory); `!expmult off` gives it back to the events."""
        text = str(args or '').strip().lower()
        if text in ('off', 'auto', 'clear', 'event', 'events'):
            with self.lock:
                self.exp_override = None
        elif text:
            value = gm.parse_float(text.lstrip('x'), 'multiplier')
            if not 0 < value <= EXP_MULT_MAX:
                raise gm.DevCommandError(f'multiplier: {value:g} must be above 0 and at most {EXP_MULT_MAX:g}')
            with self.lock:
                self.exp_override = value
        mult, why = self.exp_multiplier()
        self.server._gm_reply(session, f'EXP x{mult:g}' + (f' ({why})' if why else ' (no event)') + '.')
        log.info(f'[EVENT] !expmult {text or "?"} by {session.get("char_name")!r} -> x{mult:g} ({why})')


# The '!' commands (gm.register: no edit of GameServer.DEV_COMMANDS). Their handlers are the
# thin GameServer._dev_event / _dev_expmult, which hand over to server.events.
if 'event' not in gm.COMMANDS:
    gm.register('event', gm.DevCommand(
        '_dev_event', '!event [list|start <id> [min]|stop <id>|auto <id>|reload|reset [name]|popup]',
        'the event schedule; start/stop override it, auto gives it back; reset repeats the '
        'login effects; popup opens Event News', owner='ev-e1..e4 (P13 stage 1)'))
if 'expmult' not in gm.COMMANDS:
    gm.register('expmult', gm.DevCommand(
        '_dev_expmult', '!expmult [x|off]',
        'the exp multiplier kills use now; x overrides the events, off gives it back',
        owner='ev-e2 (P13 stage 1)'))
