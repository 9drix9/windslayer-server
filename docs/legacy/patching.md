# Client Binary Patches

The 2008 English `WindSlayer.exe` needs two patches to work with a local server. Apply these to a COPY of your legitimate client (don't distribute the patched binary).

## Patch 1: Redirect server IP to localhost

The client hardcodes the master server IP. Find and replace the 4 bytes of the server's IP address in the exe with `127.0.0.1` (bytes `7F 00 00 01` when stored in network byte order, or `01 00 00 7F` little-endian depending on context).

TODO: exact file offset — the original IP was `121.160.9.165` (Outspark's server, now dead). Search for bytes corresponding to that IP and replace.

## Patch 2: Suppress "No response from server" dialog

The client has an error dispatcher at VA `0x43ED30` — a jump table with 8 entries for different timeout/error conditions. Entry `[0]` normally points to `0x43E851`, which shows the "No response from the server" dialog and kicks the player to login.

Patch: change the DWORD at VA `0x43ED30` from `0x0043E851` to `0x0043E9DB`. The target `0x43E9DB` does minimal cleanup and jumps to a common return path — effectively making the "no response" error silent.

**File offset:** `0x3ED30` (assuming standard PE load at `0x00401000`).

**Hex edit:**
- Find at offset `0x3ED30`: `51 E8 43 00` (LE of `0x0043E851`)
- Replace with: `DB E9 43 00` (LE of `0x0043E9DB`)

### Why this matters

Without this patch, the client times out after ~30 seconds on the loading screen and shows an unhelpful dialog, then disconnects. With the patch, the client silently retries or falls through to login flow. Neither is ideal, but the patched version gives us more time to debug.

### Side effect

The patched exe's error recovery path sometimes triggers a spurious re-login (client sends `opcode 0x01` mid-flow). Our server handles this by **ignoring the second `0x01` if the session is already authenticated** — see `_handle_login` in `server/windslayer_server.py`.

## Verifying the patch

After patching, check file offset `0x3ED30` reads `DB E9 43 00`:
```python
with open('WindSlayer_patched.exe', 'rb') as f:
    f.seek(0x3ED30)
    print(f.read(4).hex())  # should print 'dbe94300'
```

The provided patched exe SHA-1: (fill in once a canonical build exists)

## Further patches you might try

### Skip map-load validation

This is the current project blocker (see `known_issues.md`). If you can find the conditional branch that checks the `.hmi` file validity in the client, patching it to unconditionally succeed would skip the map-load check entirely. The map-load code references format string `stage%02u_%02u.hmi` at VA `0x006F7DA6`, but that string has **zero direct references in `.text`** — it's accessed through an indexed string table, so finding the caller requires more work.
