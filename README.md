# WindSlayer Private Server (2008 English Client)

A reverse-engineered private server for **WindSlayer**, a 2D side-scrolling MMORPG published by Outspark in 2008. The official servers went dark around 2012; this project aims to make the game playable locally.

**Current status: PLAYABLE-ish.** As of 2026-04-25, the client logs in, transitions through character select, enters the world, renders the map (`stage01_01` = "The Beginning of the..."), shows full HUD with HP/MP/EXP/hotbar/chat/minimap, and displays our welcome message in chat. The character itself doesn't render visibly yet (last known issue — see [Final blocker](#final-blocker)).

![State](https://img.shields.io/badge/state-alpha-yellow) ![Progress](https://img.shields.io/badge/progress-in--world%20%E2%9C%93-brightgreen)

---

## What works

- **Servers**: version (TCP 7011) + game (TCP 7022), full Fireway protocol with both encryption modes
- **Login** with default test/test or admin/admin
- **Character creation** (opcode 0x0E) — stats, class, appearance, name persist via `accounts.json`
- **Character select screen** with VISIBLE character models (not invisible like before)
- **Enter-world flow** — full transition into in-game state
- **Map rendering** — `stage01_01.hmi` loads and shows the actual game world (mountains, trees, clouds)
- **Full HUD** — HP/MP/EXP bar, level, hotbar 1-8, chat panel, mini-map, top-bar buttons, movement controls
- **Welcome chat** appears from "Server[Channel-1]" via opcode 0x0A
- **Stable in-world heartbeats** (22-byte format that only appears once client is in actual gameplay state)
- **In-game opcode handlers** (ported from PySlayer):
  - 0x03 chat → broadcasts as 0x16
  - 0x04 stat increase → 0x14 stat update
  - 0x0B buy item → 0x18 got item
  - 0x0C sell item → 0x19 lost item
  - 0x15 use item/skill → 0x28+0x44 HP/MP restore
  - 0x7E change map (with 587-portal database for travel destinations)
- **`.hmi` map encryption fully cracked** — position-based ADD cipher mod 3 with constants {0xE9, 0xDE, 0xE0}; we can now craft valid map files
- **Login re-auth suppression** — ignores spurious mid-flow 0x01 from the binary patch falling through
- **UDP broadcast suppression** — binary patches NOP the two `SendTo` call sites that otherwise generate WSA error floods on localhost
- **Client error rate**: ~4 errors per session (down from 10,000+ in earlier iterations)

## Final blocker

The 0x07 spawn packet (player data sent on world entry) has a different field layout in the English 2008 client vs the Korean Yahoo client that PySlayer (our reference) targets. Specifically, after `name + uid`, the EN client reads a single byte that gates conditional fields, while PySlayer sends 9 bytes of fixed data there. The misalignment causes the EN client's player struct to receive corrupt data → character doesn't render visibly, no input response, eventually triggers a spurious auto-transition to a non-existent map (e.g. `stage447_18.hmi`) and crashes.

**Fixing this requires** field-by-field reverse-engineering of the EN client's 0x07 handler at VA `0x00450867` (the dispatch table mapping is in `docs/dispatch_table.md`). The decompiled C of the read sequence is in PySlayer's `doc/Windslayers_Full_Packet.c` around line 2046 (`case 7:`). That's bounded work — 3-5 hours of careful IDA/Ghidra time should produce a valid packet.

---

## Progress timeline

- **2026-04-21** — Login, character select, character creation working
- **2026-04-22** — First HUD render, loading screen blocker identified
- **2026-04-23** — Reversed dispatch table, mapped enter-world packet format
- **2026-04-24** — Binary-patched UDP `SendTo` calls; identified missing `stage12_85.hmi` map file as a fake-out (not the real blocker)
- **2026-04-25** — **Massive day.** Discovered [PySlayer](https://github.com/lcy8047/PySlayer), a Python emulator for the Korean Yahoo version. Cracked `.hmi` encryption (trivial mod-3 ADD cipher). Replaced our reverse-engineered enter-world flow with PySlayer's `0x03 + 0x07 + 0x0A`. Got into the actual game world with full HUD + map rendering + welcome chat. Ported in-game handlers for chat / stats / buy / sell / use-item / change-map. Loaded 587-portal database for map travel. Final remaining issue is the EN-vs-KR difference in 0x07 spawn-packet layout.

---

## How to use this

**You need your own copy of the 2008 English WindSlayer client.** This repo contains ONLY the server code and reverse-engineering notes — no game binaries, no game assets. Distributing those would be copyright infringement.

### Prerequisites
- Windows (the client is a 32-bit Windows game)
- Python 3.10+
- An unpatched copy of the 2008 English `WindSlayer.exe` and its game directory
- `ED2DSprite.dll` (if missing, copy `D2DSprite.dll` to that name)
- `__COMPAT_LAYER=RunAsInvoker` env var (the launcher tries to auto-elevate without it)

### Patching the client

Apply these binary patches to a copy of `WindSlayer.exe` (don't distribute the patched binary):

1. **Server IP** — change the hardcoded server IP to `127.0.0.1`. See [`docs/patching.md`](docs/patching.md).
2. **Suppress "No response" dialog** — at file offset `0x3ED30`, change `51 E8 43 00` → `DB E9 43 00` (jump table redirect from 0x43E851 → 0x43E9DB).
3. **NOP UDP SendTo #1** — at file offset `0x4DFF9`, replace 15 bytes (push pushes + call) with `90` (NOP).
4. **NOP UDP SendTo #2** — at file offset `0x236E0`, replace 27 bytes with `90`.

The UDP NOPs are critical to avoid WSA error floods on localhost.

### Crafting map files

The English client requests `stage12_85.hmi` (a map that doesn't exist in any version). Create one by:
1. Use PySlayer's `utils/hsdecrypt.py` algorithm (or our `tools/encrypt_hmi.py`) to encrypt a known-good decrypted map (e.g. a copy of `stage12_20.hmi.out`) as `stage12_85.hmi`.
2. Drop in `hs/` folder.

### Running

```bash
cd server
python windslayer_server.py
```

Then launch your patched client. Log in with `test`/`test`, pick or create a character, hit Start.

---

## Architecture

```
windslayer_server.py    main server, opcode dispatcher, all packet builders
cencmsg.py              Fireway CEncMsg port (XOR + MT19937 encryption)
accounts.json           local account/character DB
portals.json            587 map portal entries (from PySlayer's gamedef.sqlite3)
```

## What we've reverse-engineered

- [`docs/protocol.md`](docs/protocol.md) — Fireway wire protocol
- [`docs/dispatch_table.md`](docs/dispatch_table.md) — opcode→handler mapping (82 handlers, 130+ opcodes)
- [`docs/enter_world_fields.md`](docs/enter_world_fields.md) — the original 0x2B response field map (now superseded by PySlayer's 0x03+0x07+0x0A flow)
- [`docs/patching.md`](docs/patching.md) — client binary patches with file offsets
- [`docs/known_issues.md`](docs/known_issues.md) — blockers, failed experiments, hypotheses

## Tools

- `tools/probe.py` — read-only state snapshot
- `tools/find_char.py` — locate active character struct in heap
- `tools/live_monitor.py` — poll state for changes
- (DO NOT use INT3 breakpoints — Fireway is timing-sensitive and crashes)

## Acknowledgments

- **[lcy8047/PySlayer](https://github.com/lcy8047/PySlayer)** (originally [mirusu400/PySlayer](https://mirusu400.github.io/PySlayer/)) — a working Python server emulator for the Korean Yahoo version. The enter-world packet flow, the 50+ opcode implementations, the `.hmi` decryption algorithm, the portal database, and most of the protocol knowledge for this project came from PySlayer. License: GPL — our reuse of these algorithms is in good faith for protocol interoperability.
- The unnamed contributor who shared the asset dump (`hs_decrypt.zip`), opcode list, and map_codes lookup that helped us crack the encryption and verify the protocol.

## License

MIT for our original code (server, reverse-engineering notes, tools). Inherited code from PySlayer is GPL — that affects any direct copies of their algorithms; the protocol knowledge itself is not copyrightable.

This repository contains **no game binaries, assets, maps, or copyrighted client material** — only original code and protocol documentation. WindSlayer is the property of its original publisher (Outspark, 2008) and current operators (Sesisoft).

## Contributing

Best entry points:

- **The 0x07 spawn-packet RE work** — that's the last blocker. Reverse-engineer EN client's handler at VA `0x00450867` and rebuild the spawn packet to match. Comparison reference is in `PySlayer/doc/Windslayers_Full_Packet.c` `case 7:`.
- **Test with your own EN 2008 client** and report whether you reach the same stuck-spawn state.
- **Port more PySlayer opcode handlers** — they have ~50, we have ~10. Each one ported is one more in-game feature working.
- **Document the dispatch table handlers we haven't analyzed** — we have all 82 VAs but only a handful are understood.
