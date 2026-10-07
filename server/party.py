#!/usr/bin/env python3
"""
party.py - parties at run time (P6 stage 3; docs/systems/party.md F1-F10), both client builds
============================================================================================
party-core-state, party-invite, party-accept-join, party-leave, party-chat,
party-vitals-sync, party-map-change-hud, party-skill-hooks, party-exp-share.

    parties = Parties(server)                 # GameServer.__init__; register() adds its hooks
    parties.invite(session, target_uid)       # C2S 0x27 -> S2C 0x4E to the invitee (or a refusal)
    parties.accept(session, inviter_name)     # C2S 0x28 -> S2C 0x4F both ways
    parties.leave(session)                    # C2S 0x29 -> S2C 0x51 (+ frame compaction)
    parties.chat(session, text)               # C2S 0x6A -> S2C 0x90 to every member
    parties.tick_vitals()                     # every VITALS_SECS: S2C 0x54 / 0x55 frame sync
    parties.members(session, same_map=True)   # party-skill-hooks: who a party skill reaches
    parties.exp_shares(killer, exp, map_code) # party-exp-share: [(session, exp), ...]

Every packet here is wire-identical in the two builds (spec_2009 0x4E / 0x4F / 0x50 / 0x51 /
0x53 / 0x54 / 0x55 / 0x56 / 0x90 / 0x92 and C2S 0x44A9D6/0x27..0x29, 0x4476CB/0x6A:
"identical"; 0x54 / 0x55 only moved their entity offsets, 2009 0x4E gained a client-side
blacklist gate that needs nothing from the server).

What the client does with a party (party.md section 1, live party#00-#14)
------------------------------------------------------------------------
There is no leader, no party id and no party name on the wire. Each client shows the OTHER
members in the four HUD frames 0x75..0x78 (the "two stacked frames under the quickslots" of
the retail footage) and keeps their uids in a scene array (2008 scene+0x25C, 2009 +0x264):
  S2C 0x4F binds the first free frame (and array slot) - no duplicate check, nothing read
           when all four are bound, and a frame bound with max 0 can never be freed (C11);
  S2C 0x51 {uid} closes that uid's frame; with the RECEIVER's own uid it closes all of them.
           It never compacts (C51, live party#12): with frame 1 empty the client counts
           itself partyless (/p refused, "Make Party" offered again) although frame 2 is open;
  S2C 0x54 / 0x55 {uid, max, cur} refresh a frame's gauges (and the entity's overhead bar
           when that uid is in the scene); the frames show gauges only, no numbers (C51).
So the party is capped at 5 (the receiver + 4 frames) and the server keeps a mirror of every
client's frames (session['party_hud'], 4 uids) to never duplicate a frame, never overflow
them, and to REBUILD a client whose frame 1 emptied while others stay (0x51 self + 0x4F per
other member in join order, party.md F4 step 5).

Map loads (party-map-change-hud): the frames and the scene array SURVIVE 0x08 / 0x03 - live
2008 party#14 (frame intact on 102, /p still sent), and the 2009 0x08 handler closes the
same windows (0x2A, 0x4C4, 0x480, 0x4D, 0x47A, 0x47E, 0x4B, 0x4C6 + the FUN_00497cb0 batch
= 2008 FUN_00483470), never 0x75..0x78. So nothing is re-sent after a map load except the
vitals the mover missed while it had no scene (party_resync). Config
PARTY_HUD_REBUILD_ON_MAP_LOAD rebuilds the frames after every map load instead, should a
client ever be seen dropping them.

Server state (all under Parties.lock)
-------------------------------------
  parties {pid: Party}                     members = sessions in join order (= HUD order)
  invites {invitee uid: {inviter key: Invite}}  pending 0x4E prompts, INVITE_TTL (window 0x71
                                           "Refuse" sends nothing: C52, live party#09)
Session keys: party_id, party_hud ([4] uids), party_vitals (the last (max_hp, hp, max_mp,
mp) pushed about this member), party_resync (push every member's vitals to this client at
the next tick: it missed them during a map load).
Membership is session-scoped and never persisted (party.md 3.2, F6 step 4): leaving the world
or closing the connection leaves the party; a relog starts partyless.

Lock order (world.py "Locks"): world_lock -> combat_lock -> MapMonsters.lock ->
Parties.lock -> store.lock -> send_lock. Every packet is QUEUED (flush=False, as the
messenger does), also a reply on the requester's own thread: nothing writes to a socket while
holding Parties.lock, so one stalled client never stalls another player's party. Nothing
here takes a combat lock under Parties.lock: the skill hooks and the exp share take each
member's combat lock one at a time after their member list was snapshotted.
"""
import logging
import threading
import time
from dataclasses import dataclass, field

