# 2010 video features: falling snow and the key guide (RE 2026-09-24, workflow w0j3lxd0c)

## snow (present in 2009: partial, confidence high)

Most of the prior answer holds. Two findings change it. First, the user's video shows the winter snow in field maps, not only at character select, with repainted snowy scenery. Second, the server can trigger delivery of a snow patch through the client's built-in updater, although no game packet turns snow on directly.

What still holds (re-checked in code):
- The 2009 exe has no weather system. There is no weather opcode, no date or season check, and no weather attribute in the map format.
- field_snow.hsi / _a8.hsc ship with the client but no map uses them: 0 of 288 maps in 2009, 0 of 255 in EN 2008, 0 of 252 in KR 2025.
- The field_snow strings in the exe are only entries 1827-1830 of the updater file list, all at version 1.
- Falling snow comes only from a map layer: `<Layer front="1" flowY="N">` with field_snow tiles. The layer renderer's flowX==0 / flowY!=0 branch really exists in the asm (0x40593C-0x405A2A).
- A layer no larger than 800x600 gets a camera value of 0, and SpriteControl DrawSprite then applies no shift (0x1000574E-0x1000577E), so the layer stays fixed to the screen.
- Front layers are drawn after characters, only in states 3-6, and are skipped while [scene+0x1700] is set.
- The sprite has 8 frames of 200 ms. Each 300x300 cell holds exactly 8 flakes (not 8-10). The flakes only sway 2-5 px sideways and never move down; I re-measured this.

What the video shows ("Wind Slayer Gameplay - First Look HD", MMOHuts):
- It is the Outspark client (Cecilia channel-1, Spark Shop, Avatar and PvP buttons).
- Six-armed crystal flakes cover the whole screen on Popola Hill 2 (stage01_06, map name 10), The Beginning of the Adventure (stage01_01, map 5) and House of Adventure (stage01_04, map 8). Their style and size match field_snow.
- Trees, grass tops and bushes are snow-capped.
- The map geometry is the 2009 geometry:
  - The minimap in the video is the unchanged green minimap001 sprite 4, and its ledges and tree clusters match.
  - bg001 (mountain), cloud001 and the tile001 rock faces look the same.
- So the winter build kept the same map layouts. It repainted the textures under the same file names and added a snow layer to the .hmi files.
- The .hmi files must have been edited. The only other small animated sprite in stage01_06 is fx004, a 128 px portal effect at x=-9 and x=3882. It cannot put flakes across the whole screen.

What this means for us:
- The snowy scenery art does not exist in any client we have. tile001, tree003 and f_ob001 textures are byte-identical in the 2009, 2008 EN and KR 2025 clients and are all summer art. tree001 differs in 2008 EN but is identical in 2009 and KR 2025. KR 2025 adds no new winter .hsi files.
- The other unused art (f_ob012, f_ob006-2/-3, house012 = Halloween town, house013, t_ob003) is not winter art either.
- The proof-of-concept .hmi layer reproduces the falling flakes only. It will not reproduce the snowy ground and trees.
- The flakes in the video look sparser than the proof-of-concept's roughly 48 per screen. That is not conclusive: compression and white backgrounds hide many of them.
- The video's date is unknown. Outspark ran the game from the January 2009 closed beta to the August 2011 shutdown, so "Jan 2010" is not confirmed.

Smaller corrections:
- The position mod layer height is computed with FISTP in truncate mode (control word OR 0xC00), not round.
- The map's layer list head is at map+0x54 (list object at +0x50). A layer's tile list head is at +0x1C.
- A tile's hsi value is its 1-based position in SpriteLists; the Sprite "no" attribute is ignored. An out-of-range hsi reads NULL+0x108 and crashes.
- A Tile attaches to layer number `no` of its parent Layer, which must not exceed the layers parsed so far.
- The map's overall width and height are the maximum over all its layers, so an 800x600 snow layer changes nothing else.

