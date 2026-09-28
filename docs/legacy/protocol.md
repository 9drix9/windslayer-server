# Fireway Wire Protocol

The English WindSlayer client uses the **Fireway networking library** (`Fireway.dll`) for all network I/O. The protocol is shared across versions of the game but opcode contents differ between English (2008) and Korean (2025).

## Servers

- **Version server** — TCP port `7011`. Accepts one connection, sends a version/channel-list response, closes.
- **Game server** — TCP port `7022`. **HARDCODED** in the client at VA `0x44080E` (`push 0x1B6E`). The port in the version response is **ignored**. Don't try to set it to 7012 like the protocol docs suggest.
- **Map server** — UDP port `42907`. The client broadcasts to `255.255.255.255:42907` after world entry and expects a map-server response. Architecture is multi-server in real deployments; on localhost we haven't figured out the handshake yet.

## Packet format

Every TCP packet has an 8-byte header:

```
+0  DWORD  header:   [ size(11 bits) | no_encode(1 bit) | seq(8 bits) | reserved(12 bits) ]
+4  DWORD  checksum/reserved (set to 0 on send)
+8  BYTE   opcode
+9  ...    opcode-specific payload (+7 bytes from now)
```

- `size` = total packet size including 8-byte header (max 0x7FF = 2047 bytes)
- `no_encode` (bit 11, mask 0x800) — when SET, packet uses `EncodebyArray` (static-table XOR). When clear, uses MT19937-based encryption. Client code reads this in `CSNSocket::GetHeader` (Fireway VA `0x10003810`).
- `seq` — monotonic sequence number, 0-255 rolling

## Encryption

Two schemes, selected by the `no_encode` bit:

### 1. EncodebyArray (pre-SetCodeKey, static table)
Uses a fixed 256-byte table embedded in the client. Every byte of body + checksum is XOR'd with `table[pos & 0xFF]`. Very fast, no per-session key.

**When used:**
- Server's key-exchange packet (opcode `0x5A`) — first packet
- Client's login (`0x01`) and enter-world (`0x2B`) requests — all pre-login packets
- In-world heartbeats (`0x0D`) from the client — observed empirically
- Our server's responses to these, mirror the client's encoding

Python port: `cencmsg.py` in the server folder — `CEncMsg.encode_by_array()` / `decode_by_array()`.

### 2. MT-based Encode (post-SetCodeKey, per-session)
After the key exchange, `CSNSocket::SetCodeKey(seed)` is called with the server's chosen seed. Client and server both seed a Mersenne Twister 19937 with that seed; subsequent packets use a stream derived from it.

**When used:**
- Our server's heartbeat echoes (we reply to `0x0D` with `use_by_array=False`)
- Most in-world server→client packets (theoretical; we haven't implemented any others yet)

Python port: `cencmsg.py` — `CEncMsg.encode()` / `decode()`.

## Handshake sequence

```
1. Client opens TCP to server:7011
2. Server sends version response (opcode 0x01, seq=1, NoEncode), closes
3. Launcher shows Start button
4. User clicks Start
5. Client opens TCP to 127.0.0.1:7022 (hardcoded, NOT the version-response port)
6. Server sends key exchange:
     opcode 0x5A, payload = INT32 seed, NoEncode + EncodebyArray
7. Client calls SetCodeKey(seed)
8. Client sends login:
     opcode 0x01, CHAR[41] username + CHAR[21] password, NoEncode + EncodebyArray
9. Server replies login response:
     opcode 0x02, account_id + char list, NoEncode + EncodebyArray
10. User picks character, clicks Start
11. Client sends enter-world:
     opcode 0x2B, 42-byte payload (zeros + "10.5.0.2\0" + 0xA79B magic + char name)
12. Server replies world data:
     opcode 0x2E, ~316-byte payload padded to 2000 bytes, EncodebyArray
     THEN also
     opcode 0x2B (same payload), EncodebyArray  ← scene commit trigger
13. Client enters scene, shows loading screen
14. Client starts sending 0x0D heartbeats every ~15s (18-byte payload)
15. Client broadcasts UDP 0x11 to 255.255.255.255:42907 (map-server discovery)
    ← we don't answer correctly, so we get stuck here
```

## Opcodes (server→client, client-side handler VAs)

See [`dispatch_table.md`](dispatch_table.md) for the full 130-entry dispatch. Highlights:

| Opcode | Handler VA   | Notes |
|--------|--------------|-------|
| 0x01   | 0x44D8CA     | login response |
| 0x02   | 0x44E38F     | second login step |
| 0x03   | 0x44EF72     | timer kill |
| 0x0D   | 0x004582B3   | default/unknown — but client DOES send 0x0D as heartbeat; we echo |
| 0x2A,0x2D | 0x44F762  | shared handler — checks `cmp byte [esp+0x43], 0x2E` |
| 0x2B   | 0x4503E1     | **enter-world** — calls scene_A (0x450791) + scene_B (0x450797) |
| 0x2C   | 0x4507EE     | **DANGEROUS** — routes to `0x4305B0(session, 0x27)` which freezes client in our synthetic state |
| 0x2E   | 0x450B6D     | world data — creates list entries via 0x483430, no scene transition |
| 0x2F   | 0x4551BE     | **DANGEROUS** — always shows "arena/play room excessive" dialog, can't echo |
| 0x5A   | 0x004576C3   | key exchange |
| 0x63   | 0x44D7AD     | effectively a no-op (just LeaveCriticalSection + ret), safe to echo |

## Heartbeat formats

Pre-world heartbeat: **27 bytes total** (18-byte payload + 8-byte header + 1-byte opcode). Includes a client-generated token.

In-world heartbeat: **31 bytes total** (22-byte payload). Grows by 4 bytes once the client considers itself "in scene". We observed this in a run that reached the cash-shop error state — the extra 4 bytes likely carry position or map state.

Our echo: reply with the same opcode (`0x0D`) and same payload, using MT encoding (`use_by_array=False`). Echoing with EncodebyArray or with zeros crashes the client's default handler at `0x4582B3`.
