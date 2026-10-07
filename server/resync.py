#!/usr/bin/env python3
"""
resync.py - arch09-resync-bundle (ROADMAP_2009_ADDENDUM 3.0, owner P12): the ONE ordered
resync after an S2C 0x03, for both client builds.

Every S2C 0x03 rebuilds the client's world and resets what hangs off CMessenger: friends,
mentors, memos, the room, the blacklist (2009, FUN_0047a850) and the guild, its applications
and the plaza boards (2009, FUN_0047a8c0 / FUN_0047aaa0 / FUN_0047a980) [V guild 1.1, blch
A.1]. At the very end of its 0x03 handler the client then sends, in this order, C2S 0x2F
(2008 0x44EF4F, 2009 0x45353B), C2S 0x63 (2009 0x453557) and - 2009 only - C2S 0x8A
(0x453581). What the server owes after a 0x03 is one bundle in one order:

  stage           trigger                     packets (in order)
  ------------    -------------------------   ------------------------------------------------
  (MapTransfer)   GameServer._map_transfer    0x03, own 0x07, the on_enter_world lines (welcome
                                              0x15, bank 0x65, presence 0x04 / 0x05: the
                                              live-verified 2009 prefix 0x03 0x07 0x15 0x65)
  STAGE_SPAWN     the same map load, then     0x28 / 0x44 (CURRENT hp / mp), 0x6F (the owned
                                              cash list; while a pet is worn it carries that
                                              record, C2 / pet H5), 0x6D gift queue
                  (then the map's 0x1A monsters and 0x11 ground items: world content, not
                  part of the resync)
  STAGE_FRIENDS   C2S 0x2F                    0x0B [+0x7E] [+0x78] (messenger.py F1), then
                                              0xBD (2009: blacklist.py F-B1, after EVERY 0x2F)
  STAGE_CARDS     C2S 0x63                    0x8A deck + one 0x59 per quest slot, 0x99 sub 8
                                              "In channel N." (first of a connection), then the
                                              events tail: announcement, login gift, event
                                              quest push, Event News popup (events.py E4)
  STAGE_GUILD     C2S 0x8A (2009)             the guild reply. P14 guild-g1 (guild.install, guild
                                              F0) replaced 'guild': 0xB3 sub 3 (the member list)
                                              for a member, else sub 15 {0}; then 'guild_apps'
                                              (sub 4 to a master with applications, only after
                                              this 0x8A's sub 3); guild-g6 adds 'guild_boards'
                                              (0xBB in 9702) before 'gm_tag'; 'gm_tag' = sub 37
                                              {own uid} for a visible GM (its tag; sub 15 / 3
                                              replaced entity+0x12), last. Never sub 19 (re-request
                                              loop), never S2C 0xB9, and every step here runs
                                              after the own 0x07 (subs 1/3/6/8/15 write the local
                                              entity): guild.Guilds.send_sub guards all three.

The stages before this module were scattered over _map_transfer and three handlers; the
steps below are those same sends, registered in the same order (no wire change: the
live-verified sequences of both builds are the tests' byte-exact sequences). A group that
joins the bundle registers a step instead of editing the handlers.

    rs = Resync(server)                     # GameServer.__init__ (install_defaults)
    rs.register(STAGE_FRIENDS, 'x', fn, builds={'2009'}, after='blacklist')
    rs.run(STAGE_CARDS, sock, session)      # the C2S 0x63 handler
    rs.order('2009')                        # {stage: [step names]} (tests, docs)

A step is fn(server, sock, session, ctx) -> None | STOP (end the stage here). Steps resolve
server methods at call time (a test that patches one on the instance still sees it). A step
that raises is logged and the stage goes on: one group's bug must not cost another group's
resync packet (a client that misses its 0xBD runs with an empty blacklist until the next 0x03).
An OSError (the connection closed) still propagates, as it did from the inline sends.
"""
import logging
from dataclasses import dataclass, field

log = logging.getLogger('WS')

STAGE_SPAWN = 'spawn'
STAGE_FRIENDS = 0x2F
STAGE_CARDS = 0x63
STAGE_GUILD = 0x8A
STAGES = (STAGE_SPAWN, STAGE_FRIENDS, STAGE_CARDS, STAGE_GUILD)
STAGE_NAMES = {STAGE_SPAWN: 'map load', STAGE_FRIENDS: 'C2S 0x2F', STAGE_CARDS: 'C2S 0x63',
               STAGE_GUILD: 'C2S 0x8A'}
BUILD_2009 = '2009'
STOP = object()


@dataclass
class Step:
    name: str
    fn: object = field(repr=False)
    builds: frozenset = None          # None = every build
    note: str = ''

    def runs_on(self, client_build):
        return self.builds is None or str(client_build) in self.builds