Scratch outputs are in <local scratch>\re2010\snow_verify\:
- yt\*.jpg (video frames)
- mm_compare.png (2009 minimap vs video)
- flakes_compare.png
- popola_art.png (2009 summer art)
- unused_art2.png
- scan2.py --hs X --out Y (map and asset scan)

### Trigger chain

A. Drawing the snow (confirmed, unchanged from the prior answer):
1. The map loads: "%s%s%02u_%02u.hmi" with "./hs/" + "stage" or "indun" (FUN_00451960 sprintf sites; string 0x52E08C). Character select loads ./hs/main99_02.hmi (push 0x4521B4, call 0x4521BA), then EDI=5 at 0x4521E4 and FUN_004405B0 sets state 5.
2. FUN_00407800 parses the map:
   - CheckValid at 0x407B05, Decode at 0x407B1B.
   - Each SpriteLists name goes to OpenFromFile. The returned base index is stored at entry+0x108, and a failure aborts the whole map load.
   - Each Layer goes to FUN_00408A20 (w, h, front, flowX×1e-6, flowY×1e-6). The layer is appended to the list at map+0x50 and grows map+0x74/+0x78 to the maximum size.
   - Each Tile looks up its hsi by 1-based walk of the sprite list (to 0x408548), then attaches to layer[no] via [map+0x70] and FUN_00408AC0.
3. Each frame, FUN_0043E480 draws in this order:
   - FUN_004320B0 (back layers, front=0)
   - FUN_00432130, FUN_004330C0, FUN_00432E10, FUN_004332F0 (characters and objects)
   - if [scene+0x1700]==0: FUN_00432270, then FUN_004320F0 (front layers, front=1), FUN_0043BCB0, FUN_0043D0F0
   - UI passes
   Both layer calls require [gs+0xF18]-3 < 4.
4. FUN_004056B0, per tile:
   - camX = 0 if layer w ≤ 800, camY = 0 if layer h ≤ 600.
   - FLDZ at 0x4056CD is the real zero used in the compares, not decompiler junk.
   - flowX==0 and flowY!=0 (0x40593C / 0x40594A): two DrawSprite calls at y = trunc(t×flowY) mod H + pos_y and that minus H, with sprite = base + sprite - 1 and time = delay + t.
   - t = [scene+0x364] - [scene+0x420], a millisecond timer (0x42C920).
5. SpriteControl DrawSprite (0x100056F0): with camX=0 the shift is 0, and the frame time is looped by the sprite's duration (0x100057CD-0x100057E8).

