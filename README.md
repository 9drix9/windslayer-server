# WindSlayer Private Server

A reverse-engineered private server for **WindSlayer**, the 2D side-scrolling MMORPG that Outspark ran in North America (2008–2010). The official servers have been dead since about 2012. This project rebuilds the server from scratch by reverse engineering the game client. The goal is to play the game the way it looked and played in retail.

![State](https://img.shields.io/badge/state-alpha-yellow) ![Client](https://img.shields.io/badge/client-EN%202009%20Build%2014-blue) ![Python](https://img.shields.io/badge/python-3.10%2B-blue)

> This repo holds server code, client patch scripts, reverse-engineering notes and a few text data tables taken from the clients and from PySlayer (item/NPC/map/quest names and stats). It contains no game binaries, art, sound or map files. You need your own copy of the client.

---

## Status

The server runs the **English Outspark client v1.04, Build 14 (Jan 2009)**, the main target. The server also supports the **2008 English beta client** (`CLIENT_BUILD "2008"`, data from `CLIENT_DIR`, default `..` = `server/` placed inside the 2008 install), but this repo has no 2008 client patcher.

### Working in-game
- **Connection:** the Fireway protocol and encryption are cracked, and every packet the client can receive is mapped (165 S2C opcodes in 2008, the 2009 spec in `server/protocol_spec_2009.json`).
- **Basics:** login, character create/select/delete, entering the world, portals, smooth movement (includes a client patch for the stutter).
- **Retail combat.** The client detects the hit and reports it, and the server applies damage using the client's own damage formula.
  - Skills and buffs/debuffs. Ice Spear freezes.
  - The combo counter with CRITICAL! / GOOD / BAD hits.
  - Monsters only fight back after you hit them, and their name turns red while they're fighting.
  - Monster melee and chasing.
  - **Position sync between players.**
    - Each player's moves are replayed on the other clients with the right timing.
    - A hit relays the monster's knockback and stun, matched to the attacker's swing: basic swing, combo stage, dash attack or skill.
    - Ice freezes last the same time on every screen.
    - Live two-client tests: every settled player position matches to the pixel; every resting monster is within 7.5 px.
- **Progression:** EXP, leveling, stats, class change, HP/MP regen, death and revive, village return.
- **World and items:**
  - Quests and quest items.
  - NPC shops, bank, loot drops with pickup and ownership, card deck.
  - Crafting, reinforcement and gathering.
  - Flea market.
- **Equipment:** your equipped gear, clothes and weapon show on your character, for you and for everyone else.
- **Multiplayer:**
  - Players see each other move, fight and level up.
  - Chat, whispers, friends, messenger, memos, mentors, parties, Player Info.
  - GM commands (`/go`, `/kick`, `/manner`, …).
- **Exchange:** player trading, personal shops (stalls), Praise/Report.
- **Spark Shop (cash shop):**
  - Wind Cash/Mileage wallet: there is no top-up yet, so a GM credits it with `!cash <n>`.
  - Buying and gifting cash-bag items (costumes and pets are not sold yet), the cash box, the gift popup.
  - Hair dye, rename, megaphone, stat reset, region/friend warp stones, period EXP items with expiry.
  - Bag slot extensions, memo notes.
  - Players inside the mall are hidden from the map.
  - Enter with `!mall`: Build 14's own Spark Shop button only shows "Coming Soon!!!".
- **Events** (off by default: set `"EVENTS_FILE": "events.json"` in `server/config.json`, then a GM types `!event start p13-exit`, or set `"enabled": true` in `events.json`):
  - A UTC schedule and "[Announcement]" lines, EXP multipliers.
  - One-time login gifts, the Event News popup, event quests handed in to Nicolas.
- **Field bosses:**
  - 11 field-boss spawns (Rynx, Monkey King, two Wasablanca, Leo Wolf, Drill Mole, Wook, King Frog, Waterfrog, Firefrog, Blue Shark), with respawn timers that survive restarts.
  - Per-entry drop rolls, killer-owned trophies, boss attacks A/B and dash, boss quests.

### In progress (being built; not in this repo yet)
- Blacklist and channels 1-4, with Change Channel.
- Guilds: tags, guild points, create/apply/kick, guild chat, grades.

### Researched, coming next
Pets (need a client data patch), instance dungeons, PvP (Arena / Battlefield / Play & Chat / Guild Battle). Specs are in [`docs/systems_2009/`](docs/systems_2009/) and the plan is in [`docs/ROADMAP_2009_ADDENDUM.md`](docs/ROADMAP_2009_ADDENDUM.md).

---

## Quick start (EN 2009 client)

### Requirements
- Windows (the client is a 32-bit Windows game).
- Python 3.10+.
- `pip install pefile` for the client patcher. The server itself needs only the standard library. The dev tools also use `Pillow`, `capstone` and `pefile` (optional: `keystone-engine` for `combo_hud_2009.py --check`, `openpyxl` for `server/data/build_data.py`).
- Your own install of the **WindSlayer EN v1.04 (Build 14)** client.

### 1. Patch the client
Copy the files from [`client_2009/`](client_2009/) into your WindSlayer install folder (next to `WindSlayer.exe`), then run:

```
python patch_2009.py              # server at 127.0.0.1
python patch_2009.py my.host.name # or another address
python patch_2009.py --p2         # optional 2nd client (UDP port 42908) for multiplayer on one PC
```

This writes `WindSlayer_patched.exe` and leaves the original exe unchanged. Before patching, it checks every original byte. The patch:
- stubs out X-Trap;
- points the client at your server;
- disables the "No response" timer and the LAN probe;
- fixes the movement stutter;
- applies the retail monster-aggro and name-colour rules;
- adds the combo/grade HUD. Skip it with `--no-combo-hud`.
- fixes a client bug where about 2.5 % of your own knockbacks were never reported to the server, leaving other players' view of you 30 px off. Skip it with `--no-knock-fix`; details in `docs/CATCHUP_KNOCK_FIX_RE_2026-10-05.md`.

The script header lists every patched address.

### 2. Configure and start the server
In `server/config.json`, set:

```json
"CLIENT_BUILD": "2009",
"CLIENT_DIR_2009": "C:/path/to/your/WindSlayer2009"
```

The server reads item, NPC, map and quest data from your client install. `CLIENT_DIR_2009` may be relative to `server/`. Then run:

```
cd server
python windslayer_server.py
```

The server uses ports **7011** (version), **7022** (game) and **7099** (admin, bound to 127.0.0.1 only). On first start it creates `accounts.json` with the test accounts `test`/`test` (character TestHero) and `admin`/`admin`. Passwords are stored hashed.

- **More accounts:** stop the server and add `"name": {"password": "pw"}` to `accounts.json`. It is hashed on the next start. Or set `"AUTO_REGISTER": true`.
- **Players on other machines:**
  - set `"PUBLIC_IP"` in `config.json` to the server's IPv4 address (host names are not accepted);
  - open TCP 7011 and 7022; 7099 stays local;
  - change or remove the test accounts first.

### 3. Play
The 2009 client has no ID/password form; it takes the login from launcher arguments. Use:

```
play_2009.bat [account] [password]      (default test test)
```

In the launcher, click **Window** (or Full Screen), then **START**. Next, pick the channel, then your character.

For a second player on the same PC:
1. Run `python patch_2009.py --p2`. It writes `WindSlayer_p2.exe`, which uses P2P UDP port 42908.
2. Start it from the install folder with another account: `set __COMPAT_LAYER=RunAsInvoker`, then `WindSlayer_p2.exe -admin admin -x -x`. Or use `python wsdev.py --build 2009 --client 2 up` (default admin/admin).

---

## Repo layout

| Path | What |
|---|---|
| `server/` | The server. `windslayer_server.py` is the main file. Most systems have their own module: `packets.py` (spec-driven codec for both clients), `store.py` (accounts, migrations), `combat.py`, `damage.py`, `mobai.py`, `quests.py`, `inventory.py`, `trade.py`, `stall.py`, `social.py`, `gm.py`, etc. |
| `server/test_*.py` | 54 offline test suites with fake clients for both builds. Run one with `python test_combat.py`. They never touch your real `accounts.json`.<br><br>Most suites read game data from a client install: the 2008 client at `CLIENT_DIR` and the 2009 client at `CLIENT_DIR_2009`. Without one, they skip or fail. Keep the shipped `config.json` on `"2008"` when you run `test_store.py`; it compares the file with the defaults. |
| `server/protocol_spec*.json` | Machine-readable packet grammars for the 2008 and 2009 clients, used by the codec. |
| `server/wsdev.py`, `wsview.py`, `wsre.py` | Dev harness:<br>• `wsdev`: start the server and client and run `!` dev commands;<br>• `wsview`: screenshots, live entity state from client memory, and input;<br>• `wsre`: the reverse-engineering toolkit. |
| `client_2009/` | Client patcher (`patch_2009.py`), the combo HUD patch and the launcher `.bat`. |
| `docs/` | Reverse-engineering docs:<br>• `PROTOCOL.md`: the full protocol reference;<br>• `IMPLEMENTATION_ROADMAP.md` and `ROADMAP_2009_ADDENDUM.md`: the phase plan;<br>• `systems/` and `systems_2009/`: per-system specs;<br>• combat, damage-formula, aggro and equipment RE notes;<br>• a survey of retail gameplay videos. |
| `docs/legacy/` | The April–June 2026 notes from the first 2008-client attempt. |
| `tools/` | Older standalone RE helpers. |

### GM and dev commands
New characters are not GMs, and that includes TestHero. There are two ways to make one:
- run `python wsdev.py gm TestHero` while the server is running;
- or, with the server stopped, set `"gm": 1` on the character in `accounts.json`.

The client picks up the flag after a relog or a portal.

GM chat commands:
- `/go <name>`
- `/kick <slot>` (slot numbers come from `!who`)
- `/manner <name> <n>`

For a GM, a chat line starting with `!` is a dev command. For anyone else it is ordinary chat, except `!mall` (and `!mall status`), which every player can type to enter the Spark Shop. You can also send one from the command line: `python wsdev.py --build 2009 dev <character> "!cmd"`. Examples: `!warp <map> [x y]`, `!level <n>`, `!give <item> [n]`, `!gold <n>`, `!hp <n>`, `!learn <skill> [force]`, `!who`, `!mobs`, `!where`.

---

## How it was done (short version)

1. Mapped the client's packet dispatch, then exported all ~1,500 functions with Ghidra and wrote a grammar for every packet.
2. Cracked the Fireway XOR table and the `.hsi/.hmi/.hsc` asset cipher (additive mod-3 plus a SHA-1 footer).
3. Found, unpacked and patched the Outspark Build 14 client (X-Trap stubs, SSO login, per-channel ports).
4. Used [PySlayer](https://github.com/lcy8047/PySlayer), a Korean-client emulator, as a cross-reference for packet shapes and content tables.
5. Built each system in reviewed, tested phases. Each one was then checked live with two real clients against the server, and against retail gameplay footage from 2009–2011.

---

## Credits

- [PySlayer](https://github.com/lcy8047/PySlayer), originally by mirusu400, is the Korean-client emulator we used as the ground-truth protocol reference. `server/gamedef.sqlite3` comes from PySlayer, which is GPL-3.0.
- Retail gameplay videos from 2009–2011 on YouTube, used as the reference for how the game should look and feel.

## Legal

WindSlayer is the property of its original developers and publishers. This project is a non-commercial preservation effort. It does **not** distribute game binaries, art, sound or map files. Don't upload patched executables or game files to this repo. Our code is under the MIT license (see `LICENSE`); `server/gamedef.sqlite3` keeps PySlayer's GPL-3.0 license.