class Resync:
    def __init__(self, server):
        self.server = server
        self.steps = {stage: [] for stage in STAGES}

    def register(self, stage, name, fn, *, builds=None, before=None, after=None, replace=False, note=''):
        """Add step `name` to `stage`: at the end, or before / after a named step; replace=True
        swaps the step of that name in place (the P14 guild reply). Returns the Step."""
        if stage not in self.steps:
            raise ValueError(f'{stage!r}: not a resync stage ({STAGES})')
        steps = self.steps[stage]
        step = Step(name, fn, frozenset(str(b) for b in builds) if builds is not None else None, note)
        names = [s.name for s in steps]
        if replace:
            if name not in names:
                raise KeyError(f'{name!r}: no such step in {STAGE_NAMES[stage]}')
            steps[names.index(name)] = step
            return step
        if name in names:
            raise ValueError(f'{name!r}: already a step of {STAGE_NAMES[stage]}')
        anchor = before or after
        if anchor is None:
            steps.append(step)
        else:
            if anchor not in names:
                raise KeyError(f'{anchor!r}: no such step in {STAGE_NAMES[stage]}')
            steps.insert(names.index(anchor) + (0 if before else 1), step)
        return step

    def order(self, client_build=None):
        """{stage: [step names]} as `client_build` (default: the server's) runs them."""
        build = getattr(self.server, 'client_build', None) if client_build is None else client_build
        return {stage: [s.name for s in self.steps[stage] if s.runs_on(build)] for stage in STAGES}

    def run(self, stage, sock, session, **ctx):
        """Run the steps of `stage` for this server's build in order. Returns the names run."""
        build = getattr(self.server, 'client_build', None)
        ran = []
        for step in list(self.steps[stage]):
            if not step.runs_on(build):
                continue
            ran.append(step.name)
            try:
                if step.fn(self.server, sock, session, ctx) is STOP:
                    break
            except OSError:
                # The connection is gone (the outbox refuses once it closed): nothing later
                # can go out either, and the caller's own error path (the registry, the map
                # load) handles it as it did before the bundle.
                raise
            except Exception:                    # noqa: BLE001 - one group must not cost the next
                log.exception(f'[RESYNC] {STAGE_NAMES[stage]} step {step.name!r} failed for '
                              f'{session.get("char_name")!r}; the stage goes on')
        return ran


# ------------------------------------------------------------- the steps ---
def _vitals(server, sock, session, ctx):
    # CURRENT hp/mp: the maxima here healed the player on every portal (F6 step 5).
    server._send_hp(sock, session, ctx['hp'], no_enc=ctx.get('no_enc'))
    server._send_mp(sock, session, ctx['mp'], no_enc=ctx.get('no_enc'))


def _owned_cash(server, sock, session, ctx):
    # premium_cash-owned-list-sync (F1 step 3): the 0x03 memset the cash bag tab; after the
    # own 0x07 (its tail needs the local player, 0x441D85) and the 0x28 / 0x44.
    server._send_owned_cash(sock, session, reason=ctx.get('reason', 'map load'), no_enc=ctx.get('no_enc'))


def _gift_queue(server, sock, session, ctx):
    # chat_mail_gm-gift-inbox (P8 stage 2): the append-only 0x6D queue, relit on a map load (C20).
    server.mall.send_gift_queue(sock, session, reason=ctx.get('reason', 'map load'), relight=True)


def _friends(server, sock, session, ctx):
    server.messenger.friend_list(sock, session)


def _blacklist(server, sock, session, ctx):
    server.blacklist.send_list(sock, session)


def _cards(server, sock, session, ctx):
    return server._send_card_deck(sock, session)


def _channel(server, sock, session, ctx):
    server._send_channel_notice(sock, session)


def _events(server, sock, session, ctx):
    # ev-e1 / ev-e3 / ev-e4 (events.py): the once-per-login announcement, login gift and Event
    # News popup; nothing without an event, nothing again on a portal.
    server.events.after_resync(sock, session)


def _guild(server, sock, session, ctx):
    # The pre-P14 step; GameServer.__init__ replaces it with guild.install's (same name).
    server._send_guild_info(sock, session)


def _gm_tag(server, sock, session, ctx):
    server._send_gm_guild_tag(sock, session)


def install_defaults(rs):
    """The bundle as the server sends it today (both builds; the 2009-only steps marked)."""
    only_2009 = {BUILD_2009}
    rs.register(STAGE_SPAWN, 'vitals', _vitals, note='0x28 / 0x44')
    rs.register(STAGE_SPAWN, 'owned_cash', _owned_cash, note='0x6F (worn pet first, C2)')
    rs.register(STAGE_SPAWN, 'gift_queue', _gift_queue, note='0x6D')
    rs.register(STAGE_FRIENDS, 'friends', _friends, note='0x0B [+0x7E] [+0x78]')
    rs.register(STAGE_FRIENDS, 'blacklist', _blacklist, builds=only_2009, note='0xBD (bl-1)')
    rs.register(STAGE_CARDS, 'cards', _cards, note='0x8A + 0x59 per slot')
    rs.register(STAGE_CARDS, 'channel', _channel, note='0x99 sub 8 (first 0x63 of a connection)')
    rs.register(STAGE_CARDS, 'events', _events, note='0x15 / 0x99 sub 9 / 0x26 + 0x59 / 0x80 {1, 0x3FB}')
    rs.register(STAGE_GUILD, 'guild', _guild, builds=only_2009,
                note='0xB3 sub 15 {0} (P14: sub 3 / 15 + sub 4 + 0xBB in 9702)')
    rs.register(STAGE_GUILD, 'gm_tag', _gm_tag, builds=only_2009, note='0xB3 sub 37 {own uid}, a visible GM')
    return rs
