# WindSlayer Private Server (2008 English Client)

A reverse-engineered private server for **WindSlayer**, a 2D side-scrolling MMORPG published by Outspark in 2008. The official servers went dark around 2012; this project aims to make the game playable locally.

**Current status: LIMITED.** The server gets the client through login, character management, and partial world entry, but gets stuck on a loading-screen blocker we haven't solved. Help wanted.

![State](https://img.shields.io/badge/state-alpha-yellow) ![Progress](https://img.shields.io/badge/progress-login%20%E2%86%92%20char%20select%20%E2%86%92%20loading%20screen-orange)

---

## What works

- **Version server** on TCP 7011 (sends version response, takes launcher past "checking for updates")
- **Game server** on TCP 7022 — full Fireway protocol
- **Key exchange** (opcode 0x5A) with NoEncode + EncodebyArray static-table encoding
- **Login** (opcode 0x01) with password auth — default test/test or admin/admin
- **Character list** display on character select screen
- **Character creation** (opcode 0x0E) — stats, class, appearance, name roundtrip and persist
- **Enter-world transition** (opcode 0x2B/0x2E) — client accepts our world data and transitions
- **Heartbeat echo** (opcode 0x0D) keeps the connection alive indefinitely
- **Login re-auth suppression** — ignores spurious mid-flow 0x01 that otherwise resets client

## Current blocker

After "enter world" the client requests a specific map file: `./hs/stage12_85.hmi`. This file **does not exist in any version** of the game (we verified against both the English client and the still-active Korean client's data). The filename is computed at runtime from data we don't yet control.

Client log after the handshake:
```
Could not load MAP. (./hs/stage12_85.hmi)
```

We've tried:
- Faking the file by copying `stage01_01.hmi` → loading fails silently (the `.hmi` format is obfuscated, content-specific)
- Faking with `main99_01.hmi` (a minimal known-good map) → same
- Various field changes in the 0x2E response to see if they shift the filename → money field changes break the flow but don't change the filename

We suspect the `12` and `85` integers are computed from fields in either the 0x02 login response or a later step. Help finding those fields is the main ask.

See [`docs/known_issues.md`](docs/known_issues.md) for the full list.

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

The client hardcodes the server IP into its exe. You need to patch two byte ranges in a copy of `WindSlayer.exe`:

1. Change the hardcoded server IP to `127.0.0.1`. See [`docs/patching.md`](docs/patching.md) for exact offsets.
2. (Recommended) Patch the "No response from server" dialog jump table at VA `0x43ED30[0]` from `0x43E851` → `0x43E9DB` to suppress the unhelpful timeout dialog.

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
- [`docs/enter_world_fields.md`](docs/enter_world_fields.md) — the 21 fields the client parses from the enter-world response, with buffer offsets
- [`docs/known_issues.md`](docs/known_issues.md) — blockers, failed experiments, hypotheses

## Tools

The `tools/` directory contains memory-inspection scripts used during RE. They attach read-only to the game process and dump state — no binary patching or breakpoints (INT3 breakpoints crash Fireway's timing-sensitive code, so don't try it).

- `probe.py` — snapshot game state at a labeled moment
- `find_char.py` — locate the active character struct in heap
- `live_monitor.py` — poll state every second for changes
- `kr_capture.py` — (unused, Sesisoft's current Korean server requires web-based account auth so we can't capture)

## Contributing

**The main unsolved puzzle is the `stage12_85` map-load blocker.** If you can figure out:
- Where `12` and `85` come from in our server data, OR
- How to format a valid `.hmi` file the client will accept, OR
- How to binary-patch the client to skip map validation

...you'd unblock the project. PRs welcome. See `docs/known_issues.md` for where we've been.

Other good entry points:
- Opcode handlers we haven't mapped (we have the dispatch table but not what each handler does)
- The `.hmi` encrypted map format (we know the files share a header signature "Sa...")
- The UDP map-server protocol (opcode 0x11 broadcast handshake to port 42907)

## License

MIT. See `LICENSE`.

## Acknowledgments

- Outspark for publishing the original game
- Sesisoft (current Korean operator) for keeping a variant alive
- Everyone who's worked on private servers for dead MMOs — this is a tradition
