# WindSlayer Private Server (2008 English Client)

A reverse-engineered private server for **WindSlayer**, a 2D side-scrolling MMORPG published by Outspark in 2008. The official servers went dark around 2012; this project aims to make the game playable locally.

**Current status: BREAKTHROUGH.** As of 2026-04-25, we're past the loading screen. The HUD renders, the player loads into the world, the chat panel works. Final fix in progress for map rendering. See [Progress](#progress) below.

![State](https://img.shields.io/badge/state-alpha-yellow) ![Progress](https://img.shields.io/badge/progress-in--game%20HUD%20%E2%9C%93-brightgreen)

---

## What works

- **Version server** on TCP 7011 (sends version response, takes launcher past "checking for updates")
- **Game server** on TCP 7022 — full Fireway protocol
- **Key exchange** (opcode 0x5A) with NoEncode + EncodebyArray static-table encoding
- **Login** (opcode 0x01) with password auth — default test/test or admin/admin
- **Character list** display on character select screen
- **Character creation** (opcode 0x0E) — stats, class, appearance, name roundtrip and persist
- **Enter-world flow** — client transitions out of character select and into the in-game world
- **In-game HUD** — HP/MP/EXP bar, hotbar, chat panel, character name, top-bar buttons all render
- **Map file decryption / re-encryption** — `.hmi` files use a position-based ADD cipher (`enc[i] = dec[i] - {0xE9, 0xDE, 0xE0}[i%3]` mod 256). We can craft new map files now.
- **Heartbeat echo** (opcode 0x0D) keeps the connection alive indefinitely
- **Login re-auth suppression** — ignores spurious mid-flow 0x01 that otherwise resets client
- **UDP broadcast suppression** — binary-patched out the `SendTo` calls that otherwise generate WSA error floods on localhost

## Progress timeline

- **2026-04-21** — Login, character select, character creation working
- **2026-04-22** — First HUD render reached, loading screen blocker identified
- **2026-04-23** — Reverse-engineered the dispatch table and most of the enter-world packet format
- **2026-04-24** — Binary-patched `SendTo` calls to stop UDP broadcast retry loop; identified missing `stage12_85.hmi` map file as the loading-screen blocker
- **2026-04-25** — Cracked `.hmi` encryption (it's a trivial `idx % 3` ADD cipher); discovered [PySlayer](https://github.com/lcy8047/PySlayer), an existing Python emulator for the Korean Yahoo version. Ported its `0x03 + 0x07 + 0x08` enter-world flow to our English server. **In-game HUD now renders.** Working on map-render packets next.

---

## Final blocker (small)

The HUD and player state load successfully, but the actual map tiles don't render — the client requests an additional change-map packet but our `mapcode` field byte order may still be wrong (we send little-endian, client appears to read big-endian). Currently iterating on this; once the right mapcode arrives, we should see the actual game world.

If you want to help, the best entry point is testing with your own English 2008 client and reporting whether the map renders.

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

The client hardcodes the server IP into its exe. Patch a copy of `WindSlayer.exe`:

1. Change the hardcoded server IP to `127.0.0.1`. See [`docs/patching.md`](docs/patching.md) for exact offsets.
2. (Recommended) Suppress the "No response from server" dialog by changing the jump table at VA `0x43ED30[0]` from `0x43E851` → `0x43E9DB`.
3. (Recommended for the 2026-04-24 fix) NOP the two `SendTo` call sites at `0x004236E0..0x004236FB` and `0x0044DFF9..0x0044E007` to disable broken UDP broadcasts on localhost.

### Crafting map files

You need a valid `stage12_85.hmi` (the file the client requests). Use `tools/decrypt_hmi.py` and `tools/encrypt_hmi.py` to convert decrypted XML maps to encrypted `.hmi` format. Map XML files come from the Korean game's `hs/` folder (also encrypted there — decrypt first).

### Running the server

```bash
cd server
python windslayer_server.py
```

Then launch your patched client.

### Default credentials
- `test` / `test`
- `admin` / `admin`

Stored in plain text in `accounts.json` — this is a local-only server; don't expose it to the internet without adding real auth.

---

## What we've reverse-engineered

- [`docs/protocol.md`](docs/protocol.md) — Fireway wire protocol: packet header layout, encryption modes, heartbeat
- [`docs/dispatch_table.md`](docs/dispatch_table.md) — the client's opcode→handler dispatch mapping (82 handlers for 130+ opcodes)
- [`docs/enter_world_fields.md`](docs/enter_world_fields.md) — the 21 fields the client parses from the original 0x2B response (now superseded by the 0x03+0x07 flow we discovered from PySlayer)
- [`docs/known_issues.md`](docs/known_issues.md) — blockers, failed experiments, hypotheses

## Tools

The `tools/` directory contains memory-inspection scripts used during RE. They attach read-only to the game process and dump state — no binary patching or breakpoints (INT3 breakpoints crash Fireway's timing-sensitive code, so don't try it).

- `probe.py` — snapshot game state at a labeled moment
- `find_char.py` — locate the active character struct in heap
- `live_monitor.py` — poll state every second for changes

## Acknowledgments

This project would not have been possible without:

- **[lcy8047/PySlayer](https://github.com/lcy8047/PySlayer)** (originally by mirusu400) — a working Python server emulator for the Korean Yahoo version. The enter-world packet flow (0x03 → 0x07 → 0x08) and the `.hmi` decryption algorithm came from this project. **License: GPL.** Our reuse of these algorithms is in good faith for protocol interoperability.
- **[mirusu400's PySlayer page](https://mirusu400.github.io/PySlayer/)** — the FirekeyExtractor tool (extracts XOR keys from newer Korean Fireway.dll versions; not needed for the older clients we use, but documents that the encryption is XOR-based)
- **The unnamed reverse engineer** who shared the map_codes lookup table, NPC/item databases, and decrypted asset dump

## License

MIT for our original code (server, reverse-engineering notes, tools). Inherited code from PySlayer is GPL — that affects any direct copies of their algorithms; the protocol knowledge itself is not copyrightable.

This repository contains **no game binaries, assets, maps, or copyrighted client material** — only original code we wrote and documentation of the protocol. The WindSlayer game itself is the property of its original publisher (Outspark, 2008) and current operators (Sesisoft).

## Contributing

PRs and issues welcome. Best entry points:

- Test the server with your own English 2008 client and report whether the map renders past the loading screen
- Help port more PySlayer opcode handlers (they have ~50, we have ~10)
- Document the dispatch table handlers we haven't analyzed (the dispatch table is mapped, but only ~10 of 82 handlers are understood)