import chat as chatmod
import packets as P
import presence
import records as R
import world as worldmod

log = logging.getLogger('WS')

# ---- party.md 3.2 constants ----
PARTY_MAX = 5                # the receiver + the 4 frames 0x75..0x78 (C51: 5th scene slot unreachable)
HUD_SLOTS = 4
LEVEL_GAP = 10               # 0x56 "The level difference between party members must be within 10."
INVITE_TTL = 60.0            # seconds; server policy (the 0x71 Refuse sends nothing, C52)
CHAT_MAX = chatmod.FRIEND_LINE_MAX   # 87: S2C 0x90's 88-byte buffer, C2S 0x6A's sprintf_s(88)
VITALS_SECS = 0.25           # 0x54 / 0x55 coalescing (party.md F7; ticks.py F8 table)
# party-skill-hooks: F3c's interim target of a Heal / Remote Heal / Breath of Life (no target
# on the wire, combat_skill.md Q4): the lowest-HP% same-map member within this range.
HEAL_RANGE_PX = 400.0

# Server-worded S2C 0x15 lines: the EN client has no text of its own for these (party.md
# F1/F2; privacy.py "party"). The client prints "The party is full" itself before it sends
# (FUN_00446300 control 5), so the same words are used for the server-side recheck.
NOT_ONLINE_TEXT = 'That player is not online.'
REFUSING_TEXT = '{} is refusing party invitations.'
FULL_TEXT = 'The party is full'
EXPIRED_TEXT = 'The invitation has expired.'
ALREADY_TEXT = 'You are already in a party.'


@dataclass
class Party:
    pid: int
    members: list = field(default_factory=list, repr=False)    # sessions, join order


@dataclass
class Invite:
    """One pending S2C 0x4E: who asked (name as sent + session identity) and whom. Session
    identities, not uids: a 2009 relogin's new session must not answer a prompt its old
    client showed (the messenger's rule)."""
    name: str
    session: dict = field(repr=False)
    target: dict = field(repr=False)
    t: float


# ------------------------------------------------------------------ pure helpers ---
def uid_of(session):
    return P.session_uid(session) or 0


def name_of(session):
    return str((session or {}).get('char_name') or '')


def key(name):
    """Invite key of a character name (names are unique case-insensitively, names.py)."""
    return chatmod.name_text(name).lower()


def _u16(value, lo=0):
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        v = 0
    return max(lo, min(v, 0xFFFF))


def vitals(session):
    """(max_hp, hp, max_mp, mp) of a member as the frames get them: u16, maxima never 0
    (C11: a 0x4F with max 0 binds a frame that can never be freed, a 0x54 / 0x55 with max 0
    zeroes the receiver's copy and blanks its overhead bar). session['max_hp'] is the
    client's own formula (hpmp.refresh at every map load, level-up, equip, stat change)."""
    return (_u16(session.get('max_hp'), 1), _u16(session.get('hp')),
            _u16(session.get('max_mp'), 1), _u16(session.get('mp')))


def member_fields(session):
    """S2C 0x4F {member_name, member_uid, hp_max, hp_cur, mp_max, mp_cur} (29 B) describing
    `session` to another member."""
    mh, hp, mm, mp = vitals(session)
    return {'member_name': chatmod.name_bytes(name_of(session)), 'member_uid': uid_of(session),
            'hp_max': mh, 'hp_cur': hp, 'mp_max': mm, 'mp_cur': mp}


