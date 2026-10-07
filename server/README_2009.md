# EN 2009 client (Outspark v1.04 "Build 14")

> Developer notes. For setup, start with the top-level README.md; the patchers are in `client_2009/`.

The server speaks either the EN 2008 client (`WindSlayer2Game`, the default) or the EN 2009
client (`Desktop\WindSlayer2009`). One config key picks the build; the dev tools
(`wsview.py`, `wsdev.py`) and the server's local-memory combat driver follow it.

## 1. Build the patched 2009 exe

```
python patch_2009.py            # run in your 2009 install (copy client_2009/ there); version server 127.0.0.1
python patch_2009.py my.host    # another address
```

It writes `WindSlayer2009\WindSlayer_patched.exe` from the pristine `WindSlayer.exe` (it checks
every original byte first). The patches: X-Trap stubs, the version server address, the
no-response timer, the LAN probe, and the smooth-frame cave. Needs `pip install pefile`.
The server must never send S2C 0xC5 (X-Trap challenge) to this exe; packets.py refuses it.

## 2. Server configuration (`config.json`)

```json
"CLIENT_BUILD": "2009",
"CLIENT_DIR_2009": "../../WindSlayer2009"
```

- `CLIENT_BUILD`: `"2008"` (default) or `"2009"`. It selects `protocol_spec_2009.json`, version
  code 14, the channel entry with the game port (`GAME_PORT`), the SSO login, the 2009 records,
  and the 2009 client data.
- `CLIENT_DIR_2009`: the 2009 install, relative to `config.json`'s folder or absolute. The
  2008 install is `CLIENT_DIR` (`".."`).
- `DEV_MEMORY_COMBAT` (true): the memory driver now runs for both builds. It reads the active
  build's addresses (`client_layout.py`). It attaches only to a `WindSlayer_patched.exe` whose
  PE timestamp belongs to that build (2008 `0x48240544`, 2009 `0x49797E29`), because both exes
  have the same file name. `DRIVER_MELEE` (swing damage) stays `false`, so the driver only
  tracks the position and runs the `wsdev hit` hook.
- Restart the server after you edit the file. Keep the committed `config.json` on `"2008"`:
  `test_store.py` compares the shipped file with the defaults.

## 3. Launching

The 2009 client has **no ID/password form**. The launcher needs three SSO arguments. The
first argument is sent in C2S 0x01, and the server reads `<account> <password>` from it:

```
WindSlayer_patched.exe -<account> <password> -x -x
```

The exe path must be quoted and the arguments must not be quoted, because the client's
parser (FUN_00440e00) reads the raw command line. The password is visible in the process
list, so use this for test accounts only.

- **By hand:** start the server (`python windslayer_server.py`), then run
  `play_2009.bat [account] [password]` (in your 2009 install) (default test/test).
  1. In the launcher dialog "WindSlayer (Build: 14)", click **Window**, then **START**.
  2. In **Select World & Channel**, pick the channel, then click **OK**.
  3. At character select, pick a slot, then click **START**.
- **wsdev:**
  - `python wsdev.py --build 2009 up [--select] [--user U --pass P --char N]` stops stale
    processes, starts the server, launches the 2009 exe non-elevated with the SSO arguments,
    and auto-logs in.
  - `up` refuses if `config.json` does not say `CLIENT_BUILD "2009"`, because the server would
    speak the other build.
  - For the 2009 build, `wsdev login` cannot change the account: it is fixed by the launch
    command line.
- **Build selection** (every wsview/wsdev command), highest priority first:
  `--build 2009` > env `WS_BUILD=2009` > `config.json` `CLIENT_BUILD` > `2008`. With
  `CLIENT_BUILD "2009"` in the config, `python wsdev.py up` alone drives the 2009 client.
  wsdev passes the build on to its wsview children.
- **wsview:** `python wsview.py --build 2009 state|shot|win|key|click|...`. The `state` command
  reads the 2009 layout: scene `0x54F0C0`, local uid `scene+0x224`, entity uid `+0x88`, type
  `+0x9C`, anim `+0xEFC`, pos f64 `+0x1298/+0x1328`.