B. How Outspark most likely shipped the winter look (missed by the prior answer), and the only server-initiated path:
1. S2C 0x01 on the version socket: u16 version_code is read at 0x451A28. If it is not 14 and not above 60000 (0x451A31), the client calls SendMessageA(launcher dialog, 0x401) at 0x451A51 and reads nothing else.
2. The launcher dialog proc FUN_00488190 handles 0x401 with CreateThread(FUN_00487F00) (push at 0x488ABC, call at 0x488ACB).
3. FUN_00487F00 downloads "G_UpdateList.h" from "http://cdn2.outspark.com/windslayer/update/" (0x52E548) using WinInet (FUN_00488D80).
4. FUN_00489280 / FUN_0040B9C0 parse the list: a header version goes to DAT_0054EBC8 (_atol), and name, version and time go into the list DAT_0054EB9C. The list header version must be above 14 (0xE < DAT_0054EBC8).
5. FUN_004893D0 compares each remote entry with the built-in list at 0x4CB2F8 (9429 × 35 B, versions at 0x51BC18). It downloads every entry whose version differs or whose name is missing. FUN_00488D80 creates directories and moves the file over the old one, so hs/*.hsc and hs/stage*.hmi can be replaced.
6. It then downloads WindSlayer.hau, deletes G_UpdateList.h, and runs ShellExecuteEx("./WSLauncher.exe", verb "runas" when the OS major version is 6, same command line) before closing.
The field_snow files at version 1 in the list, the stageXX .hmi entries at versions 1-3, and the repainted art in the video fit this delivery route.

### Server action

No game-socket packet or field turns snow on. The prior answer is correct that there is no weather opcode, no layer or sprite-load opcode, and that 0xA7 only activates SCRIPT_LISTS objects already in the map. Three things change:

1. A server-triggered delivery path exists: S2C 0x01 from the version server (port 7011).
   - Send version_code = any u16 other than 14 and at most 60000, for example 15. The client then starts its built-in updater and reads nothing else from the packet.
   - Serve over HTTP at <base>/: G_UpdateList.h with a header version above 14, listing each changed file with a version different from the exe's built-in list (for example "hs/stage01_06.hmi" or "hs/tree/tree001_a1.hsc"), plus those files and a WindSlayer.hau.
   - The base URL is hard-coded as http://cdn2.outspark.com/windslayer/update/ (0x52E548). You need a hosts or DNS redirect, or an .rdata patch of that 43-byte string to a same-length or shorter URL.
   - Caveats:
     - The version server gets no C2S data, so the server cannot tell a patched client from an unpatched one. The exe's built-in list never changes, so answering "not 14" every time traps the client in an update loop and it never reaches the channel list. The server must go back to 14 afterwards, or the patch must also ship a new exe with bumped list versions and a new version constant (CMP AX,0xE at 0x451A31).
     - WSLauncher.exe and the .hau format have not been reverse-engineered.
     - On Windows 10/11 GetVersionExA reports major version 6, so the relaunch uses "runas" and shows a UAC prompt.
   The practical route is still to ship the data with our patched exe.

2. The data patch itself (make_snow_hmi.py; the prior recipe is correct):
   - Append `<Sprite name="./hs/bg/field_snow.hsi"/>` as the last SpriteLists entry. The tile hsi value is that position. Bump Number_of_HSI; it is used as a divisor for the progress bar, so it must never be 0.
   - Append `<Layer no="L+1" width="800" height="600" front="1" flowY="60000" tile_num="6">` with 6 tiles at (0|300|600, 0|300).
   - Re-encode: raw = (plain - [E9,DE,E0][i%3]) & 0xFF, then append SHA-1(raw).
   - Both proof-of-concept maps (poc_hmi\main99_02.hmi, poc_hmi\stage01_02.hmi) re-verified: footer OK, hsi and layer numbering correct.
   - Consider 3-4 tiles instead of 6 if the video's lower flake density is wanted. Density is not measurable from the thumbnails.

3. To match the video you also need repainted winter textures. The video shows snow-capped trees, grass tops and bushes, but the map geometry, minimap, bg001 and cloud001 are unchanged. That art is absent from all three clients we have, so it must be made: snowy versions of tree001/tree003_a1.hsc, tile001_a1.hsc, f_ob001_a1.hsc and so on, keeping the same atlas regions and file names, with the .hsc1/.hsc2 fallbacks. No .hsi or .hmi change is needed for the textures.

### Evidence

- Video (oEmbed): 'Wind Slayer Gameplay - First Look HD' by MMOHuts. Frames maxresdefault/hq1/hq2 show the Outspark HUD (Cecilia(channel-1), SPARK SHOP / AVATAR / PvP / OPTION, Lv.UP) with six-armed flakes over sky, trees and ground. Minimap labels: 'Popola Hill 2', 'The Beginning of the...', 'House of Adventure'. Trees, grass tops and bushes are snow-capped.
- MapLngKo.lng: 5 The Beginning of the Adventure, 8 House of Adventure, 10 Popola Hill 2. MapInfo name=5/8/10 are stage01_01/stage01_04/stage01_06.hmi.
- Video minimap = minimap001_a1.hsc region 500,0,900,100 (sprite 4, mm_hsi=10 mm_sprite=4 in stage01_06): same ledge and tree layout, still green. So the map geometry was unchanged and the minimap texture was not repainted (mm_compare.png).
- 2009 art used by stage01_06 (bg001, cloud001, f_ob001, tree003, tree001, tile001) is all summer art (popola_art.png). MD5: tile001_a1/tree003_a1/f_ob001_a1/field_snow_a8 are identical across 2009, KR 2025 and EN 2008; tree001_a1 is identical in 2009 and KR 2025. KR 2025 adds only field_ob/indun_objects.hsi to the map-art folders. No winter art exists in any client.
- stage01_06's only other small effect sprite, fx004.hsi, is a 128x128, 16-frame portal effect used at pos (-9,784) and (3882,784) in the front layer 12. It cannot produce flakes across the whole screen, so the winter .hmi must have added a field_snow layer.
- Unused map-art .hsi in 2009 (scan2.py): bg013, bg013-1, bg015, field_snow, f_ob010, f_ob012, f_ob006-2, f_ob006-3, house012 (Halloween pumpkin houses), house013, tile002/013/015, t_ob003 (milestone005). Rendered in unused_art2.png; none is winter art.
- 288/288 2009 .hmi pass the SHA-1 footer. 0 contain flowY or field_snow. Retail maps already use front screen-fixed 800x600 layers (stage02_02, stage04_29-34: fx004 at 588,403). EN 2008 stage12_85 Layer 2 has flowY="-10000".
- FUN_004056B0 asm: FLDZ 0x4056CD. flowX==0 test FCOM [EDI+0x10] at 0x40593C (JP), flowY!=0 test at 0x40594A (JNP). FMUL [EDI+0x14], FLDCW with OR 0xC00 (truncate), FISTP qword, DIV by H [EDI+0xC]. y = rem + pos_y [ESI+0x10] (0x4059BE); second draw uses rem - H (0x405A23). Sprite = [ESI]+[ESI+4]-1 (0x4059C6).
- Camera: layer w>0x320 / h>0x258 gate at 0x4056FE / 0x40578C, else 0. SpriteControl DrawSprite 0x100056F0: camX<400 -> eax=camX-400; x += eax-camX+400, which is 0 for camX=0 (0x1000574E-0x1000577E). Frame loop at 0x100057CD-0x100057E8.
- FUN_00408A20: layer 0x28 B; [+0]=front, [+4]=1, [+8]=w, [+0xC]=h, [+0x10]=flowX, [+0x14]=flowY, tile CPList at +0x18 (head +0x1C). Appended to map+0x50 (head +0x54). map+0x74/+0x78 = maximum layer size.
- FUN_00407800 tile parse: hsi looked up by 1-based walk of the SpriteLists list (to LAB_00408548), base read from entry+0x108 (a NULL entry is dereferenced if hsi is out of range). Layer chosen by walking map+0x54 'no' times, bounded by the count at map+0x5C, then [map+0x70]=layer and FUN_00408AC0. Progress bar computes 300/Number_of_HSI.
- Draw order FUN_0043E480: 4320B0 -> 432130 -> 4330C0 -> 432E10 -> 4332F0 -> if [+0x1700]==0 {432270, 4320F0, 43BCB0, 43D0F0} -> UI. [+0x1700] cleared at 0x433320 and set at 0x43AC21 for local-player actions 0xA05-0xA0F (full-screen DrawRect 0xFF000000).
- Updater: S2C 0x01 version_code read at 0x451A28; if !=14 and <0xEA61, SendMessageA(...,0x401) at 0x451A51. FUN_00488190 handles 0x401 with CreateThread(FUN_00487F00) at 0x488ABC/0x488ACB. FUN_00487F00 downloads 'G_UpdateList.h' from 'http://cdn2.outspark.com/windslayer/update/' (0x52E548), requires list version DAT_0054EBC8 > 0xE, calls FUN_004893D0, downloads 'WindSlayer.hau', then ShellExecuteEx './WSLauncher.exe' with 'runas' if the OS major version is 6.
- FUN_004893D0 downloads each remote entry (DAT_0054EB9C) whose name matches a built-in entry (0x4CB2F8, 35-B stride, 9429 entries) with a different version (DAT_0051BC18), or that matches no entry. FUN_00488D80 uses WinInet InternetOpenUrlA and CreateDirectoryA/MoveFileA. Built-in versions: field_snow x4 = 1, stage01_01..06.hmi = 3, main99_0x.hmi = 1. Version histogram: 9069 at v1, 189 at v2, 163 at v3.
- protocol_spec_2009.json: S2C 0x01 VersionCheckAndServerList, where 'version rejected -> auto-updater' is already documented. No C2S on the version socket, so the server cannot tell patched clients apart. S2C 0xA7 only activates map SCRIPT_LISTS objects (FUN_00406550 / FUN_004063B0).
- No other route to load field_snow: OpenFromFile callers use fixed names or '%s%s%03d.hsi' / '%s%s%04d.hsi' item patterns ending in digits. Effect spawns (FUN_0042CD90 into the scene+0x430 list) use hard-coded global sprite ids. '/rain' at 0x53E9DC is an emote command next to /unpleasant, /pleased and /smile.
- gameoption.cfg (SnowCurveEffect) is present in the install, but the only exe hits are the updater list entry. It is not read by the game.
- field_snow.hsi: mode 1, motion_num 9, line_num 3, 300x300, center 0 0, repeat 1, 8 frames x 200 ms. Centroid tracking: 8 flakes per cell, Y constant, X sway ±2-5 px (for example 206->203->210). Flakes in the video are about 12-25 game px, consistent with field_snow.


## keyguide (present in 2009: yes, confidence high)

I could not refute the earlier answer. Every code claim I re-checked holds, and I found only small corrections and one better choice of spawn point. The key guide is a map tile, not a UI overlay. It is Layer 7 (front="1") Tile 5 of hs/stage01_01.hmi (map 101, "The Beginning of the Adventure"): sprite 13 = ./hs/town_ob/basic_key.hsi, drawn at world (367,484), size 229x252. It is drawn every frame in world space with the other front layers, relative to a camera that follows the local player, so it shows whenever the local player is on map 101 with the camera near the left of the map. No packet field, quest, option or first-login flag controls it.

What I re-checked:
- I decoded the 2009, 2008 EN and KR copies of stage01_01.hmi myself. All three have the identical basic_key tile. The files differ only in other tiles: the 2009 portal-effect tiles are at x=1359 instead of 1411, and KR has a different background y.
- I scanned all 288 .hmi files in the 2009 client. Only stage01_01.hmi references basic_key.
- Layer visibility is only ever set: FUN_00408A20 sets layer[1]=1, and no code clears it. The tile draw loop FUN_004056B0 has no per-tile visibility flag.
- The only other writer of game+0x1700 is the local-player skill blackout, animation range 0xA05-0xA0F (0x43AB7F-0x43AC21), which also resets it to 0 at 0x433320.
- The map's SCRIPT_LISTS system can act on layers and tiles, but map 101's is empty.
- Every other map-load call site loads main99_01, main99_02, preview99_01 or indun maps, or reloads the saved field map after a PvP room kick. None hard-codes stage01_01.
- The exe has no tutorial, guide or key-guide string or UI.
- The 0x07 handler reads f64 pos_x/pos_y into entity+0x1298/+0x1328 (0x453AA5/0x453ABF). FUN_00422FC0 then converts them to integers at +0x165C/+0x1660 (0x4233E9/0x423420) before the game mode becomes 6. So the camera's first-frame snap lands on the sent position, with no pan in from the left.

Corrections:
(1) The 0x10A8/0x10AC/0x1700 offsets belong to the game object at 0x54EBD0 (`this` of FUN_0043E480). The mode is [game+0x4F0]+0xF18. This only changes names, not results.
(2) The 0x23-stride name table that holds basic_key and stage01_01.hmi (0x4D12CF) is not an integrity table. It is the auto-updater's file/version list: 9418 names from 0x4CB2F8, with a parallel u32 version array at 0x51BC18. FUN_004893D0 compares it with the downloaded patch list DAT_0054EB9C (name at +4, version at +0x108). It is not a content hash, so removing the tile from stage01_01.hmi still only needs the file's 20-byte footer, which CheckValid verifies, to be regenerated.
(3) New point: map 101 has its own start-point marker. Layer 6 Tile 6 at (200,900) has event="3", and its floor line gives the point (250,912). Every town map stageNN_01 has exactly one event-3 tile. The client uses event 3 only for PvP/room team starts (FUN_004296A0 → FUN_004073B0(map,3,…), only when game+0xF40 != 0). It uses event 1 only for C2S 0x7E (FUN_0042E790 → FUN_00407350). So in the field the client ignores the marker, and it is most likely the retail server's town start/revive point. Retail new characters may well have spawned at (250,912). There the camera is clamped to the left edge, so the whole panel sits at screen x 367-596, y 84-336. Walking right toward Murubisiri moves it to the video-style left position (x 17-246 at x=750).
(4) Murubisiri stands at (750,912), the middle of his line, not at (700,900).

I could not watch the new video ("Wind Slayer Gameplay - First Look HD", MMOHuts); I only got its metadata. Since the same tile ships in every build we have, any version's video that shows the guide on the starting map fits this.

### Trigger chain

All VAs are in the 2009 exe and I re-read each one in asm/decomp.
1) S2C 0x03 handler inside OnReceive 0x451960. At 0x452AB7 GetDataFromPacket reads the u16 map_code into [ESP+0x100]. At 0x453231 it does MOVZX / IDIV 0x64 and pushes "stage" (0x4CAC98), "./hs/" (0x5268C8) and "%s%s%02u_%02u.hmi" (0x52E08C), then calls _sprintf at 0x45325A. At 0x453269 it frees the old map (FUN_00407640), and at 0x453281 it calls the map loader FUN_00407800. The "Could not load MAP." error path is at 0x453292. Map 101 → ./hs/stage01_01.hmi.
2) Map loader FUN_00407800. Layer attributes: "width" 0x408386, "height" 0x408397, "front" 0x4083AB, flowX/flowY 0x4083BC/0x4083E6. At 0x408430 it calls FUN_00408A20, which builds the layer node [0]=front, [1]=1 (visible), [2]/[3]=width/height, [4]/[5]=flow speeds, and updates map+0x74/+0x78 (the largest layer width/height, 1600x1000 for map 101). Tile attributes are read at 0x408460-0x408519; NpcId is read only when event==2 (0x40850F). At 0x4085C7 FUN_00408AC0 appends the tile, with no condition on sprite, map, quest or flags. SCRIPT_LISTS is parsed at 0x4085F2 and is empty for map 101. FUN_00406680 (0x4088ED) builds collision lines from the tiles.
3) Per-frame draw FUN_0043E480 (this = game object). It returns early if mode == 7. Otherwise it calls FUN_004320B0 (back layers, front=0), FUN_004332F0, then `if (game+0x1700 == 0) { FUN_00432270(); FUN_004320F0(); ... }`, and draws the UI afterwards (FUN_00490840). FUN_004320F0 checks `[game+0x4F0]+0xF18 - 3U < 4` and calls FUN_004056B0(map=game+0x4FC, camX=game+0x10A8, camY=game+0x10AC, front=1, t=game+0xE40). FUN_004056B0 loops over layers with [1]!=0 && [0]==front. For layers wider than 800 the effective camera centre is 400 if camX<400, layerW-400 if camX>mapW-400, and otherwise (layerW-800)/(mapW-800)*(camX-400)+400 (asm 0x40570D-0x405785; the same for y with 600/300). For Layer 7 (1600 = map width) that is camX clamped to [400,1200]. With no flow speeds, the tile is drawn at its position: DrawSprite(..., camX_eff, camY_eff, 1600, 1000). game+0x1700 is set to 1 only at 0x43AC21 (local-player animation in 0xA05-0xA0F) and cleared at 0x433320.
4) Camera FUN_00431B20. When the mode is not 6 it sets 0x10A8/0x10AC = 0x80000000. In mode 6 it snaps (FUN_00432000) to (entity+0x165C, entity+0x1660 - 100) on the first frame, then follows with a spring. The followed entity is [game+0x4F0]+0xF00, set to the local player by FUN_00422FC0 from the 0x07 record. The 0x07 finalisation FUN_004405B0 sets mode 6.
Screen position of the panel: x = 367 - (clamp(px,400,1200) - 400) and y = 484 - (clamp(py-100,300,700) - 300). Fully visible for px ≤ 767, partly visible for 767 < px < 996, gone for px ≥ 996. For every py from 800 to 912 the camera y clamps to 700, so the panel sits at screen y 84-336.
There is no other trigger. Event-3 marker tiles are read only by FUN_004296A0 in room/PvP mode (game+0xF40 != 0), and the ↓ key sends C2S 0x7E only for event-1 lines (FUN_0042E790 line 1166 → FUN_00407350).

### Server action

There is no flag to send. Show the guide by putting a new character on map 101 near the left of the map, using the normal enter-world burst (0x08 → 0x03 → 0x07 → 0x0A/0x28/0x44 → 0x1A):
(a) S2C 0x03: map_code (the u16 right after the u8 channel_id) = 101, bytes `65 00`.
(b) S2C 0x07, the local player's own record (uid == login uid): f64 pos_x ≤ 767 and f64 pos_y from 800 to 912.

Two good start points:
- (250.0, 812.0). This is the retail marker: the event-3 line on Layer 6 Tile 6, 100 px above the ground line at 912. The 100 px lift follows the portal-arrival convention, e.g. 102→101 lands at (1411,714) above the 814 ledge. The panel then sits at screen (367-596, 84-336).
- The earlier answer's (700.0, 812-912), right beside Murubisiri at (750,912). The panel then sits at screen (67-296, 84-336).

Both are on the solid ground line (floor=1 tiles at y 900 → line y 912, across x 0-1600). Sending y=912 exactly or y=900 also works.

How to apply it in this server: the new-character point comes from START_X/START_Y. Override them in server/config.json with "START_X": 250.0 (or 700.0) and "START_Y": 812.0.
- Do not change the defaults in config.py: test_store.py line 194 asserts (101, 1411.0, 714.0). store.py DEFAULT_RECORD also hard-codes 1411/714, but record_defaults() overrides it from the config.
- Existing characters keep their saved x/y, so only new characters get the new point.
- Side effects:
  - combat.revive_point makes START_MAP/X/Y the revive point for deaths on map 101 and on unrouted/instance maps, so those players will also land in front of the guide. That matches retail.
  - _warp_point uses it for a GM warp to 101 with no position.

Taking it away: no packet or field removes it. It disappears when the player leaves map 101 through the event-1 portal tile (Layer 5 Tile 27, one-way ledge line y 814 at x 1355-1455, value_num 102). On later logins you send the saved map/position. It also disappears if the player stands at x ≥ about 996 on map 101. Returning to the left of map 101 always shows it again, as in retail. The only permanent removal is a client-data edit: delete Layer 7 Tile 5 from stage01_01.hmi and regenerate its 20-byte CheckValid footer. The exe's name/version table is only the updater's patch list, not a hash check.

### Evidence

- I independently decoded the 2009, 2008 EN and KR stage01_01.hmi (additive key E9 DE E0, footer dropped). The decoded XML and scripts are in scratchpad\re2010\keyguide_verify\ (dec.py --inp --out). All three contain `<Sprite no="13" name="./hs/town_ob/basic_key.hsi" />` and `<Layer no="7" width="1600" height="1000" front="1" tile_num="5">…<Tile no="5" hsi="13" sprite="1" pos_x="367" pos_y="484" />`. They differ only in other tiles: 2009 Layer 6 Tiles 3/13 fx at x=1359 against 1411 in 2008 and KR, and a KR background y of 303 against 276.
- scan.py over all 288 2009 .hmi files finds basic_key only in stage01_01.hmi. The 2009 basic_key.hsi decodes to one 229x252 sprite, center 0 0, line_count 0. The .hsc/.hsc1/.hsc2 quality variants all exist (16357/12430/14049 bytes), so a graphics-quality option does not remove it.
- FUN_00408A20 creates layers with [1]=1 (visible), and no function clears it. I checked every function that walks map+0x54: 004056B0 (draw), 00406680 (build lines), 00407640 (free), 004073B0/00407530 (line lookup), 00405D20 (minimap). FUN_004056B0 has no per-tile visibility check.
- The only writes to game+0x1700 are 0x433320 (clear) and 0x43AC21 (set). 0x43AB7F/0x43AB89 gate the set on animation 0xA05..0xA0F, and 0x43AB9F requires the local uid.
- FUN_004320F0: `if ([this+0x4F0]+0xF18 - 3U < 4) FUN_004056B0(this+0x4FC, this+0x10A8, this+0x10AC, 1, this+0xE40)`. FUN_0043E480 calls it only when this+0x1700 == 0.
- FUN_004056B0 camera clamp asm at 0x4056FE-0x405785 (x) and 0x40578C-0x405813 (y): if layerW > 0x320: camX<0x190 → 0x190; camX > mapW-0x190 → layerW-0x190; else ftol((layerW-800)/(mapW-800)*(camX-400))+400.
- 0x07 position: 0x453AA5 reads the f64 into entity+0x1298 and 0x453ABF into +0x1328. FUN_00422FC0 (called at 0x453AD2) stores the integer positions (0x4233E9 → +0x165C, 0x423420 → +0x1660), so FUN_00431B20's first mode-6 frame snaps to the sent position.
- Map 101 event-3 marker: Layer 6 `<Tile no="6" hsi="7" sprite="2" pos_x="200" pos_y="900" floor="1" event="3" />`. tile001 sprite 2 has `line: 0 0 12 100 12`, which gives the point (250,912) by FUN_004073B0's midpoint rule. scan3.py: exactly one event-3 tile on each of stage01/02/04/05/06/07/08/09/10/11_01, 20 on each PvP arena stage98_01-04. The client consumes event 3 only in FUN_004296A0 line 101 (inside `if (*(param_1+0xf40) != 0)`, team 1/2 starts). The ↓ portal path FUN_0042E790 line 1166 sends '~' (C2S 0x7E) only when the event == 1.
- Murubisiri: Layer 6 Tile 12 at (700,900), event=2, NpcId=75. FUN_00445970 places NPCs with FUN_004073B0(map,2,…) at the line midpoint: tile001 sprite 3 `line: 0 0 12 100 12` → (750,912).
- The table at 0x4CB2F8..0x51BBF4 (stride 0x23, 9418 names including hs/stage01_01.hmi at 0x4D12CF and basic_key at 0x516267) is walked by FUN_004893D0 (MOV ESI,0x4cb2f8 at 0x489472). It compares each entry with the patch list DAT_0054EB9C (name at +4, version at +0x108) using u32 versions at DAT_0051BC18. That makes it the updater's version list, not an integrity hash.
- Server context: config.json and config.py set START_MAP 101 and START_X/Y 1411/714, which match PySlayer gamedef maps row (102, portal 26 → 101, 1411.0, 714.0), i.e. the portal arrival point. combat.revive_point(start=...), windslayer_server._warp_point and store DEFAULT_RECORD/record_defaults all use START_X/Y. test_store.py:194 asserts the defaults.
- Video: the new link __xieit9aWc resolves (oEmbed) to "Wind Slayer Gameplay - First Look HD" by MMOHuts. I could not fetch its frames or description.