def levels_ok(levels):
    """party.md F1 step 2.6: the highest and lowest level of the party plus the invitee are at
    most LEVEL_GAP apart (the 0x56 text: "must be within 10")."""
    levels = [int(v) for v in levels]
    return not levels or max(levels) - min(levels) <= LEVEL_GAP


def split_exp(exp, n, bonus_pct):
    """party-exp-share (party.md 5 / Q10: server policy, no client evidence): a kill worth
    `exp` in a party of `n` eligible members pays a pool of exp * (100 + bonus_pct * (n - 1))
    / 100, split evenly; the killer gets the remainder. The retail footage shows the
    "party EXP split" and "a party gives more total XP than soloing" (RETAIL_VIDEO_SURVEY
    P6 item 13). Every share is at least 1. Returns [killer's, each other's...]."""
    exp, n = int(exp), max(1, int(n))
    if exp <= 0 or n == 1:
        return [exp]
    pool = exp * (100 + max(0, int(bonus_pct)) * (n - 1)) // 100
    each = max(1, pool // n)
    return [max(1, pool - each * (n - 1))] + [each] * (n - 1)


class Parties:
    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        self.parties = {}
        self.invites = {}
        self._next_pid = 1
        # party-skill-hooks: fn(server, session, party, reason, others=[...]) after `session`
        # left `party` - `others` are the members it left behind (also when the party
        # dissolved and party.members is already empty). The aura owner (cs-party-skills,
        # GameServer._auras_after_leave) ends the auras a member no longer gets (combat_skill
        # F3f step 4). Called outside Parties.lock.
        self.leave_listeners = []
        # fn(server, session, party) after `session` joined `party` (cs-party-skills: the
        # joiner is covered by the members' auras and they by its). Outside Parties.lock.
        self.join_listeners = []

    # ------------------------------------------------------------------ views ---
    def party_of(self, session):
        """The Party `session` is a member of, or None."""
        if session is None:
            return None
        with self.lock:
            p = self.parties.get(session.get('party_id'))
            return p if p is not None and any(m is session for m in p.members) else None

    def of_uid(self, uid):
        """_party_of(uid) (party-core-state): the party of the online session of account
        `uid`, or None."""
        s = self.server.world.find(int(uid), in_world=False) if uid else None
        return self.party_of(s)

    def members(self, session, *, same_map=False, include_self=True, alive=False):
        """party-skill-hooks: the member sessions of `session`'s party in join order (just
        [session] without a party). same_map: only reachable members on the session's map -
        what a party skill cast there reaches (the caster's client applies Group Heal /
        auras only to party uids in its scene, FUN_004189f0). alive: skip the dead."""
        with self.lock:
            p = self.party_of(session)
            group = list(p.members) if p is not None else [session]
        here = self.server.world.map_of(session)
        out = []
        for m in group:
            if m is session:
                if include_self:
                    out.append(m)
                continue
            if same_map and (here is None or not worldmod.reachable(m) or self.server.world.map_of(m) != here):
                continue
            if alive and m.get('dead'):
                continue
            out.append(m)
        return out

    def online(self, session):
        """A member / inviter that is still this account's live session in the world (a map
        load in flight counts: its frames survive it)."""
        return bool(session is not None and session.get('sock') is not None and not session.get('closed')
                    and not session.get('kicked') and session.get('entered_once') and session.get('char_name')
                    and not self.server.world.superseded(session))

    def level_of(self, session):
        lv = session.get('level')
        if lv:
            return int(lv)
        char = self.server._session_char(session)
        return int(R.level_of(char)) if char is not None else 1

    # --------------------------------------------------------------- sending ---
    def _push(self, target, key_, fields, tag='PARTY'):
        """Queue one packet on `target` (never a socket write under Parties.lock)."""
        if target is None or target.get('sock') is None or target.get('closed') or target.get('kicked'):
            return False
        return self.server._push(target, key_, fields, tag, flush=False)

    def _notice(self, target, text):
        return self._push(target, '0x15', self.server.notice_fields(text, 'warn'))

    # ------------------------------------------------------------ HUD mirror ---
    def hud(self, viewer):
        """The server's copy of `viewer`'s frames 0x75..0x78 (uids; 0 = free)."""
        hud = viewer.get('party_hud')
        if not isinstance(hud, list) or len(hud) != HUD_SLOTS:
            hud = viewer['party_hud'] = [0] * HUD_SLOTS
        return hud

    def _hud_add(self, viewer, member):
        """S2C 0x4F about `member` to `viewer` into its first free frame. Skipped: the viewer
        itself (frames hold others only), a uid it already shows (0x4F has no duplicate
        check - a second frame would be bound for good) and a full HUD (nothing is read)."""
        uid, hud = uid_of(member), self.hud(viewer)
        if not uid or uid == uid_of(viewer) or uid in hud:
            return False
        if 0 not in hud:
            log.warning(f'[PARTY] {name_of(viewer)!r}: 4 frames bound, no 0x4F for {name_of(member)!r}')
            return False
        if not self._push(viewer, '0x4F', member_fields(member)):
            return False
        hud[hud.index(0)] = uid
        return True

    def _hud_remove(self, viewer, uid):
        """S2C 0x51 {uid}: that member's frame closes on `viewer` (no compaction)."""
        hud = self.hud(viewer)
        self._push(viewer, '0x51', {'member_uid': int(uid)})
        for i, u in enumerate(hud):
            if u == uid:
                hud[i] = 0

    def _hud_clear(self, viewer, send=True):
        """S2C 0x51 {viewer's own uid}: every frame closes and the scene array empties."""
        if send:
            self._push(viewer, '0x51', {'member_uid': uid_of(viewer)})
        viewer['party_hud'] = [0] * HUD_SLOTS

    def _hud_rebuild(self, viewer, party):
        """0x51 self, then 0x4F per other member in join order: frame 1 is bound again."""
        self._hud_clear(viewer)
        for m in party.members:
            if m is not viewer:
                self._hud_add(viewer, m)

    def renamed(self, session, old=None, new=None):
        """premium_cash-rename (P8 stage 3; the world.ON_RENAME hook, ROADMAP_2009_ADDENDUM C4): a
        frame shows a member's name only from the 0x4F that bound it (0x54 / 0x55 carry gauges
        only), so every OTHER member's frames are rebuilt - 0x51 self, then 0x4F per member in
        join order (_hud_rebuild), which now carries the new name (member_fields reads the
        session's char_name). Returns how many members were rebuilt."""
        with self.lock:
            p = self.party_of(session)
            others = [m for m in p.members if m is not session and self.online(m)] if p is not None else []
            for m in others:
                self._hud_rebuild(m, p)
        if others:
            log.info(f'[PARTY] {old!r} is now {new!r}: frames rebuilt for {[name_of(m) for m in others]}')
        return len(others)

    # =============================================================== F1 ===
    def invite(self, session, target_uid):
        """C2S 0x27 {target_uid} (popup 0x50 "Make Party": the client printed "You requested
        <name> to join the party." and waits for nothing). In order (party.md F1 step 2):
        not online / oneself -> 0x15; refuses parties (privacy 'party') -> 0x15; already in a
        party -> 0x50 (in OUR party: the inviter's frames are stale, rebuild them); the party
        is full -> 0x15; level gap over 10 -> 0x56; else the invite is recorded (a repeat
        refreshes it) and the invitee gets 0x4E {inviter} (window 0x71 "<A> wants to party
        with you. Would you like to accept?")."""
        me = name_of(session)
        if not session.get('in_world') or not me:
            log.info(f'[PARTY] invite from {session.get("username")!r} not in world - dropped')
            return None
        uid = int(target_uid or 0)
        with self.lock:
            target = self.server.world.find(uid) if uid else None
            if target is not None and not presence.visible_to(target, session):
                target = None
            if target is None or target is session or uid_of(target) == uid_of(session):
                self._notice(session, NOT_ONLINE_TEXT)
                log.info(f'[PARTY] {me!r} invites uid {uid}: not online (0x15)')
                return 'not online'
            other = name_of(target)
            if self.server.blacklist_drops(target, session):
                # P12 bl-3, BLACKLIST_FILTER 'silent' (blch F-B4): the invitee's client would drop
                # the 0x4E (FUN_00484190 at 0x45BD0E) - no window 0x71, no pending invite and
                # nothing back (the inviter's client printed its own request line).
                log.info(f'[PARTY] {me!r} invites {other!r}: blacklisted - dropped (no invite recorded)')
                return 'blacklisted'
            if self.server.refuses(target, 'party', session):
                # The privacy flag, or BLACKLIST_FILTER 'refuse' (blacklist.py).
                self._notice(session, REFUSING_TEXT.format(other))
                log.info(f'[PARTY] {me!r} invites {other!r}: refuses parties (0x15)')
                return 'refused'
            mine, theirs = self.party_of(session), self.party_of(target)
            if theirs is not None:
                if theirs is mine:
                    self._hud_rebuild(session, mine)
                    log.info(f'[PARTY] {me!r} invites {other!r}, already in its party: frames rebuilt')
                    return 'resync'
                self._push(session, '0x50', {})
                log.info(f'[PARTY] {me!r} invites {other!r}: already in a party (0x50)')
                return 'in a party'
            group = list(mine.members) if mine is not None else [session]
            if len(group) >= PARTY_MAX:
                self._notice(session, FULL_TEXT)
                log.info(f'[PARTY] {me!r} invites {other!r}: the party is full (0x15)')
                return 'full'
            if not levels_ok(self.level_of(m) for m in group + [target]):
                self._push(session, '0x56', {})
                log.info(f'[PARTY] {me!r} invites {other!r}: level gap over {LEVEL_GAP} (0x56)')
                return 'level gap'
            now = time.monotonic()
            self._prune(now)
            self.invites.setdefault(uid_of(target), {})[key(me)] = Invite(me, session, target, now)
            self._push(target, '0x4E', {'inviter_name': chatmod.name_bytes(me)})
        log.info(f'[PARTY] {me!r} invites {other!r} (0x4E)')
        return 'invited'

    def _prune(self, now):
        for target_uid, pend in list(self.invites.items()):
            for k, inv in list(pend.items()):
                if now - inv.t >= INVITE_TTL:
                    del pend[k]
            if not pend:
                del self.invites[target_uid]

    def _forget(self, session):
        """Drop the invites `session` received (its dialog went with its client). One it
        MADE stays until answered - the accept then gets 0x53 "<name>is not in server."
        (F2 step 2.2) - or the TTL."""
        for target_uid, pend in list(self.invites.items()):
            for k, inv in list(pend.items()):
                if inv.target is session:
                    del pend[k]
            if not pend:
                del self.invites[target_uid]

    # =============================================================== F2 ===
    def accept(self, session, inviter_name):
        """C2S 0x28 {inviter_name} (window 0x71 Accept echoes the name 0x4E put there,
        live party#09). No pending invite or an expired one -> 0x15; the inviter gone -> 0x53
        "<name>is not in server."; already in a party -> 0x15; full now -> 0x15; level gap
        now -> 0x56 to both. Else the party is created (or extended), the joiner's other
        invites dropped, the joiner gets one 0x4F per member in join order and every member
        a 0x4F about the joiner - real non-zero HP/MP, never the receiver itself (F2 step 6)."""
        me = name_of(session)
        wanted = chatmod.name_text(inviter_name)
        if not session.get('in_world') or not me:
            log.info(f'[PARTY] accept from {session.get("username")!r} not in world - dropped')
            return None
        with self.lock:
            inv = self.invites.get(uid_of(session), {}).pop(key(wanted), None)
            if inv is None or inv.target is not session or time.monotonic() - inv.t >= INVITE_TTL:
                self._notice(session, EXPIRED_TEXT)
                log.info(f'[PARTY] {me!r} accepts {wanted!r}: no pending invite (0x15)')
                return 'expired'
            host = inv.session
            if not self.online(host):
                self._push(session, '0x53', {'char_name': chatmod.name_bytes(inv.name)})
                log.info(f'[PARTY] {me!r} accepts {inv.name!r}, who left (0x53)')
                return 'inviter gone'
            if self.party_of(session) is not None:
                self._notice(session, ALREADY_TEXT)
                log.info(f'[PARTY] {me!r} accepts {inv.name!r} while in a party (0x15)')
                return 'in a party'
            party = self.party_of(host)
            group = list(party.members) if party is not None else [host]
            if len(group) >= PARTY_MAX:
                self._notice(session, FULL_TEXT)
                log.info(f'[PARTY] {me!r} accepts {inv.name!r}: the party is full now (0x15)')
                return 'full'
            if not levels_ok(self.level_of(m) for m in group + [session]):
                self._push(session, '0x56', {})
                self._push(host, '0x56', {})
                log.info(f'[PARTY] {me!r} accepts {inv.name!r}: level gap over {LEVEL_GAP} now (0x56 to both)')
                return 'level gap'
            if party is None:
                party = Party(self._new_pid(), [host])
                self.parties[party.pid] = party
                host['party_id'] = party.pid
                if any(self.hud(host)):
                    self._hud_clear(host)             # leftover frames of an earlier party
            self.invites.pop(uid_of(session), None)
            if any(self.hud(session)):
                self._hud_clear(session)
            party.members.append(session)
            session['party_id'] = party.pid
            for m in group:
                self._hud_add(session, m)
            for m in group:
                self._hud_add(m, session)
            for m in party.members:
                m['party_vitals'] = vitals(m)         # what the 0x4F just carried
            names = [name_of(m) for m in party.members]
        log.info(f'[PARTY] pid={party.pid} join {me!r} (uid {uid_of(session)}), invited by {inv.name!r}: '
                 f'members={names}')
        for fn in list(self.join_listeners):
            try:
                fn(self.server, session, party)
            except Exception:                       # noqa: BLE001 - a listener must not stop a join
                log.exception('[PARTY] join listener failed')
        return 'joined'

    def _new_pid(self):
        pid = self._next_pid
        while pid in self.parties:
            pid += 1
        self._next_pid = pid + 1
        return pid

    # =========================================================== F4 / F5 ===
    def leave(self, session, reason='leave', notify_self=True):
        """C2S 0x29 (Break Party, which always means "the sender leaves": the packet names
        nobody) and the disconnect / leave-world path (notify_self False: the socket is
        gone). The leaver gets 0x51 {own uid} (every frame closes); with one member left
        the party dissolves (0x51 self to it); else every remaining member gets 0x51
        {leaver} and, when its frame 1 is now empty while another is bound, a rebuild
        (party.md F4 step 5, C51). A 0x29 with no party on the server clears the sender's
        frames (a stale HUD). Returns True when the session was in a party."""
        with self.lock:
            party = self.party_of(session)
            session.pop('party_id', None)
            session.pop('party_resync', None)
            if party is None:
                if notify_self and reason == 'leave':
                    self._hud_clear(session)
                    log.info(f'[PARTY] {name_of(session)!r} breaks a party it is not in: 0x51 self (stale frames)')
                else:
                    session['party_hud'] = [0] * HUD_SLOTS
                return False
            party.members = [m for m in party.members if m is not session]
            self._hud_clear(session, send=notify_self)
            uid = uid_of(session)
            rebuilt = []
            if len(party.members) <= 1:
                for r in party.members:
                    r.pop('party_id', None)
                    r.pop('party_resync', None)
                    self._hud_clear(r)
                self.parties.pop(party.pid, None)
                left = list(party.members)
                party.members = []
            else:
                for r in party.members:
                    self._hud_remove(r, uid)
                    hud = self.hud(r)
                    if hud[0] == 0 and any(hud[1:]):
                        self._hud_rebuild(r, party)
                        rebuilt.append(name_of(r))
                left = list(party.members)
        log.info(f'[PARTY] pid={party.pid} {name_of(session)!r} left ({reason}): '
                 + (f'dissolved, 0x51 self to {[name_of(r) for r in left]}' if not party.members
                    else f'0x51 uid {uid} to {[name_of(r) for r in left]}'
                    + (f', frames rebuilt on {rebuilt}' if rebuilt else '')))
        for fn in list(self.leave_listeners):
            try:
                fn(self.server, session, party, reason, others=left)
            except Exception:                       # noqa: BLE001 - a listener must not stop a leave
                log.exception('[PARTY] leave listener failed')
        return True

    def gone(self, session, reason=''):
        """Leaving the world for good / the connection closed (F5): out of the party without
        a packet to the gone socket, and its pending prompts dropped. Idempotent."""
        with self.lock:
            self._forget(session)
            in_party = self.party_of(session) is not None
        if in_party:
            self.leave(session, reason or 'disconnect', notify_self=False)

    # =============================================================== F8 ===
    def chat(self, session, text):
        """C2S 0x6A {text_len, "<own name> : msg"} (/p or the party chat mode; no local echo)
        -> S2C 0x90 (orange, live party#07) with "<real name> : msg" (the embedded name is
        client-controlled: chat.friend_line re-prefixes it) clamped to 87, to EVERY member
        including the sender, on any map. Outside a party: dropped (the client's own gate
        already said "Only possible when you are in a party."). Returns the names reached."""
        name = name_of(session)
        with self.lock:
            party = self.party_of(session)
            if party is None:
                log.info(f'[PARTY] chat from {name!r} dropped: not in a party')
                return []
            line = chatmod.friend_line(name, text)
            heard = [name_of(m) for m in list(party.members) if self._push(m, '0x90', {'text': line})]
        log.info(f'[PARTY] {line.decode("cp949", "replace")!r} -> {heard}')
        return heard

    # =============================================================== F7 ===
    def tick_vitals(self):
        """party-vitals-sync, every VITALS_SECS on the tick thread: for every member whose
        (max_hp, hp, max_mp, mp) changed since the last push, S2C 0x54 (HP pair) and/or 0x55
        (MP pair) to each other reachable member, on any map (a frame updates across maps;
        the entity bar too where the uid is in the receiver's scene). A receiver flagged
        party_resync (it missed pushes during its map load) gets every member's pair. One
        pass coalesces every damage / potion / regen / level-up / death / revive of the
        period into at most one pair per member (per-hit packets lagged the client before,
        0x40). Never to the member itself: its own HUD uses 0x28 / 0x44. Returns the number
        of packets queued."""
        sent = 0
        with self.lock:
            for party in list(self.parties.values()):
                members = list(party.members)
                now = {id(m): vitals(m) for m in members}
                for r in members:
                    if not worldmod.reachable(r):
                        continue
                    full = bool(r.pop('party_resync', False))
                    for s in members:
                        if s is r:
                            continue
                        cur, last = now[id(s)], (None if full else s.get('party_vitals'))
                        if last is None or cur[:2] != last[:2]:
                            sent += self._push(r, '0x54', {'uid': uid_of(s), 'max_hp': cur[0], 'cur_hp': cur[1]})
                        if last is None or cur[2:] != last[2:]:
                            sent += self._push(r, '0x55', {'uid': uid_of(s), 'max_mp': cur[2], 'cur_mp': cur[3]})
                for s in members:
                    s['party_vitals'] = now[id(s)]
        return sent

    # ============================================================ F6 hooks ===
    def entered(self, session, first=False):
        """on_enter_world: after every map load of a member its client missed the pushes made
        while it had no scene, so the next vitals tick re-sends every member's pair to it; its
        own refilled HP/MP reach the others by change detection. The frames themselves
        survive the map load (module docstring) unless PARTY_HUD_REBUILD_ON_MAP_LOAD."""
        with self.lock:
            party = self.party_of(session)
            if party is None:
                if first:
                    session.pop('party_id', None)
                    session['party_hud'] = [0] * HUD_SLOTS
                return
            session['party_resync'] = True
            if not first and self.server.config.get('PARTY_HUD_REBUILD_ON_MAP_LOAD'):
                self._hud_rebuild(session, party)
                log.info(f'[PARTY] {name_of(session)!r} map load: frames rebuilt (PARTY_HUD_REBUILD_ON_MAP_LOAD)')

    # ===================================================== party-skill-hooks ===
    def heal_target(self, caster):
        """F3c interim target of a targeted heal (combat_skill.md: no target on the wire):
        the living same-map member within HEAL_RANGE_PX of the caster (server position
        estimates) with the lowest HP fraction, the caster on a tie or with no party."""
        mh, hp, _, _ = vitals(caster)
        best, best_frac = caster, hp / mh
        cx, cy = caster.get('pos') or (0.0, 0.0)
        for m in self.members(caster, same_map=True, include_self=False, alive=True):
            mx, my = m.get('pos') or (1e9, 1e9)
            if (float(mx) - float(cx)) ** 2 + (float(my) - float(cy)) ** 2 > HEAL_RANGE_PX ** 2:
                continue
            mh, hp, _, _ = vitals(m)
            if hp / mh < best_frac:
                best, best_frac = m, hp / mh
        return best

    # ======================================================= party-exp-share ===
    def exp_shares(self, killer, exp, map_code):
        """[(session, exp)] for one kill worth `exp`, the killer first (party.md 5
        party-exp-share, config PARTY_EXP_SHARE / PARTY_EXP_BONUS_PCT). The kill credit is
        the shared-monster rule of P5 (GameServer._kill_monster): the killer is whoever
        landed the last hit while on the monster's map. Eligible besides him: his party's
        members in world on that same map and alive (a corpse or another map earns
        nothing). Without a party, with the flag off or nobody eligible: [(killer, exp)]."""
        exp = int(exp)
        cfg = self.server.config
        if exp <= 0 or not cfg.get('PARTY_EXP_SHARE', True):
            return [(killer, exp)]
        others = [m for m in self.members(killer, same_map=True, include_self=False, alive=True)
                  if self.server.world.map_of(m) == map_code]
        if not others:
            return [(killer, exp)]
        amounts = split_exp(exp, 1 + len(others), cfg.get('PARTY_EXP_BONUS_PCT', 20))
        return list(zip([killer] + others, amounts))

    # ------------------------------------------------------------ dev view ---
    def describe(self, session):
        """Lines for `!party`: the server's view of this session's party, its frames and the
        invites waiting for it."""
        with self.lock:
            party = self.party_of(session)
            lines = []
            if party is None:
                lines.append('no party')
            else:
                # one member per line: an S2C 0x15 line holds 88 bytes
                lines.append(f'party {party.pid}, {len(party.members)} member(s):')
                for m in party.members:
                    mh, hp, mm, mp = vitals(m)
                    lines.append(f' {name_of(m)}(uid {uid_of(m)}) map {m.get("current_map")} HP {hp}/{mh} '
                                 f'MP {mp}/{mm}{" dead" if m.get("dead") else ""}')
            lines.append(f'frames: {self.hud(session)}')
            pend = self.invites.get(uid_of(session), {})
            now = time.monotonic()
            lines.append('invites: ' + (', '.join(f'{inv.name} ({INVITE_TTL - (now - inv.t):.0f} s)'
                                                  for inv in pend.values()) or '-'))
        return lines


# ------------------------------------------------------------------- hooks ---
def register(hooks, parties):
    """The lifecycle hooks (world.py): every map load of a member queues its vitals resync;
    leaving the world or closing the connection leaves the party (F5); a rename rebuilds the
    other members' frames (ON_RENAME, the C4 hook)."""
    def on_enter_world(server, session, first=False, **_):
        parties.entered(session, first=first)

    def on_leave_world(server, session, reason=None, **_):
        parties.gone(session, reason or 'left the world')

    def on_disconnect(server, session, reason=None, **_):
        parties.gone(session, reason or 'disconnect')

    def on_rename(server, session, old=None, new=None, **_):
        parties.renamed(session, old, new)

    hooks.register(worldmod.ON_ENTER_WORLD, on_enter_world)
    hooks.register(worldmod.ON_LEAVE_WORLD, on_leave_world)
    hooks.register(worldmod.ON_DISCONNECT, on_disconnect)
    hooks.register(worldmod.ON_RENAME, on_rename)
    return on_enter_world, on_leave_world, on_disconnect, on_rename