- **sendspec / cap:**
  - `wsdev --build 2009 sendspec <op> ...` builds with `protocol_spec_2009.json`.
  - `wsdev --build 2009 cap <secs> <action>` decodes each captured C2S packet with the 2009
    spec.
- `python patch_2009.py --p2` writes `WindSlayer_p2.exe` (P2P UDP port 42907 -> 42908), which
  `wsdev --build 2009 --client 2` launches as the second client (default admin/admin).

### Where the addresses live

- `client_layout.py` has `LAYOUT_2008` and `LAYOUT_2009` (globals, scene and entity offsets,
  PE timestamp). `SOURCES` names the `client_map_2009.json` entry of every field, and
  `test_tooling2009.py` checks both columns against that file.
- The auto-login click points are named constants in `wsdev.py`. They are client-area
  coordinates that you can adjust after a live run:

| constant | value | what |
|---|---|---|
| `LAUNCHER_WINDOW_2009` | (626, 355) | launcher "Window" (windowed-mode) radio |
| `LAUNCHER_START_2009` | (613, 381) | launcher START bitmap (x 564..660, y 361..403, FUN_00488190) |
| `WORLD_CHANNEL_2009` | (250, 155) | "Select World & Channel": channel row |
| `WORLD_OK_2009` | (186, 422) | "Select World & Channel": OK (sends C2S 0x01) |
| `CHAR_SLOTS_X_2009`, `CHAR_SLOT_Y_2009` | 2008 values | character slots (first guess) |
| `CHARSEL_START_2009` | (703, 507), 2008 value | character-select START (first guess) |
| `LAUNCHER_SETTLE_2009` | 3.0 s | wait for the version reply before START |

- Screens are told apart as follows:
  - **Launcher:** window class `#32770` or the title "(Build:".
  - **Character select:** scene mode `scene+0xF18 == 5`, with no local player yet.
  - **World:** an entity whose uid equals `scene+0x224`.
  - **World select:** any other game window, before login.

## 4. Live checks for the lead

Run these with `CLIENT_BUILD "2009"` in `config.json` and the patched exe built.

1. `python wsdev.py --build 2009 up`:
   - The server log header says `Client build: 2009`.
   - The client starts non-elevated.
   - The launcher gets **Window** and **START** clicked.
   - "Select World & Channel" gets the channel and **OK** clicked.
   - Character select gets slot 0 and **START** clicked.
   - The output ends with `IN-WORLD`.
   - If a click misses, take `wsview --build 2009 shot` on that screen and adjust the matching
     `*_2009` constant.
2. `python wsdev.py --build 2009 up --select` stops at character select and reports
   `AT CHARACTER SELECT`. This checks scene mode `+0xF18`.
3. `python wsdev.py --build 2009 status`:
   - It shows the client pid, `IN-WORLD`, and the Lv, HP/MP and position of the character.
   - The HP/MP values must match the in-game bars. This checks `+0x9D`, `+0xA0/+0x11AC`,
     `+0xA4/+0x11B0` and `+0x1298/+0x1328`.
   - `monsters visible` must be greater than 0 on map 102.
4. `python wsview.py --build 2009 state`:
   - The list shows `TestHero` with uid 1 and the Pupu with uids `0x000F0000+`.
   - While you walk, the position changes.
5. `server_live.log`:
   - It shows `[COMBAT] local-memory combat driver started (client build 2009: scene
     0x54F0C0, ...)` and then `attached to client PID <pid>`.
   - Walk somewhere and wait more than 60 s (the world save), or relog. The character must
     come back at the new point, which proves the driver's position read.
6. Stand next to a Pupu and repeat `python wsdev.py --build 2009 hit 3` until it dies. The
   log must show `[KILL]`, and the client must show the death, the exp and the drop.
7. Face left, then right, and use a skill. Its hitbox must follow the facing, which checks
   the facing byte `+0x949` (2 = left, 6 = right).
8. `python wsdev.py --build 2009 cap 3 key q` (or any action) lists the C2S packets decoded
   as `0x.../0x.. <name>: ...` with 2009 keys.
