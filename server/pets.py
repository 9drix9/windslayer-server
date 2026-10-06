#!/usr/bin/env python3
"""
pets.py - the 2009 pet hooks P8 owes P15 (ROADMAP_2009_ADDENDUM carry-ins C5 / C6)
================================================================================
Pets are phase P15 (systems_2009/pet.md; behind the client patch gate G-CP). What P8 must
provide before that, so P15 does not reopen it, is (1) the pet record itself (cash.py: kind 3,
C1 / C2) and (2) two entry points that already reach the server on a STOCK client and must not
be left unowned. This module is that seam: a stub P15 replaces with the pet model (tick,
0xAB..0xB2, the per-viewer pet_info mirror), keeping the entry points.

    pets = PetStub(server)                  # GameServer.pets
    pets.install(server.cashuse)            # C6: the C2S 0x48 gates / effects of food + ticket
    pets.rename(sock, session, rec)         # C5: 2009 C2S 0x4D PetRename -> S2C 0x73 {0}

C6 - C2S 0x48 on an UNPATCHED exe (pet.md B2, F5 step 5)
------------------------------------------------------
The exe hard-codes the KR item ids (4 lower than EN above 4248: cp-2 fixes them), so the
pet-food case of its cash-use switch (FUN_0046d6f0, KR 0x10BA..0x10BD) and the rename dialog
(KR 0x10DE -> window 0x4CB) never match the EN items. Using EN Pet Food 4286..4289 or the pet
name ticket 4322 from the bag therefore falls into the generic dialog 0x3F4, which sends C2S
0x48 {id} behind "Waiting for the server to respond." - the cash-use dispatcher
(cashuse.CashUse.use) answers S2C 0x72 and calls this hook:
  - food: the gate refuses (0x72 {uid, 0, 0}: the box closes, the food is kept) unless the
    character wears a pet (cash.equipped_pet); with one, the use consumes one (0x72 {uid, id,
    serial} - the client's own consume-by-serial) and fed() applies pet F5 step 2 to the model
    (gauge up to FOOD_GAUGE, a sleeping pet wakes). TODO(P15 pet-s4): S2C 0xB1 {gauge, serial 0}
    (no second consume) and 0xAD {uid, 1} to the owner / the viewers holding its pet_info.
  - the name ticket: always refused and kept. The rename window 0x4CB (-> C2S 0x4D) opens only
    on a cp-2 patched exe; through 0x3F4 the ticket would be spent for nothing.
Every other 0x48 is the generic dispatcher's own business.

C5 - C2S 0x4D PetRename (2009 only; pet.md F9, 5)
-------------------------------------------------
{u32 pet_serial, str[13] new_name}, sent from window 0x4CB, after which the client shows
"Waiting for the server to respond." (0x52CF5C) - a MUST-REPLY (registry BUILD_MUST_REPLY
0x4D). The success reply 0xC0 (+ 0xB0 to the viewers) belongs to the pet model (P15 pet-s6);
until then every request gets the planned refusal S2C 0x73 {0} through the one 0x73 builder
(cashuse.name_change_fields): it closes box 0x16 and shows the client's name-in-use text.
Unreachable on the stock exe (B2: 0x4CB never opens), so it matters once cp-2 is installed.
"""
import logging

import cash as CASH
import cashuse as CU
import names

log = logging.getLogger('WS')

# pet F5 step 2 [I]: "stamina will be recovered up to 90%" (hii text of 4286..4289).
FOOD_GAUGE = 90


class PetStub:
    """The P8-side pet seam of one GameServer (GameServer.pets). P15 replaces the bodies, not
    the entry points (install(), rename())."""

    def __init__(self, server):
        self.server = server

    # ------------------------------------------------------------------- C6 ---
    def install(self, cashuse):
        """Register the pet items with the generic C2S 0x48 dispatcher (CashUse.gates /
        effects). Harmless on a 2008 server: its item table has no such ids, so the dispatcher
        refuses them before any gate runs."""
        for item in CASH.PET_FOOD:
            cashuse.gates[item] = self.food_gate
            cashuse.effects[item] = self.fed
        cashuse.gates[CASH.PET_NAME_TICKET] = self.ticket_gate
        cashuse.effects[CASH.PET_NAME_TICKET] = self.ticket_used
        return self

    @staticmethod
    def food_gate(server, session, d, record):
        """Why this food use is refused (the food kept), or None. Caller holds store.lock."""
        char = server._session_char(session) if session.get('char_name') else None
        if CASH.equipped_pet(char) is None:
            return f'{d.name or d.id}: no pet worn to feed (the food is kept)'
        return None

    @staticmethod
    def fed(server, session, d, record):
        """After the consume + 0x72 (pet F5 step 2 on the model). TODO(P15 pet-s4): the 0xB1 /
        0xAD packets and the viewers' pet_info mirror."""
        char = server._session_char(session) if session.get('char_name') else None
        with server.store.lock:
            pet_rec = CASH.equipped_pet(char)
            if pet_rec is None:
                return None
            pet = CASH.normalize_pet(pet_rec.get('pet'))
            woke = not pet['awake']
            pet.update(gauge=max(pet['gauge'], FOOD_GAUGE), awake=True)
            pet_rec['pet'] = pet
        server.store.mark_dirty(f'pet fed {session.get("char_name")}')
        log.info(f'[PET] {session.get("char_name")!r} fed {pet["name"]!r} ({pet_rec["serial"]:#x}) with '
                 f'{d.name or d.id} (serial {record["serial"]:#x}): gauge {pet["gauge"]}%'
                 f'{", awake" if woke else ""} (model only: 0xB1 / 0xAD are P15 pet-s4)')
        return pet

    @staticmethod
    def ticket_gate(server, session, d, record):
        return (f'{d.name or d.id}: the pet name ticket renames through window 0x4CB (C2S 0x4D, a cp-2 '
                f'patched exe); used from the bag it would be spent for nothing (kept)')

    @staticmethod
    def ticket_used(server, session, d, record):     # never reached while ticket_gate refuses
        log.warning(f'[PET] {session.get("char_name")!r}: pet name ticket {record.get("serial")} used '
                    f'without a rename')

    # ------------------------------------------------------------------- C5 ---
    def rename(self, sock, session, rec):
        """2009 C2S 0x4D {pet_serial, new_name} -> S2C 0x73 {0} (the planned refusal; the pet
        rename itself is P15 pet-s6). Returns the fields sent."""
        serial = int(rec.get('pet_serial', 0) or 0) & 0xFFFFFFFF
        new = names.clean(rec.get('new_name', b''))
        char = self.server._session_char(session) if session.get('char_name') else None
        with self.server.store.lock:
            pet = CASH.equipped_pet(char)
            worn = f'worn pet {pet["serial"]:#x}' if pet is not None else 'no pet worn'
        why = (f'pet {serial:#x} -> {new!r} ({worn}): pet renames are P15 pet-s6 (0xC0 / 0xB0); '
               f'answered with the planned refusal')
        return self.server.cashuse.name_change_result(sock, session, False, why=why, what='pet rename')


def name_change_refusal():
    """The 0x73 fields every 0x4D gets today (registry BUILD_MUST_REPLY's backstop is the same)."""
    return CU.name_change_fields(False)
