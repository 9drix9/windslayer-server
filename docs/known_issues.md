# Known Issues & Open Questions

## The big blocker: `stage12_85.hmi`

After the client processes our "enter world" response, it tries to load a map file:
```
Could not load MAP. (./hs/stage12_85.hmi)
```
This filename is built at runtime via `sprintf("stage%02u_%02u.hmi", major, minor)`. Format string is at VA `0x006F7DA6` in the English client.

**Neither the English nor the Korean game has `stage12_anything`.** We searched both binaries' embedded stage-name tables — 250 unique `stageNN_NN.hmi` strings, zero references to `stage12_`.

So `12` and `85` are **dynamically computed** from either:
- A field in our 0x2E response that we haven't identified
- A field in our 0x02 login response (6 per-character UINT32s + 14 USHORTs that we currently zero out)
- A client-side constant (seems less likely since the filename would be baked in)

### Why faking the file doesn't work

We tried:
1. Copy `stage01_01.hmi` → `stage12_85.hmi` — client tries to load it, gets further but errors differently (`Fireway (21) WSA(75)` = invalid parse)
2. Copy minimal `main99_01.hmi` (457 bytes) → `stage12_85.hmi` — same failure mode

The `.hmi` files appear obfuscated/encrypted. The first 0x50 bytes of `main99_01.hmi` and `stage01_01.hmi` are identical (a header signature starting with `"Sa\x61\x98..."`), and the rest is binary gibberish in the 0x80-0xFF range. Simple byte-swap doesn't reveal plaintext — probably XOR + some key.

### What to try next

- **Binary-patch the map-loader** to skip the file-exists check or force-load `stage01_01.hmi` regardless
- **Reverse the `.hmi` format** by XORing pairs of files and looking for patterns
- **Find where `12` and `85` come from** by probing memory right before the `sprintf` call (requires finding that call site, which is non-trivial — the format string at `0x006F7DA6` has zero direct references in `.text`, so it's accessed through an indexed string table)

## Non-determinism between runs

Some runs reach the loading screen, others crash immediately with various dialog messages. Specific crash manifestations we've seen:
- "Waiting for server to respond" followed by crash
- "Village transfer failed"
- "Shop is closed or adjusting" (random dialog during error fallback)
- "First purchase of wind cash" (random dialog during error fallback)
- Screen fades to black and freezes

The client cycles through error message IDs in an indexed table — the "content" of the dialog isn't meaningful, it's just the template the error handler happened to grab. Our job is to avoid the error in the first place.

### Error code patterns from client logs (at `./log/YYYYMMDDHHMMSS.log`)

- `Fireway (21), WSA(75)` — message-too-long or invalid parse
- `Fireway (21), WSA(36)` — `WSAEINPROGRESS`, UDP async operation stuck
- `Fireway (21), WSA(30)` — `WSAEACCES`, access denied on socket
- `Fireway (21), WSA(109)` — `ERROR_BROKEN_PIPE`
- Hundreds of identical errors in milliseconds = retry loop

`Fireway (21)` appears in ALL runs regardless of outcome — probably a specific send function. The WSA code changes based on which error the OS returned that run.

## UDP map-server protocol

The client broadcasts `opcode 0x11 + account_id` to `255.255.255.255:42907` expecting a map-server response. We can't bind port 42907 on localhost (the client has it), but we CAN send UDP packets TO the client's port 42907 from any source port — we confirmed this in the 2026-04-24 session (our packets were parsed, but opcode 0x11 happens to map to a "cash purchase bonus" handler on receive, so we got wrong dialogs).

We don't know:
- The correct response opcode (the map-server sends what?)
- The response payload format (map name? IP redirect? initial map state?)

## Double-parse avoidance

We send BOTH `0x2E` (world data) AND `0x2B` (scene commit) after the client's `0x2B` request. This appears to give the most stable flow (reaches loading screen reliably). Sending only `0x2E` works but doesn't trigger scene transition. Sending only `0x2B` doesn't populate world data. Sending `0x2C` (tried briefly) triggers `0x4305B0(session, 0x27)` which freezes the client.

## Mid-flow re-login

After receiving our `0x2E + 0x2B`, the client sometimes sends `opcode 0x01` (login) again with the same credentials. This appears to be an automatic recovery trigger (possibly from the binary patch we applied to suppress the "No response" dialog — it may fall through to login flow).

**Our fix:** if the session is already authenticated, we ignore the 2nd `0x01` instead of replying with a fresh character-list. Replying would reset the client to character-select. Ignoring keeps it stable.

## Things we've ruled out

- The `game_state` global at `[0x70E710]` is **not** the game state — it points to an item-database string table. Prior session notes had this wrong.
- The 600+ entries in the "list at `[game_state+0x5C0]`" are the item database (keys = item IDs), not a dialog-manager list.
- Offset `0x4CC` is on the session/socket object, not on `game_state` (confirmed empirically — reading `[game_state+0x4CC]` returns ASCII bytes of a string table, not a char pointer).
- The INT3 debugger approach consistently crashes Fireway. Only use `ReadProcessMemory`-based tools.

## What a working session looks like (the goal)

Based on what we can partially reproduce:
1. Login → character select ✅
2. Pick character → press Start ✅
3. Loading screen appears (but stuck at 0% because map can't load) ✅
4. Map loads ❌ (blocker above)
5. Character appears in the world, HUD renders, NPCs visible
6. Movement, chat, combat, etc.

We've seen the HUD render in one run (the Aug 2026-04-22 session) because the client got far enough before the map-load timeout. That run lasted ~60 seconds before the error dialog cascade started.