9. `python wsdev.py --build 2009 sendspec 08 ?` prints `u8 reason`, which confirms the 2009
   spec.
10. With both clients running (2008 `WindSlayer2Game\WindSlayer_patched.exe` and 2009):
    - `wsview state` (2008) and `wsview --build 2009 state` each read their own process.
    - `wsdev --build 2009 down` leaves the 2008 client running.
11. Regression: with `CLIENT_BUILD "2008"`, `python wsdev.py up` still logs in through the ID
    and password form exactly as before.
12. The P8 carry-ins (`ROADMAP_2009_ADDENDUM.md` section 2; offline in `test_carryins.py`):
    - `!cash box 3381` in the world: the client pops "Congratulation. You received an event
      item.." (S2C 0x6C, origin 3, T-E6) and the next `!mall` lists the Megaphone in the box;
      the Wind Cash / Mileage labels do not change.
    - `!cash item 4294` (or `!pet give picky`): Picky lands in the equipment tab as a bound pet
      record (limit_type 3). Double-clicking it sends C2S 0x82, answered since P15 stage 1 with
      S2C 0xAB (see 13). With the stock hii no sprite can appear (pet B1).
    - Using Pet Food 20 (`!cash item 4289`) from the bag on the unpatched exe opens the generic
      "use" dialog; its OK must close again with the food kept (no pet worn).
    - If the Spark Shop's sell-to-player window (0x4B8) can be opened: its OK must end in "Your
      target user does not exist in the server." and its Cancel must close the window.
