# Retail WindSlayer videos compared with our server (EN 2009 Build 14)

Sources: 20 video catalogs; `project_status.md`, `reference_combat_model.md`, `reference_retail_videos.md` and `reference_en2009_client.md`; `IMPLEMENTATION_ROADMAP.md` (P0–P11); `RETAIL_COMBAT_FEEL_RE_2026-09-23.md`; and the last 300 lines of `LIVE_TEST_LOG.md`. I also checked a few numbers against the server code: `progression.py`, `hpmp.py` and `en_item_catalog.json` in `C:\Users\ohdri\Desktop\WindSlayer2Game\server\`.

## Which era each video shows

Our client, Build 14 (built 2009-01-23), is the Jan–Mar 2009 client.

- **Our client's era.** Bottom-right buttons read "Spark Shop / Avatar / PvP Etc / Option", with Msg/Gift/Report above them.
  - lSAXdVlJd8Y (Jan 2009 closed beta), fRrgsphUl8w (Feb 2009), W7DlmupK3EY (Mar 2009).
  - These three are the ground truth for what our client should look like.
- **Our 2008 client's HUD.** Buttons read "Premium Shop / Avatar / PvP Etc / Option".
  - uGkNIrv1cSw and b6B7pwhtUSw. They were uploaded in Feb 2009 but show the older build.
- **Later Outspark client.** Uppercase "SPARK SHOP / AVATAR / PvP / OPTION" plus 4 small round buttons. It adds the combo counter, grade words and a white loading screen.
  - Seen from about Jul 2009 to Sep 2010: q2szDnD1o4o, -VzQ0rYohBQ, yal_mcf74Bg, 2b70F9hk_Lc, 5FvE81G417w, y-ONQ8e0okM, HOZqk6jDPIc.
- **WindSlayer 2 (2011–12).** The world is spelled "Sesilia"; buttons are Char/Arena/System + Premium Shop.
  - r6TUkXgLgEk, Rlul2vQnLE8, 3Ia9cd9Lfg8, jJiOg5S_VqA, PY7ZyKArnYU, T7Ez9BGl-4Q.
- **Korean client:** 5UhUZp12BZ0, DRPIyKrUmvY, 8Ol-DThGCZ8.

---

## 1. Feature table

Era codes:
- **B14** = our client's era (Jan–Mar 2009)
- **08** = our 2008 client
- **O-late** = later Outspark (mid-2009 to 2010)
- **WS2** = the 2011–12 relaunch
- **KR** = Korean client

Status values:
- **done**: implemented. It may be offline-tested only, and the notes say so.
- **partial**: some of it is in, or it is not live-verified.
- **missing**: not in the server yet.
- **later**: needs client data or code that Build 14 doesn't have.

### Login and character

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| World/channel select: CECILIA, "Channel - 1..4", each with a green "Low" load label | 2b70F9hk_Lc 0:00 | O-late | partial | One channel is live. Retail ran 4 channels and showed a load label, which we set from the `u16 users` field of the channel entry. |
| "Waiting for the server to respond." while connecting | 2b70F9hk_Lc 0:02, q2szDnD1o4o 1:10 | all | done | Built into the client. |
| Character select: town plaza with a statue and fountain | 5UhUZp12BZ0 0:11 (KR) | B14/KR | done | Build 14 renders this screen live. The forest/cottage and winter-snow select screens are later clients. |
| "Character Info." popup on select: Total Rank "21103 Etc." -46, Class Rank, Arena Rank "PvP not yet played." | 2b70F9hk_Lc 0:04 | O-late | partial | Only the "not played" text is possible until PvP records exist (P9). Whether Build 14 has this popup is unchecked. |
| Character creation: Novice with fixed base stats (Kuzenko Lv5 STR13 INT2 DEX4 SPR6 = 25 points) | lSAXdVlJd8Y 0:58 | B14 | done | Our P1 stat rule gives 9 + 4 x 4 = 25 at Lv5, which matches. |
| Key-guide overlay: Jump [Up], Move, Move Down & Portal [Down], Item Pickup [W], Basic Attack [S], [SPACE] Use NPC & Object | fRrgsphUl8w 0:00 | B14 | unknown | Memory credits this to the Jan 2010 video, but it already shows in Feb 2009. We should check whether Build 14 draws it for a new character. |

### HUD and system text

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| Top-left panel: HP/MP/EXP bars, "LV n", class name, no numbers | every EN video | B14 | done | Built into the client. |
| 8 quickslots, page number, yellow down-arrow | fRrgsphUl8w, q2szDnD1o4o 1:59 (pages 1/2) | B14 | done | The hotbar is persisted (live-verified). |
| Minimap titled "Cecilia(channel-N)" with the map name | all EN | B14 | done | The channel number comes from our config. |
| Rotating "[Announcement]" tips | all EN | B14 | done | These live in the client file hs/windslayer.hbi. |
| Server broadcast "[Announce]Select Def. Wings are 45% today!! Check out the Wind Slayer Store!!" | HOZqk6jDPIc 6:10 | O-late | done | The GM `/not` command exists. No scheduled or automatic ad broadcasts yet. |
| Guild-battle broadcast "[Friday*] guild has won the game against [Rawr ] guild." | y-ONQ8e0okM 0:00-6:15 | O-late | missing | Needs guild battles. No roadmap phase covers them. |
| Buttons: Spark Shop, Avatar, PvP Etc, Option; Msg/Gift/Report | lSAXdVlJd8Y, fRrgsphUl8w | B14 | partial | They draw. Spark Shop (P8), Msg (P6 memos), Gift (P8) and Report (P7) have no server side yet. |
| Option tooltip "System Settings: Optimize your keyboard settings..." | uGkNIrv1cSw 2:35 | 08 | done | Built into the client. |
| System log lines: "You've received (+10) experience points.", "You've received Wooden Stick. (Count:1)", "Sold 6 Item (Green Leaf).", "Gave 5 item(Golden Leaf).", "You've learned skill(Strong Attack).", "[Quest] Quest Completed." | fRrgsphUl8w 0:13/0:38, HOZqk6jDPIc 1:55, y-ONQ8e0okM 9:26 | B14 | done | Our packets trigger the client's own strings (P2–P4). |
| "In channel 1." notice | — | — | done | Our 0x99 notice. |

### Stats and levelling

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| Status window numbers: Lv4 HP 116, MP 86, EXP 81/425; Lv5 HP 118, MP 88, EXP 67/700; STR 17 + 4 free at Lv4, 23 + 2 at Lv5 | y-ONQ8e0okM 4:27, 9:51 | O-late (same rules as B14) | done, verified | These match `EXP_INC` (425, 700), `hpmp.max_hp/max_mp` (116/86, 118/88) and `stat_total`. |
| Status window extras: Arena Win/Lose/KO/Down, Reputation, Rank "Trainee" with a fist icon, Manner, Couple "Does not exist" | y-ONQ8e0okM 4:27 | O-late | partial | Manner exists. Arena records are P9. Couples are not modelled anywhere. |
| Level up: blue comic "LEVEL UP!" text, then a "Level up" button in the Msg/Gift row (an orange "Lv.UP" button in 2010) | fRrgsphUl8w 1:29, y-ONQ8e0okM 3:17 | B14 | done | The client levels itself from 0x21. Observers get 0x22 (P5). |

### NPCs, quests and world

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| NPC tags: two green lines (name / title); "!" = quest available, green "?" = ready to turn in; ambient speech bubbles | fRrgsphUl8w 0:00, y-ONQ8e0okM | B14 | done | Built into the client from map and NPC data. |
| NPC dialog: big portrait, blue quest list, Accept/Decline | fRrgsphUl8w 0:04-0:09, y-ONQ8e0okM 9:06 | B14 | done | Offering a quest happens entirely in the client. |
| Elder's Test: Wooden Stick given once, 1 kill, "You are ready to complete the quest." | fRrgsphUl8w 0:13-0:38 | B14 | done | P0 exit criterion 6 and P2 live checks. |
| Adventurer's Token: 5 Golden Leaf, rewards the Strong Attack skill and +30 exp | y-ONQ8e0okM 9:26 | O-late | done (offline) | The type-3 skill reward is mirrored. Not live-verified. |
| Gangdalf's list: Mining/Gathering learn quests, class-curiosity quests, "Gangdalf's Recommendation" item | y-ONQ8e0okM 10:00-11:06 | O-late | done (offline) | Driven by the client's own .hqi quest data. |
| Class-change quest (Mage Guild Master: 20 White Feathers, 10 White Wings, 5 Monkey Hat Trinkets, Advanced Class License) | 2b70F9hk_Lc 2:25 | O-late | done (offline) | Handled by the P3 quest mirror of the job items. Not live-verified. |
| 2nd classes (Lv30): BeastMaster, Trapper, Assassin, Elementalist, Summoner, Bishop | q2szDnD1o4o, -VzQ0rYohBQ, 5FvE81G417w | O-late | done | `classchange.py`. The HUD labels match. |
| Portals: sparkle plus Down key; tip "To enter next area, press Down arrow key..." | fRrgsphUl8w 0:17 | B14 | done | Live-verified. |
| Map change: about a 1 s fade, no loading screen | fRrgsphUl8w 0:20 | B14 | partial | The P11 "single fade" goal applies. The white "Loading..." screen with the witch illustration is O-late. |
| World map (M) with legend: Center of Town, Normal/Boss Hunting Zone, Class Training Grounds, Blue/Orange portals | HOZqk6jDPIc 0:12 | O-late | done | Built into the client. Shows Foothill as a boss zone. |
| Town sets: Popola, Ozi, Monk Temple, Serien, Atajokuna, Balderan, Amakusa, Mage Town | lSAXdVlJd8Y, 2b70F9hk_Lc, 5FvE81G417w | B14/O-late | done | Map data comes from the client. Warps and portals work. |
| Event NPCs under a pink "EVENT" arch | lSAXdVlJd8Y 0:20 | B14 | partial | They render from map data. No event quests or rewards on the server. |
| Winter snow on fields and towns in Jan 2009 | lSAXdVlJd8Y 0:00-0:16 | B14 | unknown | Our exe references `hs/bg/field_snow.hsi`. This is probably a switch we can trigger, not missing art (RE in progress). |
| Village transfer via Area Shifter / Region Movement Guide "GaramMarin" | 2b70F9hk_Lc 0:12 | O-late | done (offline) | P3 stage 5. Live check of the fee and arrival point is still pending. |

### Monsters

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| Beginner monster "Ssiyo", +10 exp per kill | fRrgsphUl8w 0:21/0:38 | B14 | done, verified | Live: 10 exp. |
| Field mobs: Koring, Oraring, Poco, Hendove, Cuckoo, Pponyang, Monkey Soldier/Sergeant/Lord, Sunny Crab, B.H Swordsman/Pikeman | y-ONQ8e0okM, HOZqk6jDPIc, 2b70F9hk_Lc, 5FvE81G417w, yal_mcf74Bg | B14/O-late | partial | Spawns come from the map files. Only 101/102 and a few maps have been checked live. |
| Monsters wander, chase and attack when hit; monkey packs cluster on the player | uGkNIrv1cSw 0:49-1:30, 2b70F9hk_Lc 1:03 | 08/O-late | partial | The server AI (hate list, 0x2A chase words, 0x9E hand-back) is in. Swinging templates (monkeys) have not been live-verified. |
| Boss monsters: Monkey King (Ascetic Quarter), Monkey Lord (Village Byway, Rocky Valley 2), Bomb Rat (Foothill boss zone) | b6B7pwhtUSw, -VzQ0rYohBQ 0:00, HOZqk6jDPIc | 08/O-late | missing | Boss spawns, respawn timers and boss behaviour are unverified. |
| Monster name-box colours: black / blue / red (about 44 % alpha, so teal, green or orange over grass); Ssiyo grey-blue for a Lv1 player; Koring green vs Oraring orange for Lv3 | fRrgsphUl8w 0:21, y-ONQ8e0okM 1:15, yal_mcf74Bg | B14 | done natively, changed by our patch | This is exactly Build 14's level-gap rule (RE doc §2.1). Our AGGRO_RULES option A (black when idle, red when aggroed) is the KR/WS2 rule and replaces it. See section 4. |
| No monster HP bar, no level box | all B14/O-late | B14 | done | Correct as is. WS2 has both. |
| Respawn: continuous, maps always populated | y-ONQ8e0okM | all | done | About 12–15 s, live-verified. |

### Combat

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| Hit words CHUD / KRAKK / THUD plus orange starburst | fRrgsphUl8w 0:30, uGkNIrv1cSw 0:08 | B14 | done | Built into the client. |
| Damage digits: green on the monster (dealt), red on the player (taken) | fRrgsphUl8w, uGkNIrv1cSw, b6B7pwhtUSw | B14 | partial | The client draws them, but the number is its own estimate and can differ from our damage (FUN_00418ac0 port is still open). |
| Combo counter "N Combo!" (green, orange from about 22 up) | 2b70F9hk_Lc 0:41 (Dec 2009), y-ONQ8e0okM | O-late | later; done by our patch | Absent from all Feb–Jul 2009 footage. |
| Grade words GOOD (green), BAD (orange), CRITICAL! (orange-red) | yal_mcf74Bg, 2b70F9hk_Lc, y-ONQ8e0okM 2:09/3:51 | O-late | later; our patch differs | Our patch shows Good!/Great!/Wow! by combo stage. Retail shows GOOD/BAD/CRITICAL! (see section 4). |
| Big white/orange flash on skills; ice encasement (freeze); dizzy/stun icon over the monster | yal_mcf74Bg, 5FvE81G417w 4:22, uGkNIrv1cSw 2:14 | 08/O-late | partial | Skills and debuffs are in (P3, offline). Freeze and stun visuals are not live-verified. |
| Knockdown: monkeys lie flat, then get up | 2b70F9hk_Lc 0:41 | O-late | done | Client animation. |
| Jump + D Strong Attack technique | HOZqk6jDPIc 4:10 | O-late | partial | Needs the Strong Attack skill from Leon. The hit-report path is not live-verified for D. |
| Kill: X-eyes, fade, corpse gone | fRrgsphUl8w 0:35 | B14 | done | Corpse 0x06 about 3 s after death. |

### Loot and inventory

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| Loot lies on the ground; W picks it up | fRrgsphUl8w 0:55 | B14 | done (partial live) | P4 stage 2. W pickup is not live-verified in 2009. |
| "You don't have ownership of this item." on others' loot | 2b70F9hk_Lc 1:15 | O-late | partial | We protect loot for 15 s. Whether the client prints that message itself needs checking. |
| Loot messages that look like items went straight to the bag | 5FvE81G417w 4:56, y-ONQ8e0okM | O-late | n/a | Probably pet auto-loot (2009 send site 0x42EA76). |
| Bag window: Equipments / Consumables / Miscellaneous / Spark Items tabs, Gold, WSP, SC Item Inventory | fRrgsphUl8w 0:51, y-ONQ8e0okM 8:57 | B14 | partial | Gold and the bag work. WSP/Victy exists in the model. The cash tab needs P8. |
| Tooltips: Wooden Stick N+1/S+6; Wooden Spike N+5/S+12, 441g/91g; Wooden Sword Lv7, 602g/119g; Yellow Novice Hat 441g/88g | fRrgsphUl8w 0:52, HOZqk6jDPIc 2:03 | B14 | done | Read from the client's .hii data. |
| Equipment window including Pet slots and "Pet info." | fRrgsphUl8w 0:32, 5FvE81G417w 2:16 | B14 | partial | Pets are missing (2009 opcodes 0xAB–0xB2; no roadmap item). |

### Shops, bank and crafting

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| NPC shops: "ITEM SHOP" window, Purchase / Sale, "Are you sure to sell the items?" | HOZqk6jDPIc 1:20 | O-late | done | P4. |
| Skill-book shops (Bro' Singha for Monk, Establue for Mage): e.g. Double Kick 770g, Flying Kick 950g, Counterblow 1750g | 2b70F9hk_Lc 1:56-2:24 | O-late | done | Type-3 purchases teach the skill. |
| Bank (Mikomakisho) | lSAXdVlJd8Y 1:42, HOZqk6jDPIc 8:07 | B14 | done (partial live) | 2009 has no password dialog; we send 0x65 at entry. |
| Crafting: Johabriner, Composition/Forging Station, "Welcome to the world of Mineral Refining and Composition!" | HOZqk6jDPIc 8:15 | O-late | done (offline) | Not live-verified. |
| Gathering: Common/Imperial Herb, Common/Imperial Mineral nodes, Shovel Frog merchant | 5FvE81G417w 6:02, 7:51 | O-late | done (offline) | Not live-verified. |
| Monster card deck | 2b70F9hk_Lc 0:17 (menu) | O-late | done | P4 stage 4. |

### Social

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| Other players visible and moving; "Name : msg" chat with speech bubbles | all | B14 | done (offline) | P5 stages 2 and 4. Live two-client check pending. |
| Emote smiley over the head; macro lines ("Looking for Party", "Thankyou~") | uGkNIrv1cSw 1:09, 2b70F9hk_Lc 2:36 | 08 | done | The client handles these; they travel through chat. |
| Player right-click menu: Char. Info, Trade, Make Party, Whisper, Add as Friend, Converse, Copy Name, Praise, Report | lSAXdVlJd8Y 0:56 | B14 | partial | The popup opens. Every entry is consumed and does nothing yet (P6/P7). |
| Player Info window: Lv, Class, Guild, stats, Rank, Manner, equipment row | lSAXdVlJd8Y 0:58 | B14 | missing | `lc-player-info` (P6). |
| Whisper "<From: X> ..." | 2b70F9hk_Lc 2:36 | O-late | missing | P6. |
| Friends window "Community (9/20)" (later 39/50), online smiley, "yonah has logged in. (Friend)" | 2b70F9hk_Lc 0:17, 5FvE81G417w 4:58, 11:15 | O-late | missing | P6. Friend List Helper NPC: 50,000 gold per 5 slots, up to 50. |
| Messenger chat invite: "X would like to have a chat with you. Will you accept?" | 5FvE81G417w 5:00 | O-late | missing | P6 chat room. |
| Mentor tab (mentee gives the mentor EXP) | 5FvE81G417w 11:15 | O-late | missing | P6. |
| Party and party frames (two stacked HP/MP frames under the quickslots) | b6B7pwhtUSw 0:00 | 08 | missing | P6. |
| Guild: name and emblem under the player name, "Guild Master:Hello. Welcome to ... guild.", Guild Info window (Lv, members x/20, Guild Battle record, ranks Master/Soldier/Trainee), "(20) guild points are gained." | yal_mcf74Bg, 5FvE81G417w 10:50, 4:56 | O-late | missing | 2009 opcode 0xB3 (37 sub-commands) exists. **No roadmap item.** |
| Post box / memos | 5FvE81G417w, 2b70F9hk_Lc | O-late | missing | P6 memos. |
| Trade | lSAXdVlJd8Y 0:56 (menu) | B14 | missing | P7. |
| Private shop: "Shopping..." sign, Flea Market | Rlul2vQnLE8 (WS2) | all | partial | The stall window opens; the rest is P7. |
| Praise (manner) / Report | lSAXdVlJd8Y 0:56 | B14 | missing | P7. |

### Cash shop and events

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| Premium Shop NPC: "Click on me, and move to the Premium Shop!" | lSAXdVlJd8Y 2:42-3:38 | B14 | missing | P8. This is the retail way into the mall (see section 4). |
| Event popups: "Wind Slayer - Special EVENT!!", "EXP/ WSP X2 WEEKEND 12/19-20"; login gift "Love Potion x5" | 2b70F9hk_Lc 0:09, memory mrkmzLs5DcQ | O-late/B14 | missing | No event system on the roadmap. |

### PvP

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| PvP menu: Play & Chat / Battle Field / Arena / Guild Battle; tooltip "Battle Mode: Includes Arena, Battlefield, and Chat Room." | HOZqk6jDPIc 7:42 | O-late | partial | The windows open; the server refuses (P0 stubs). |
| Arena room list: "Waiting / Total 9/13", "NNN Name(cur/20) STD/ULMT/Novice/Lv.13", Quick Join / Create / Join | q2szDnD1o4o 1:10, -VzQ0rYohBQ 0:36 | O-late | missing | P9. |
| Room dialog "Select team & Status information": BLUE/RED 10 slots each plus WAITING; kills, class, heart/skull; Join Blue/Red, Viewing & Waiting, Leave Arena; status texts | q2szDnD1o4o 0:50, 5FvE81G417w 1:19 | O-late | missing | P9. |
| Round flow: READY, ACTION, "The round will start in N seconds", "Fight!!", scoreboard "BLUE n VS n RED" (players alive), timer 3:00, "[x] is down.", "Red WIN !", "Red team wins.", "[Blue0:1Red]" | q2szDnD1o4o, -VzQ0rYohBQ, 5FvE81G417w 0:17-2:12 | O-late | missing | P10. |
| Automatic balancing: "System will balance number of each team members next round." / "The latest red team member has moved to blue team." | -VzQ0rYohBQ 0:51 | O-late | missing | P9/P10. |
| Rewards: "+12Reputation Points, +12WSP, +12...", "The same amount of experience point you gained as fame point will be applied when you go back to the field." | -VzQ0rYohBQ 3:27 | O-late | missing | P10. |
| Leave dialog: "Will you exit the arena? If you select [Leave Now], you will not receive EXP and WSP gained this round." Leave Now / Reserve Quit / Cancel | 5FvE81G417w 2:26 | O-late | missing | P10. |
| Arena shows LV 49 for every player in STD rooms (field Lv 11/33/46) | q2szDnD1o4o, -VzQ0rYohBQ, 5FvE81G417w | O-late | done natively | The client's FUN_0041b830 sets level 49; this confirms that RE note. |
| Dead players become grey stone statues until the round ends | -VzQ0rYohBQ 1:03 | O-late | n/a | Client presentation once rooms exist. |
| Battlefield "Flag mode Ticket", "Standing by (5/12)", Abandon; "You already subscribed to join the battle." | -VzQ0rYohBQ 0:00-0:48 | O-late | missing | P9 (12-player match). |
| Play & Chat rooms: New Room (Title, Capacity 20, Password, "Puppet Theme"/"Ice Room Theme"), Playground room with "Exit Room", "Would you exit from play room?", room banner over the owner's head | 5FvE81G417w 6:44-7:30, W7DlmupK3EY | O-late/B14 | missing | P9 (`pvp-room-balloon`). |
| Guild Battle | 5FvE81G417w 10:50, y-ONQ8e0okM | O-late | missing | No roadmap item. |

### GM, pets, dungeons and WS2/KR-only items

| Feature | Where seen | Era | Status | Notes |
|---|---|---|---|---|
| GM tag "Game Master" in green | W7DlmupK3EY 0:12 | B14 | done | Live-verified, including after a map change. |
| GM client shutdown box: "The game client will be shut down as requested by administrator." | jJiOg5S_VqA 3:40 | WS2 | missing | P6 `/kick`. Whether EN has this text needs checking. |
| Pets (BeastMaster hawk companion, pet equipment slots) | q2szDnD1o4o 2:00 | O-late | missing | 2009 opcodes 0xAB–0xB2. No roadmap item. |
| Party instance dungeon: all members press Down at the portal | 8Ol-DThGCZ8 1:07 (KR) | KR | missing | 2009 has indun01-03 and room mode 5. P11. |
| WS2 HUD: target frame, quest tracker, 2-row quickslots, chat tabs, EXP bar along the bottom | r6TUkXgLgEk, PY7ZyKArnYU | WS2 | later | |
| WS2 tutorial cutscene, Nubeae Town, 2nd password, 12 character slots, Auto stat, full stat refund at 1st class change, Levitate, titles ("Arena Hopeful", "Beta Champ") | r6TUkXgLgEk, Rlul2vQnLE8, jJiOg5S_VqA | WS2 | later | |
| Lv60 3rd classes (Supreme Knight ... Dread Master), Lv99+ monsters | 3Ia9cd9Lfg8, DRPIyKrUmvY | WS2/KR | later | |
| Multi-colour damage (cyan, pink, gold), mob HP bar under the tag, mounts, Korean hit words | DRPIyKrUmvY, 8Ol-DThGCZ8 | KR/WS2 | later | |

---

## 2. What to build next, by roadmap phase

Everything here is aimed at a player who remembers Outspark in 2009.

**Now (finishing P4/P5): era fidelity at no cost**

1. **Make the name-box rule a choice, and default to the Outspark look.**
   - Retail Build 14 colours the box by level gap:
     - blue when the monster is L-1 to L+3;
     - red when above L+3;
     - black when below.
   - Evidence: a Lv1 Ssiyo is blue (fRrgsphUl8w); Koring shows green (a black box over grass) while Oraring shows orange (a red box) for a Lv3 player (y-ONQ8e0okM).
   - Ship Option B from the RE doc as the default: level colour while idle, red while aggroed. Keep Option A (the KR rule) as a WS2-look switch.
   - Before choosing, compare a frame of our client against fRrgsphUl8w 0:21.
2. **Match the grade words and combo counter to the footage (or turn them off for the B14 look).**
   - Grade words: GOOD in green, BAD in orange, CRITICAL! in an orange-red gradient. No Great! or Wow! appears in any video.
   - CRITICAL! goes with a big hit number; BAD seems to go with weak hits. So grades likely rate each hit, not the combo stage (inferred).
   - Combo counter: green digits, orange from about 20–22 hits. It keeps counting across different monsters and resets after a pause.
   - Build 14 footage (Jan–Jul 2009) has neither feature. Our patch flags can stay, but for "Outspark 2009" the default should arguably be off.
   - We have no BAD art. We need to make it or take it from a later client's data.
3. **Finish the pending live checks from the log:**
   - P4: W pickup, the ownership message, bank, crafting.
   - P5: two-client presence, shared mobs, chat bubbles, level and equipment broadcasts, and the G1 position numbers.
4. **Seasonal snow.** The Jan 2009 closed-beta footage (lSAXdVlJd8Y) shows falling snow and snowy roofs in Popola and Ozi. Finish RE workflow w0j3lxd0c on `field_snow.hsi` and expose it as a server or config flag.

**P3/P11: world depth**

5. **Bosses and boss zones.**
   - Monkey Lord appears among regular monkeys on Village Byway and Rocky Valley 2, and a party fights it.
   - Monkey King is the Ascetic Quarter boss; it chases and melees and has no HP bar.
   - Bomb Rat belongs to Foothill, which the world map marks as a "Boss Hunting Zone".
   - Needed: spawn rows from the map files and monster templates, long respawn timers, and live-verified melee attack commands (0x2A motion 05/06 or 15/16) for AI[0]/AI[8] templates such as monkeys.
6. **Monster melee.** In the video, packs of 4–6 Monkey Soldiers cluster and hit for red single-digit damage (e.g. '2', '4'). Ssiyo only does contact damage. Verify that our swing commands play and that 0x28 applies the damage.
7. **Client damage-digit formula** (FUN_00418ac0 / FUN_0041ace0), so the green number equals the HP the monster loses. Retail values: bare-handed 1, stick 2–3, Lv10 Monk about 40–80.
8. **One fade per map change** (P11 `world-single-fade`). Feb 2009 retail shows about a 1 s fade and no loading art.

**P6: communication and grouping**

9. **Whisper.** Lines appear as "<From: X> msg". Also the "is rejecting whispers" path.
10. **Friends and presence.**
    - Window "Community (n/20)".
    - Green line "X has logged in. (Friend)".
    - Friend slots expand up to 50 at 50,000 gold per 5 (the WS2 NPC text; EN showed 39/50).
11. **Messenger room invite.** Popup "X would like to have a chat with you. Will you accept?" with Accept / Refuse.
12. **Mentor tab**, with mentor EXP lines.
13. **Party.** Two stacked HP/MP frames under the quickslots (b6B7pwhtUSw), plus the party EXP split.
14. **Player Info window.** Name, Lv, Class, Guild, STR/INT/DEX/SPR, Rank with the fist icon, Manner, equipment row.
15. **Memos and the Msg button.**
16. **New roadmap item: guilds.** Not in P0–P11, which were designed for 2008. Needed:
    - 2009 opcode 0xB3 with its 37 sub-commands;
    - guild name and emblem under the player name;
    - the guild-master welcome line;
    - Guild Info window: "Lv.2 (4/20)", progress %, "Guild Battle 5Round 0Win 4Lose 1Draw", ranks Master/Soldier/Trainee, Kick / Guild Battle / Converse;
    - guild points on kills ("(20) guild points are gained." via the conditional field in 0x21);
    - guild-battle start/win broadcasts to all players.

**P7: exchange and reputation**

17. **Trade.**
18. **Private shops.** The owner shows a "Shopping..." sign; buyers browse and buy in the Flea Market.
19. **Praise and Report.** Praise changes manner. Report ties to the red Report button.

**P8: cash shop and events**

20. **Spark Shop entry.**
    - Retail entered the mall through town NPCs ("Luxary / Shop Assistant", "Click on me, and move to the Premium Shop!") and a Premium Shop building. Check that NPC's UI type before settling on `!mall`.
    - Also needed: Gift and the SC Item Inventory.
21. **New roadmap item: events.**
    - An EXP/WSP multiplier weekend with its notice.
    - An event popup at login ("Wind Slayer - Special EVENT!!"; the art may be later-client).
    - An event-prize gift delivered at login ("Love Potion x5").
    - Scheduled [Announce] ad lines.
    - Event NPC quests (Eventina Event Announcer).

**P9/P10: PvP**, which matters most to retail players (half of the Outspark footage is arena):

22. **Arena room list.**
    - Header "Waiting / Total a/b".
    - Rows "NNN Title(cur/20) MODE". Modes: STD (level set to 49 by the client), ULMT, Novice, "Lv.13" (level-limited, lock icon).
    - Buttons Quick Join / Create / Join.
23. **Room dialog.**
    - BLUE / RED columns of 10 slots each, plus WAITING.
    - Each slot shows kills (kept between rounds), class, and a heart or skull.
    - Joining mid-round puts you in next round: "You subscribed to enter red team. / You will be in red team from next round."
    - Spectating, and automatic team balancing each round.
24. **Rounds.**
    - Countdown "The round will start in 5..1 seconds", then "Fight!!".
    - READY and ACTION banners.
    - Timer 3:00; "BLUE n VS n RED" counts players alive.
    - Kill feed "[x] is down."; result "Red WIN !" / "Blue WIN !" and "Red team wins."; score line "[BlueX:YRed]".
    - The room dialog opens again automatically after each round.
25. **Rewards and leaving.**
    - Per round win: +12 Reputation and +12 WSP.
    - Field EXP converts to fame when you return to the field.
    - Leave dialog: Leave Now forfeits EXP and WSP; Reserve Quit leaves after the round.
26. **Battlefield Flag mode.** Ticket panel "Standing by (n/12)" with Abandon. Refusal "You already subscribed to join the battle." if you try to join an arena while queued.
27. **Play & Chat rooms.** Themes "Puppet Theme" and "Ice Room Theme", capacity 20, a password, "Exit Room", "Would you exit from play room?", and the room-title banner over the owner's head.
28. **Guild Battle mode**, which depends on item 16.

**Infrastructure**

29. **Four channels**, each with a load label, with players and maps kept separate per channel.

---

## 3. Later-client-only features, and whether a patch could fake them

| Feature | Seen in | Approximate with a small patch? |
|---|---|---|
| Combo counter and grade words | O-late, WS2 | Already patched in. Change the words to GOOD/BAD/CRITICAL! and add the orange colour at about 20 hits. BAD needs new art. The CRITICAL! rule needs a crit flag; the client's damage estimate may expose one. |
| Red name box while aggroed | WS2/KR | Already patched in (option A). Suggest making option B the default (section 2, item 1). |
| Monster HP bar under the tag, and a level box | WS2/KR | Hard. Build 14 has only the party-bar pool. It would need a code cave in the tag drawer at 0x43C300 that draws a bar from the monster's HP field. Possible, but medium effort. |
| Top-centre target frame (portrait, name, Lv, HP) | WS2/KR | No: needs new UI art and layout. Skip. |
| Multi-colour damage (per source, element or crit) | WS2/KR | A small patch in the digit popup (FUN_00431830) could pick an orange digit on crit. Low value. |
| White "Loading..." screen with illustration | O-late | Possible only if the art is in the 2009 data. Otherwise skip; the Feb 2009 fade is correct for our era. |
| Winter character-select screen, "Special EVENT!!" popup art | O-late | Depends on the snow RE. The popup could be imitated with an existing notice or dialog. |
| Uppercase SPARK SHOP HUD with small round buttons | O-late | No, and not needed: Build 14's HUD is the correct one for its era. |
| WS2 HUD, quest tracker, 16 quickslots, chat tabs, tutorial cutscene, Nubeae Town, 2nd password, 12 slots, Auto stat, stat refund at class change, titles, Levitate | WS2 | No. It would mean switching clients. |
| Lv60 3rd classes, Lv99+ zones, mounts, Korean lava maps | WS2/KR | No: no data in the EN 2009 client. |
| "LEVEL UP" golden light pillar | WS2 | No. Build 14's blue "LEVEL UP!" is correct for its era. |

---

## 4. Surprises and corrections

1. **The video-to-era mapping in `reference_retail_videos.md` needs updating.**
   - Build 14's own era is on video: lSAXdVlJd8Y (Jan 2009 closed beta, Spark Shop, PvP Etc) and fRrgsphUl8w (Feb 2009).
   - The "Premium Shop" videos (uGkNIrv1cSw, b6B7pwhtUSw) are not a newer build; they match our 2008 client's HUD.
   - The combo counter and grade words first appear in the later Outspark client (Nov/Dec 2009). They are not WS2-only.
2. **Our grade-word design doesn't match retail.**
   - No video shows "Great!" or "Wow!". Retail shows GOOD, BAD and CRITICAL!, and BAD is frequent. That points to a per-hit quality or critical grade, not a combo stage.
   - The RE doc's guess that BAD is actually Great! is contradicted: many clear frames read "BAD" in orange block letters.
3. **Name-tag colours: Build 14's native level rule is the Outspark look.** The KR rule we patched in (black when idle, red when aggroed) is the WS2 look. The "black box, dark teal over grass" seen in videos is the 44 %-alpha black box described in the RE doc.
4. **Our numbers match retail exactly.**
   - EXP per level: 425 at Lv4, 700 at Lv5.
   - Novice HP/MP: 116/86 at Lv4, 118/88 at Lv5 (+2 each per level).
   - Stat points: 9 + 4(L-1). Lv4: 17 STR + 4 free = 21; Lv5: 23 + 2 = 25.
   - Ssiyo: +10 exp. Elder's Test needs 1 kill.
   - (The WS2 numbers, HP 154 and STR 4/2/2/2, are a different, later formula.)
5. **The client's arena level-49 override is real.** Three videos show LV 49 in STD rooms against field levels 11, 33 and 46. Our server should not send the real level for room HUDs.
6. **Mall entry.** The roadmap assumes the EN client has no working mall button and plans `!mall`. Retail had a "Luxary Shop Assistant" NPC ("Click on me, and move to the Premium Shop!") and a Premium Shop building in every town. Check that NPC's UI type in the 2009 data before P8.
7. **The key-guide overlay** (W pickup, S attack, SPACE for NPCs) is already in Feb 2009 EN footage, not only Jan 2010. Check whether Build 14 shows it for a new character, and which flag triggers it.
8. **Snow** in the Jan 2009 closed beta matches Build 14's reference to `field_snow.hsi`. The seasonal look is probably a switch we can turn on, not missing art.
9. **The roadmap has gaps for 2009-only systems.** Guilds (guild points, guild battles, guild tags), pets, the blacklist and instance-dungeon rooms (0xC1/0xC2) have no phase. P0–P11 were written for the 2008 client, so a 2009 addendum is needed.
10. **Loot and ownership.** Ground loot with W pickup is correct for our era. The 2010 "item went straight to the bag" lines are most likely pet auto-loot, not a rule change. Retail showed "You don't have ownership of this item." when someone else stood on your drops; check our 15 s protection produces that message.
11. **Contact damage.** No Outspark-era footage shows untouched monsters to be harmless. Build 14 natively lets any touch hurt. The WS2 commentator calls early mobs "peaceful", which fits the later contact gate. The gate patch is fine for comfort, but it is a WS2-era rule; worth one flag with the other era toggles.
12. **Monsters.** Monkey Soldiers attack in packs, while Ssiyo, Koring and Poco are mostly contact-only. Boss-class monsters (Monkey Lord, Monkey King) spawn on normal field maps, and one boss, Bomb Rat, sometimes wasn't there, which suggests a long respawn. Our AI and spawn model currently treats every monster the same.