13. P15 stage 1 (pets.py; offline in `test_pets.py`). `CLIENT_ITEM_IDS` (default `"en"`) says
    which item ids the installed exe hard-codes: `"en"` for the cp-2 patched exe (the G-CP patch
    set, installed on the live clients), `"kr"` for the stock exe. `SHOP_EXTRA_ITEMS` adds the
    Pet Bell to the potion grocers (cp-3; a grocer row shows only once the hni lists it).
    - `!pet give picky wear` on A: the pet follows A; B sees it (S2C 0xAB 21 B). `!pet seen`
      lists which clients hold A's pet info. Unequip from the Equipment window: the pet goes
      back to the bag on both clients.
    - `!pet set awake 0|1`, `!pet set level 5`, `!pet set name Tweety` (the owner sees the new
      name at once, the others at A's next appearance), `!pet bell 3`, `!pet food 20`.
14. P15 stage 2 (pets.py; offline in `test_petlife.py`). The pet tick runs over the owner's
    field time (config `PET_*`, all [I]: -1 % gauge and +1 EXP a minute awake, +1 % per 5
    minutes asleep, asleep below 2 %); `!pet tick [n]` runs n steps now.
    - `/Pet smile` plays on both clients (S2C 0xB2); `/Pet warning` needs pet level 5.
    - `!pet food 20`, `!pet set gauge 11`, `!pet tick`: the bubble, the auto-feed (C2S 0x85),
      the gauge back at 90 % and one Pet Food fewer (S2C 0xB1).
    - Without food the pet falls asleep on both clients; `!pet bell 1`, then the bell below
      10 %: "Pet has to have at least over 10% HP to wake up."; `!pet tick 10`, the bell again:
      the bell is used up and the pet is back on both clients.
    - `!pet gear 4290` (Red Hood; `!give 4290` too) and double-click it: the hood on Picky on
      both clients. Ulie's gear (`!pet gear 4297`) on Picky: "This is not the equipment of
      your pet.".
15. P15 stage 3 (pets.py, mall.py; offline in `test_petextras.py`). The pet name ticket (4322,
    `!pet ticket`) renames the worn, awake pet: C2S 0x4D -> S2C 0xC0 to the owner ("Pet name has
    been changed.", one ticket used) and 0xB0 to the clients that show the pet; asleep, a wrong
    pet or a bad name gets S2C 0x73 {0} (the box closes, nothing used). `MALL_PETS` (default
    `true`) sells and gifts the pets and pet gear in the Spark Shop next to the food and the
    tickets.
    - Wear Picky, `!pet ticket`, use the ticket, type "Tweety": the wait box, then "Pet name has
      been changed."; the name tag reads Tweety on both clients. Put the pet to sleep and try
      again: the box closes with the name-in-use text and nothing crashes.
    - `!cash 30000`, `!mall`, buy Picky, Red Hood, Pet Food 20 and a ticket, move them to the bag
      (the pet asks "...it can't be moved to other characters."), leave: Picky is in the
      equipment tab at level 1, ready to wear; it cannot be moved back to the box.
16. P15 stage 4, guild-g6 (boards.py; offline in `test_guild_boards.py`). The Guild Plaza boards
    on map 9702. On the cp-2 exe (`CLIENT_ITEM_IDS "en"`) Moiba's "Purchase advertisement" sells
    the Guild Billboard (EN 4284, 1,000 gold; the dialog says 0 Gold because the EN hni lists the
    premium board 4283 there - the server sells the 1-hour board and says so in a chat line).
    The client sends this purchase with npc_id 0 (its dialog close zeroes the id first): the
    server takes a board id bought on 9702 as Moiba's. A guild master uses it in
    9702: board dialog, text, OK (C2S 0x88) -> the board appears for everyone on 9702 (S2C 0xBA)
    and the billboard leaves the bag. Boards last `GUILD_BOARD_MINUTES` (60; the cash Premium
    Guild Billboard 4283 `GUILD_PREMIUM_BOARD_MINUTES`, 1440), are kept in `GUILD_BOARDS_FILE`
    (`guild_boards.json`) across restarts and come back to anyone entering 9702 (S2C 0xBB). On
    the stock exe (`"kr"`) only `!guild board place` makes boards.
    - A (master of a guild, `!guild gmtag off`), on 9702: Moiba -> Purchase advertisement -> 1:
      "you've received Guild Billboard" and "Guild Billboard x1 bought for 1,000 gold.", gold
      -1,000 (log: `[BUY] Guild Billboard item=4284 x1 from guild NPC 181 Moiba ... (wire npc_id
      0: 0x474327)`). Double-click it, type "Join us", OK: the
      board stands at A's spot on A and B; one billboard gone from A's bag.
    - B (no guild) clicks the board -> the join dialog -> OK: "You have applied for this
      guild." (C2S 0x89 u16); A's notification icon appears.
    - `!guild board ttl <guild> 10`: after ~10 s the board disappears on both (S2C 0xB8).
    - Place one, B portals out of 9702 and back: the board is there again (0xBB). Restart the
      server, both return to 9702: still there.
    - `!guild board place <guild> <text>` / `premium` / `list` / `expire <guild>|all`.

## 5. Offline tests

`test_tooling2009.py` covers the following without a client:

- the layout tables against `client_map_2009.json`, and the PE timestamps against the exes
- the `--build`, `WS_BUILD` and config precedence (wsview and wsdev run in child processes)
- the 2009 command line through a port of the client's SSO parser, and its first field
  logging in on a 2009 server (fakeclient)
- the per-build memory reads of wsview, wsdev and the driver, plus a 2009 driver flow:
  position, facing, and a kill decoded with the 2009 spec
- the 2009 auto-login click order
- sendspec and cap with the 2009 spec

The login and world flows of the 2009 build are in `test_client2009.py` and
`test_world2009.py`; the P8 carry-ins C1-C8 (pet records, the worn pet in every 0x6F, the
KR -> EN item id shift, the rename hook, the 0x73 builder, the pet items through C2S 0x48, the
origin-3 box grant, the 2009-only cash opcodes) in `test_carryins.py`; the P15 pets (records'
pet block, the per-client pet info mirror, wear / take off, sleep / wake, the id switch, the Pet
Bell at the grocers) in `test_pets.py`; the pet tick, emotes, feeding, the Pet Bell, pet gear and
the pet auto-loot in `test_petlife.py`; the pet rename, the owner-rename hook and the pet tabs of
the Spark Shop in `test_petextras.py`; the Guild Plaza boards (Moiba's sale, placement, the
0x15 echo, the 0xBB on entering 9702, expiry, disband, restart, the stock-exe ids) in
`test_guild_boards.py`.
