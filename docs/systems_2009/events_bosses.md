# Events and bosses: server design (EN 2009 client, Build 14)

Client: EN Outspark v1.04 Build 14 (2009-01-23). Group: `events_bosses`, two content systems that share the NPC/monster template table `hs/windslayer.hni` and the map files.

Sources:
- Ghidra corpus `re_tools/corpus_2009` (decomp + asm). Every VA below is a 2009 VA.
- `corpus_2009/protocol_spec_2009.json`. All grammars are quoted from it.
- Decoded client data, decoded into COPIES under `docs/systems_2009/_work/events_bosses/` with `dec.py --in --out` (the install was only read):
  - `windslayer.hni` (204 NPC/monster templates)
  - `windslayer.hqi` (291 quests)
  - `windslayer.hii` (4322 items)
  - `windslayer.hui` (1245 windows)
  - all 288 `.hmi` maps
  - the plaintext `NPC/QST/UI/ITM/MapLngKo.lng`
- `RETAIL_VIDEO_SURVEY_2026-09-24.md` and `RETAIL_VIDEO_CATALOG_2026-09-24.json`.
- `MONSTER_AGGRO_RE_2026-09-23.md`, `RETAIL_COMBAT_FEEL_RE_2026-09-23.md`, `DAMAGE_FORMULA_2026-09-25.md`.
- 2008 specs `systems/premium_cash.md` and `systems/quest_cards_misc.md`.
- PySlayer `gamedef.sqlite3`, the newer KR build. It is used only for comparison.
- Server code in `WindSlayer2Game/server`, read only.

**Legend.**
- **VERIFIED**: read in the 2009 code (the VA is given) or in a decoded 2009 data file (the file is named).
- **INFERRED**: a deduction, retail-video evidence, 2008 behaviour that was not re-read in 2009, or a design choice.

Every claim carries one of the two marks.

Work files (all in `_work/events_bosses/`, all scratch):

| File | Content |
|---|---|
| `hni_2009.json` | parsed NPC/monster templates with names, titles, AI, drops |
| `spawns_2009.json` | every event-2 tile of every map (NpcId, `value_num`, position) |
| `boss_catalog_2009.json` | the 12 spawn tiles with `value_num` >= 300, with stats and drops |
| `ai_table.txt` | aggression classes of every monster template |
| `hqi_2009.json`, `hii_2009.json`, `hui_2009.json` | parsed quests, items, windows |
| `ui011_event_news.png` | the art of the Event News window (hui 1019) |
| `eventina_npc136.png` | Eventina's NPC sprite, which includes the EVENT arch |
| `video_hits.txt` | the retail-video observations about events and bosses |

---

## 0. Summary

1. **Build 14 has no event system in the protocol.** [VERIFIED]
   - No opcode carries an event flag, an EXP multiplier or an event time.
   - The 2009 S2C 0x03 grammar ends at the etc bag list. The newer KR 0x03 has a trailing "Event time" u32 (PySlayer `server_packets/opcode_0x03.py:71`), and the EN client does not read it.
   - No string contains "x2", "double exp" or "WSP".
   - Events therefore have to be built from existing pieces: text notices, item grants, quest pushes and one reused window.
2. **Eventina (hni 147) is a client-side NPC whose click opens the "Pop-Up (Event News)" window 1019. Nothing is sent to the server.** [VERIFIED]
   - The window art is fixed in `ui011_a1.hsc`: "WIND SLAYER CLOSE BETA EVENTS", Race to Level 10 and Bug Slayer, Jan 7-14 2009.
   - The pink EVENT arch is part of her NPC sprite.
3. **Nicolas (hni 76, "Self-styled Handsome") is the turn-in NPC of 27 event quests.** [VERIFIED]
   - 26 of the 27 have SNPC 0; 239 starts at Fiona. The 2009 offer list skips SNPC 0 (FUN_0048ac00).
   - So these quests can only be started by a server push (S2C 0x26). [VERIFIED code, INFERRED design]
4. **The "Wind Slayer - Special EVENT!!" login popup (Dec 2009 video) does not exist in Build 14.** [VERIFIED by string and art search]
   - S2C 0x80 {1, 0x3FB} opens window 1019 through the same call the Eventina click uses. [VERIFIED code; live test needed]
5. **Event gifts have three client paths.** [VERIFIED]
   - S2C 0x99 sub 9 puts the item straight into the bag.
   - S2C 0x6C with box-record origin 3 shows "Congratulation. You received an event item..".
   - S2C 0x6D lights the HUD Gift icon.
   - Love Potion (item 1282, Cash 0) can only use 0x99. The gift inbox accepts only cash items or WS Gift Certificates; anything else stalls the queue.
6. **"Love Potion x5" is exactly the reward of event quest 63 "Let's Gather Chocolate"** (Nicolas; 15 Sweet + 15 Smooth Chocolate). [VERIFIED data]
7. **The data has no boss flag.** [VERIFIED]
   - Field bosses are the 11 map tiles with `value_num = 300`: Rynx, Monkey King, 2 x Wasablanca, Leo Wolf, Drill Mole, Wook, King Frog, Waterfrog, Firefrog and Blue Shark.
   - Each boss drops a quest trophy at rate 99990.
   - Reading 300 as a 300 s respawn is INFERRED.
8. **Monkey Lord is not a boss.** [VERIFIED data] It is a Lv 12 regular monkey with 53 tiles on 7 maps, `value_num` 25-50.
9. **"Bomb Rat" does not exist in Build 14.** [VERIFIED] The Foothill boss is Rynx: quest 6 says "Defeat Rynx in Foothill". Its sidekick is the Kamikaze Rat.
10. **The client has no boss HP bar, no monster title and no boss announcement.** [VERIFIED] Name-tag colour comes only from the level gap. [VERIFIED]
11. **Aggression per the data:** [VERIFIED]
    - 17 templates are contact-only.
    - The 4 monkey types swing attack A only.
    - No field template has proximity aggro (only the indun Slow Peach has it).
    - Nothing in the client implements pack assist.

---

## 1. Opcodes used by this group

The group owns no opcode. [VERIFIED] It is built on opcodes specified elsewhere; this table lists the ones the design uses. Grammars are from `protocol_spec_2009.json`.

| Op | Dir | Name | Grammar (2009) | Role here | Status |
|---|---|---|---|---|---|
| 0x15 | S2C | SystemMessage | `u8 msg_type; u8 text_len; str[text_len] text` (text_len <= 88) | Event and boss announcements. msg_type 2 also shows a centre-screen notice (0x456743) | VERIFIED |
| 0x21 | S2C | ExpDelta | `i32 exp_delta; if(exp_delta>0 && own guild id>=2) u32 guild_points` | Where an EXP multiplier becomes visible | VERIFIED (0x459063, 0x459216) |
| 0x26 | S2C | QuestAccepted | `u16 quest_id` | Push an event quest into one of the 5 log slots | VERIFIED spec |
| 0x27 | S2C | QuestCompleted | `u16 quest_id` | Event and boss quest turn-in | VERIFIED spec |
| 0x59 | S2C | QuestProgressUpdate | `u8 slot; if(slot<6) u8 progress` | Boss kill counts (quests 165/181/182/184/205/215) | VERIFIED spec |
| 0x16 | C2S | QuestAcceptRequest | `u16 quest_id` | Accept button (event 2) in a dialog chain | VERIFIED spec |
| 0x17 | C2S | QuestCompleteRequest | `u16 quest_id` | Nicolas/Mei/... turn-in (FUN_0048a510) | VERIFIED (0x48A510) |
| 0x80 | S2C | SecondPasswordResult | `u8 result; if(result==1) u32 target_window_id` | Reused as "open window 1019 (Event News)" | VERIFIED (0x45291C) |
| 0x99 | S2C | SystemNotice_ChannelOrItemGrant | `u8 sub; sub 8: u8 channel; sub 9: u16 item_id, u16 count` | Event gift straight into the bag | VERIFIED (0x45D50F) |
| 0x6C | S2C | CashShopBuyResult | `u8 result; if 1: u32 cash, u32 mileage, 28-byte box record` | Event item into the cash item box (origin 3 popup) | VERIFIED (0x46B60C) |
| 0x6D | S2C | GiftInboxList | `u8 n; n x 130-byte record` | HUD Gift icon + gift popup (cash items only) | VERIFIED (0x46C33B, 0x4646C0) |
| 0x98 | S2C | MileageEventBonusNotice | empty | "※Mileage Event※ You got bonus mileage." | VERIFIED (0x46C550) |
| 0x4B | C2S | SendNote (gift thank-you) | `u16 9999; str[17] name; str[91] text` | Reply typed in gift popup 0x3EF | VERIFIED spec |
| 0x06 | C2S | GM /notice, /expexp | `06 01 u8 len text` / `06 06 i32 amount` | GM tools for events | VERIFIED spec (0x446824) |
| 0x1A | S2C | NpcMonsterSpawn | see spec | Boss spawn (template = hni position) | VERIFIED spec |
| 0x29 | S2C | EntityDie | `u32 uid; u32 respawn_tick; u32 x; u32 y` | Boss death | VERIFIED spec |
| 0x06 | S2C | PlayerDespawn | `u32 uid` | Corpse removal (server respawn model) | VERIFIED spec |
| 0x2A/0x9E/0x9F | S2C | move-state keyframe / queued | `u32 uid; u32 lo; u32 hi [; u32 target ...]` | Boss AI commands (attack A/B, dash) | VERIFIED (MONSTER_AGGRO_RE) |
| 0x28 | S2C | SetLocalHp | `u16 hp` | Boss damage to the victim | VERIFIED spec |
| 0x0D | C2S | PlayerMoveState / hit report | 61 B interact form | Player hits the boss; boss hit events 1..10 | VERIFIED (combat model) |

No dispatch entry exists for a "boss", "event" or "announcement" opcode in either S2C switch (primary 0x451960, SubHandler4 0x46A2E0). [VERIFIED; dispatch_2009.json lists every case]

---

# Part A. Events

## A1. What Build 14 contains

| Thing | Where | Status |
|---|---|---|
| Eventina "Event Announcer", EVENT arch | hni 147; npc001.hsi sprite 136; 9 towns | VERIFIED data |
| Event News popup | hui window 1019 (0x3FB), sprite 362 = `ui011_a1.hsc`, close-beta art | VERIFIED data |
| Nicolas "Self-styled Handsome", event quest turn-in | hni 76; 27 quests with ENPC 76; 8 towns | VERIFIED data |
| Event quests | hqi 63, 66, 148, 158-161, 232-242, 262-281, 285-291 (44 quests with a `*` title, plus the orphaned 63; see A3) | VERIFIED data |
| Event items | hii 83, 84, 91, 92, 98, 122, 123, 160, 196, 2029, 3202, 3203, 3382-3384, 3416-3419, 3442, 4182-4186, 4205, 4249-4252 ("<Event>"/"(Event)") | VERIFIED data |
| Event gift popup text | exe 0x52B8C4 "Congratulation.\r\n\r\nYou received an event item.." | VERIFIED |
| Mileage event text | exe 0x52A5AC "※Mileage Event※ You got bonus mileage." | VERIFIED |
| Cash-shop "Event Items" / "Event Effect" tabs | hui 463 ctrl 68 (UI 3307), ctrl 64 (UI 3303) | VERIFIED data (P8 scope) |
| EXP multiplier indicator | none | VERIFIED (absence, string/grammar search) |
| Login event popup | none; window 1019 can be reused | VERIFIED (absence) |
| Rotating "[Announcement]" tips | `hs/windslayer.hbi`, loaded client-side by FUN_004013e0 | VERIFIED |

## A2. Eventina (hni 147) and the Event News window

**Template** (`windslayer.hni` line 147) [VERIFIED data]:
```
147 434 ./hs/npc001.hsi NONE AI: 0x13 ... UI: 1019 Quest: 0 Illust: 000 Lv: 0 type: 0 ... sprite: 136 x21 ... Infor: # Cmt: # ExtraName: 255
```
- Name: NPCLngKo 434 "Eventina". Title: NPCLngKo 255 "Event Announcer".
- EN has no ambient bubble (`Cmt #`). The KR row has "이벤트가 진행 중 입니다. 클릭~ 클릭~" ("An event is running. Click~ click~") and the same UI 1019. [VERIFIED KR gamedef]

**Sprite** [VERIFIED data]:
- npc001.hsi sprite 136 uses image 11 = `npc009_a1.hsc`, region (512,0)-(936,600), 212x200, 6 frames of 300 ms, center (-108,-199).
- The rendered frame (`eventina_npc136.png`) shows the green-haired girl on a gift box under a pink "EVENT" arch with wings and balloons.
- So the "EVENT arch" of the videos is NPC art. It needs no map object and no packet.

**Placement** (event-2 tiles, NpcId 147, `value_num` 30) [VERIFIED data]:

| Town | Map file (code) | Eventina tile | Nicolas tile |
|---|---|---|---|
| Popola Village | stage02_01 (201) | (1800,1494) | (1200,1494) |
| Ozi Village | stage04_01 (401) | (2110,1311) | (1494,1420) |
| Amakusa | stage05_01 (501) | (2615,945) | (2042,970) |
| Atajokuna Settlement | stage06_01 (601) | (3800,915) | (4132,625) |
| Balderan | stage07_01 (701) | (2950,1298) | (3850,799) |
| Balderan Mining Village | stage08_01 (801) | (1900,1938) | (1422,1678) |
| Serien, the City of Water | stage09_01 (901) | (4100,854) | none |
| Serien Underworld | stage10_01 (1001) | (1400,1300) | (1400,819) |
| Underwater City RA | stage11_01 (1101) | (2100,824) | (1500,524) |

**The client builds her itself.** [VERIFIED] FUN_00445970 walks the event-2 tiles through FUN_004073b0(map, 2, ...). For a template with `type` (+0x654) <= 2 it:
- allocates an entity with uid 33000000+;
- copies `value_num` to +0xEF4, `type` to +0xF00 and `UI` (+0x64C) to +0xF08 (0x445970, decomp lines 49-89).

The server sends nothing for town NPCs.

**Name tag.** [VERIFIED] FUN_0043c300 draws a type-4 entity with +0xF00 == 0 (and no pet owner +0x163C) in two lines:
- the name, colour 0xFF32FF00;
- template+0x11, the hni ExtraName "Event Announcer", colour 0xFF00FF00, in box sprites 0x26..0x28.

**Click path** (no network traffic) [VERIFIED]:
1. FUN_00450130 is the mouse-on-entity handler. On WM_LBUTTONUP (0x202) in field phase (scene+0xF18 == 6), on an entity with +0x9C == 4 and +0xF00 < 3, it calls FUN_00446000 (call at 0x450395). FUN_00450ec0, the key-bound interaction reached from FUN_0043d870, also calls it (0x450F40).
2. FUN_00446000 has two gates:
   - FUN_0049f160() == 0: no other NPC window open (it scans the open windows for +0x11C != 0);
   - no stall window 0x259.
3. Template `Quest` (+0x648) != 0 would take the quest path FUN_0048a510. Eventina has 0.
4. The window id is taken from entity+0xF08 = 1019. It is not 0x1A7 (bank) or 0x298..0x29A (crafting), and not 0x52.
5. FUN_0049f140 stores the template index at window+0x11C (no packet).
6. `ui_root+0x5A4 = 1; FUN_00448730(1019, 0, 3, 0, 1)` opens the window.

**Window 1019** (`windslayer.hui`) [VERIFIED data]:
```
1019 6765 Sprite: 362... Pos: 150 50 Size: 512 460 Type 0 Bt_No 2
  1 Text 2 "Title"  Type 10 (drag bar) Pos 3,5 Size 480x20
  2 Text 3 "Close"  Type 9  Event 1    Pos 485,9 16x16
```
- Name: UILngKo 6765 "Pop-Up  (Event News)".
- Background: ui001.hsi sprite 362 = image 11 `./hs/ui011_a1.hsc`, region (0,0)-(512,460).
- The rendered art (`ui011_event_news.png`) reads:
  - "WIND SLAYER CLOSE BETA EVENTS"
  - "RACE TO LEVEL 10 (IN-GAME) ... ACHIEVE LEVEL 10 BEFORE CLOSE BETA IS OVER ... JANUARY 7TH, 2009 3PM PST - JANUARY 14TH, 2009 6PM PST ... PRIZE: EXCLUSIVE PREMIUM ITEM"
  - "BUG SLAYER (COMMUNITY) ... SUBMIT BUG HERE" (a drawn button, not a control)
- No code references 0x3FB as an immediate. A grep of all asm finds no `PUSH 0x3fb`, so the window is only reached through the generic id paths (the NPC UI field and S2C 0x80). [VERIFIED]

**Server duty:** none. Optional login popup: section A4.

## A3. Nicolas (hni 76) and the event quests

**Template** [VERIFIED data]:
- NPCLngKo 143 "Nicolas". ExtraName 403 "Self-styled Handsome".
- `Quest: 1`, `UI: 293`, `Illust: 53` (dialog portrait).
- `Cmt: 144`, his ambient bubbles: "Why must I be so handsome? / I'm scared to look at myself in the mirror. / There's always the danger of falling in love with myself, ha ha!"
- Because `Quest != 0`, FUN_00446000 always takes the quest path. `UI 293` is never used as a window from a click. [VERIFIED]
- The WS2 video calls him "Event Mania Nicolas". [INFERRED: later title]

**2009 quest-NPC logic** (5 active slots at char+0x260..+0x268) [VERIFIED]:

- **Turn-in, FUN_0048a510.** For every active slot whose quest ENPC (+0xE0C) == the clicked template, it checks:
  - `Money < 0` means gold >= |Money|;
  - progress >= `ReqPro` (+0xDF8);
  - every Demand item is in the bag (types 0/1/2).

  If all pass, FUN_004276e0 re-checks the bag, then **C2S 0x17 {u16 quest_id}** is sent (Add 0x17 at 0x48A934) and the quest's Finish chain opens. A failed bag check shows message box FUN_0049ebc0.
- **In-progress.** A held quest with SNPC (+0xE08) == npc opens its Ing chain.
- **Offer list, FUN_0048ac00.** It keeps a quest only if all of these hold:
  - SNPC != 0;
  - the quest is not in the 5 slots;
  - FUN_0048aa30 == 0 (not blocked by completion or repeat);
  - `Start_Lev <= level <= End_Lev`;
  - the job flag `record[0x310 + class*3 + tier] != 0`;
  - PrevQuest (+0xE00) is in the completed list (char+0x274, 85 entries).
- **Offer window, FUN_0048b0d0.** It lists the survivors whose SNPC == npc in window 0x236.
- **FUN_0048ab40.** If nothing else opened, it picks the first quest with SNPC == npc and opens its `None` chain.

**Consequence.** [VERIFIED data + code]
- All 27 quests with ENPC 76 have SNPC 0, except 239 (SNPC 42 Fiona). No quest has SNPC 76.
- So Nicolas never offers anything. The Ing path also needs SNPC == 76, so it never fires for him. A click opens a dialog only when the player holds one of his quests with all requirements met (the turn-in). Otherwise nothing opens. [VERIFIED code; the "nothing" result is INFERRED until T-E0]
- 44 quests in the table have SNPC 0.

[INFERRED] Retail either shipped event-time hqi files with SNPC set, or pushed the quest from the server. The quest log Intro text supports the push: "Windslayer Event is on-going. Visit Nicolas in each town to get the quest." (QSTLngKo 1852). This design pushes with S2C 0x26.

**Event quests in the 2009 hqi.** Titles from QSTLngKo. Korean titles are translated in brackets. [VERIFIED data]

| Quest | Title | SNPC/ENPC | Lv | Rep | Needs | Reward | Text language |
|---|---|---|---|---|---|---|---|
| 63 | "Let's Gather Chocolate==" (Valentine) | 0 / 0 | 0-0 | 0 | 1283 Sweet Chocolate x15, 1284 Smooth Chocolate x15 | **1282 Love Potion x5** | EN (Explain QST 250). Dialogs UI 8287-8290 (Nicolas) |
| 158 | "*Monster Card Challenge" | 0 / 76 | 1-99 | 0 | 3 Blue Mushroom x20 | 2031 Monster Card <Koring>, 50 exp, 500 gold | EN (Intro QST 1852, dialogs UI 9008-9016) |
| 159 | "*Monster Card Challenge2" (Prev 158) | 0 / 76 | 1-99 | 0 | 140 Pork x30 | 2038 Card <Dumpling Pig>, 100 exp, 1000 gold | EN |
| 160 | "*Monster Card Challenge3" (Prev 159) | 0 / 76 | 1-99 | 0 | 181 Gray Scrap Iron x50 | 2043 Card <Iron Ball>, 200 exp, 2000 gold | EN |
| 66 | "*최고수를 찾아서" [Find the best player] | 0 / 0 | 1-99 | 0 | kill 33 B.H Pikeman x30 + 187 Wooden Doll x30 | potions, 10000 exp | KR (EN dialogs UI 8301-8303 "Looking for Exceptional Players") |
| 148 | "*윈드스타를 찾아라!" [Find the Wind Stars] | 0 / 0 | 1-99 | 99 | 2029 Wind Star x50 | 161 Baobab Branch x20 | KR |
| 161 | "*엘리멘티리움만들기" [Make Elementirium] | 0 / 0 | 5-99 | 1 | 2975 x1 | Eledust x5, Elestone | KR |
| 232 | "*트리장식 모으기!" [Collect tree ornaments] (Christmas) | 0 / 76 | 15-99 | 10 | 2029, 3202, 3203 x10 each | 1270/1340 x5 | KR |
| 233-239 | "*퀴즈릴레이 1..7" [Quiz relay] (New Year) | chain 0>9>18>21>36>41>42>76 | 1-99 | 1 | one quiz-answer item each (e.g. 239: 123 Golden Pig) | small items | KR |
| 240-242 | "*황금돼지를 모으자!" [Collect Golden Pigs] | 0 / 76 | 1-20 / 21-40 / 41-99 | 99 | 123 x20/30/50 | elixirs | KR |
| 262-264 | "*케이크의 재료" [Cake ingredients] (Anniversary) | 0 / 76 | bands | 10 | 3382-3384 x5 | elixirs | KR |
| 265-267 | "*러블리 초콜렛" [Lovely Chocolate] (Valentine) | 0 / 76 | bands | 10 | 3419 x5/10/20 | elixirs | KR title; EN dialogs UI 7271-7286 |
| 268-275 | "*발렌타인데이 싫어1..8" [I hate Valentine's Day] | 0 / 9,21,78,89,84,110,132,149 | 1..80+ | 0 | kill 200 of npccode 5/17/29/34/85/114/136/156 | exp 500..200000 | KR |
| 276 | "*황금돼지를 잡아라!" [Catch the Golden Pigs] | 0 / 76 | 1-99 | 10 | 123 x100 | raffle entry | KR |
| 277 | "*윈슬노트 모으기" [Collect WS Notes] | 0 / 76 | 1-99 | 10 | 3442 x20 | raffle | KR (EN dialog UI 7529) |
| 278-280 | gift-certificate / iPod / NDSL raffles | 0 / 76 | 1-99 | 10 | 160 Raffle Ticket x30/50/100 | raffle | KR (EN dialogs UI 7815-7836, 7789-7804) |
| 281 | "*윈슬노트 AGAIN" [WS Note again] | 0 / 76 | 1-99 | 10 | letters 212-221 "W,I,N,D,S,L,A,Y,E,R" | raffle | KR (EN dialogs UI 7842-7857) |
| 285-289 | "*시원한팥빙수" [Cool Red Bean Sherbet] (Summer) | 0 / 76 | 5 level bands | 5 | 4186 x1 | potions, exp 280..7030 | KR (EN dialog UI 7946) |
| 290 | "*송편이 먹고 싶어~" [I want songpyeon] (Thanksgiving) | 0 / 76 | 5-99 | 99 | 92 Rice Cake x10 | 1341 x10 | KR (EN dialogs UI 8629-8632) |
| 291 | "*대보름반지 만들기" [Make the Full Moon Ring] | 0 / 76 | 6-99 | 0 | 98 Ring + 196 Moon Dust | 4205 Full Moon Ring | KR |

Notes:
- Quest dialog windows use their own numbering. For example quest 158 uses windows 1191-1195, which are named "Quest 279_xx" (KR numbering). [VERIFIED data]
- **Only quests 158-160 are fully English** (title, log text, dialogs).
  - Quest 63 has English text but ENPC 0, so no NPC can ever turn it in: FUN_0048a510 compares ENPC with the clicked template, and no template is 0. [VERIFIED]
  - The Korean titles and Explain/Intro strings of the others would show as CP949 bytes in the EN font. [INFERRED: not checked live]
- Quest 158's Finish chain (window 1195) contains an **Accept** button (event 2) that offers the next challenge. What C2S it sends from a Finish chain was not traced (open question Q4).

## A4. Login event popup

- **The Dec 2009 popup is not in Build 14.** [VERIFIED]
  - Video 2b70F9hk_Lc 0:09 shows "Wind Slayer - Special EVENT!!" with Bro' Singha art and "EXP/ WSP X2 WEEKEND 12/19-20".
  - None of the phrases "Special EVENT", "Holiday", "WEEKEND" or "WSP" is in `strings.tsv` (exe) or `UILngKo.lng`.
  - The only 512x460 event art is window 1019's close-beta sheet.
  - That a later client added its own popup is INFERRED.
- **Reusing window 1019 at login.** [VERIFIED code, live test needed]
  - S2C 0x80 handler (0x45291C) reads `u8 result`. result != 1 shows "Invalid password.\r\n\r\nPlease check your password again." (0x452A11) and stops.
  - For result 1 it reads `u32 target_window_id`. The ids 0x1F9, 0x1FA, 0x4B8 and 0x4CF have special handlers. **Any other id** goes to `FUN_00497c70(ui_root, id, 3, 1)` (0x4529BF..0x4529CA).
  - FUN_00497c70 is `ui_root+0x5A4 = 1; FUN_00448730(id, 0, 3, 0, 1)`, exactly the call the Eventina click makes.
  - So the packet `80 01 FB 03 00 00` opens the Event News window.
  - The handler has no state gate (spec). Send it after the enter-world burst.
- **The art cannot be changed from the server.** Replacing `ui011_a1.hsc` (a 512x460 DDS inside the ZIP+cipher container; `re_tools/combo_art_builder/hscodec.py` already re-encodes .hsc) would give a real "Special EVENT" sheet. That is a client-data patch, not retail. [INFERRED feasible]
- **Text alternative.** [VERIFIED] S2C 0x15 with msg_type 2 and text starting "[Ann" gives:
  - a chat line in 0xFF00E4FF;
  - a centre-screen notice (strncmp 4 against "[Announcement]" at 0x456774, then FUN_00495e30).

  Keep text_len <= 88. "[War..." is red.

## A5. Event gifts (e.g. "Love Potion x5")

Item 1282 "Love Potion" (hii): Type 0 (consumable), **Cash 0**, HP 15, MP 20, CT 3500 ms, Buy 100. Text: "A drink that enhances one's mood, and helps relax the mind and body." [VERIFIED data]

| Path | Packet | Client result | Constraints | Status |
|---|---|---|---|---|
| **G1 bag grant** | S2C 0x99 `09 <u16 item> <u16 count>` | Looks up the item (FUN_00404750) and adds it by Type. See the list below | Type 3/4 items and unknown ids are ignored. A full tab fails silently. No ack | VERIFIED (0x45D546..0x45D6CE) |
| **G2 cash-box event item** | S2C 0x6C `01 <u32 cash> <u32 mileage> <28-byte record>`, record+0x1A origin = 3 | Stores cash; see the list below | Needs the P8 box model (withdraw through the mall). A mileage above the stored value (mall+0x594) also pops "50% bonus mileage has been deposited." (0x46B656..0x46B673), so send the unchanged mileage | VERIFIED (0x46B60C..0x46B72F) |
| **G3 gift inbox** | S2C 0x6D `u8 n; n x 130 B` | Appends to queue ctx+0x600. Sets HUD window 3 ctrl 6 ("Receive a gift", tooltip UI 58 "You have a new gift!") visible | See the list below. **Love Potion (Cash 0) would stall the queue** | VERIFIED |

G1 details:
- Type 0: FUN_00424fb0 into the consumable tab.
- Type 1: FUN_00425420, only if the equip slot byte (+0x3BB) <= 0x2D.
- Type 2: FUN_004252a0.
- Then it refreshes the bag (FUN_0046ef40) and shows "You've received %s. (Count:%u)" (0x5298B0) through FUN_00495e30 (0x45D6A1).

G2 details:
- A record with item_id 0x75C..0x761 is a slot-extension ticket and is applied at once (FUN_00464f40).
- Any other record gets the popup **"Congratulation.\r\n\r\nYou received an event item.."** (0x46B6BC..0x46B6D3) and is appended to the cash item box list mall+0x28 (FUN_0046e160).
- It then rebuilds box windows 0x1FC/0x1FD.

G3 details:
- Popup 0x3EF opens only from FUN_004646c0. That function is called at the end of S2C 0x6A (entering the Spark Shop) and after a gift-popup confirm.
- The popup is shown only if item def+0x1F0 (Cash) != 0 or the id is 0xCFF..0xD04 (3327-3332 WS Gift Certificates) (item lookup from record+0x6E at 0x46470A, then the test). Anything else never pops, and the head of the queue blocks every later gift.
- Every map load hides the icon again: FUN_00497cb0 calls FUN_0049e070(3,6,3). S2C 0x6B does the same.

**Decision.** [INFERRED design]
- The login gift "Love Potion x5" uses **G1**. The server helper `GameServer.grant_item` already sends 0x99 sub 9 and mirrors the stacking rules (windslayer_server.py ~1673).
- Cash event items (tab "Event Items") use **G2** once P8 has the box.
- **G3** is only for cash items and gift certificates, and the popup appears only inside the mall.
- The retail "Gift button" in the Build 14 HUD is G3's icon. [VERIFIED data]
- Clicking the icon in 2009 was not traced. It is inert in 2008 (premium_cash.md Q16). See open question Q3.

## A6. EXP / WSP x2

- **No client indicator and no packet.** [VERIFIED]
  - S2C 0x21 reads only `i32 exp_delta` (0x459063). For a positive delta, when the local entity's guild id (+0x12) is >= 2, it also reads `u32 guild_points` (0x459216).
  - The client adds the delta as sent and prints "You've received (+%d) experience points." (0x52C168).
  - So an EXP x2 weekend is purely server-side: double the delta. The only thing the player sees is the larger number.
- **Guild hazard.** [VERIFIED] With a multiplier, the u32 guild_points field must still be present exactly when the client's own guild id is >= 2. A missing field desyncs the stream.
- **"WSP" is unknown.** [INFERRED] No string "WSP" exists in Build 14. It is probably the later name of Victy or reputation points (0x52984C: "+%uReputation Points, +%uVicty, +%uEXP.").
- **Related client data** [VERIFIED data]:
  - Cash items 3321-3326 "+50% EXP / +100% EXP" for 7/30/60 days (Type 5, Cash 1, Cash_T 2, Cash_V days). These are **P8**. The server applies them; the client has no effect code for them. [INFERRED]
  - Event item 4205 "Full Moon Ring <Event>" (Type 1, Cash 0): "doubles your Item Drop Rate during the event period". Server-side only. [INFERRED]
- **Announcing the event:** [VERIFIED]
  - S2C 0x15 msg_type 2 "[Announcement] EXP x2 weekend is on!" gives the centre notice and chat line.
  - The rotating tips come from the client file `hs/windslayer.hbi` (FUN_004013e0 opens "./hs/windslayer.hbi"; called from FUN_00479e90). The server cannot add lines to that rotation.
  - Do **not** use S2C 0x3A: it self-terminates the client about 170 s later (live C26).
- **GM tools** (C2S 0x06, gm.py) [VERIFIED spec]:
  - `/notice <text>` is sub 1. The server broadcasts it as 0x15.
  - `/expexp <n>` is sub 6 with an i32 amount (0x446824..0x446859). It is audited but not handled (gm.py:133). An EXP grant here is a useful test tool.

## A7. Mileage events

S2C 0x98 has an empty body. It calls FUN_00495e30 with the string at 0x52A5AC "※Mileage Event※ You got bonus mileage." in colour 0xFFFDF34C (0x46C550). [VERIFIED] It changes no balance: pair it with S2C 0x70 `{u32 cash, u32 mileage, u8 first_purchase_bonus}`. This belongs to P8 (premium_cash.md F16).

## A8. Per-opcode reference (events)

### S2C 0x80 used as "open Event News"
- Direction: S2C. Grammar: `u8 result=1; u32 target_window_id=0x3FB`. Bytes: `80 01 FB 03 00 00`. [VERIFIED]
- Client: `ui_root+0x5A4 = 1`, window 1019 opens with mode 3 (0x4529BF). [VERIFIED]
- C2S back: none. The Close button (ctrl 2, event 1) closes the window locally. [INFERRED: generic window close]
- Must-reply: none. Error: result != 1 gives the "Invalid password" box, so always send 1. [VERIFIED]
- Hazards: it shares the opcode with the second-password flow. Never send an id with a special handler (0x1F9, 0x1FA, 0x4B8, 0x4CF), or 0x1A7/0x235. [VERIFIED ids]

### S2C 0x99 sub 9 (event gift)
- Grammar: `u8 9; u16 item_id; u16 count`. [VERIFIED]
- Client: see A5 G1. Message "You've received %s. (Count:%u)". [VERIFIED]
- Must-reply: none. Errors: silent failure on an unknown id, Type 3/4, a full tab or count 0. [VERIFIED spec]
- Persistence: the server bag model must mirror the add: stacks of 999 (consumable) and 99 (etc), one equipment item per packet. [VERIFIED spec]

### S2C 0x6C with origin 3 (event item into the cash box)
- Grammar: `u8 result=1; u32 cash; u32 mileage; u32 serial; u16 item_id; u8 kind; u8 unk; u16 qty; bytes[16] expire; u8 origin=3; u8 unk`. [VERIFIED]
- Client: see A5 G2. [VERIFIED]
- Must-reply: none. Hazard: a duplicate serial gives a duplicate box entry. [VERIFIED spec]
- Persistence: the account cash box record with `origin = 3` (premium_cash.md §4: origin 3 = event/GM grant, no refund). [INFERRED from 2008 design]

### S2C 0x6D (gift inbox)
- Grammar: `u8 gift_count; repeat { u8 0; str[17] sender; str[91] msg; u8 0; u16 item_id; u8 option; u8 0; bytes[16] time }`. [VERIFIED spec]
- The reply from popup 0x3EF is C2S 0x4B `{u16 9999, str[17] sender, str[91] reply}`, sent only with non-empty text. The head gift is popped either way. [VERIFIED spec]
- Must-reply to 0x4B: none known (premium_cash.md Q15). For system senders ("GM", "Event") the server drops the note. [INFERRED]
- Hazard: a non-cash item stalls the queue (A5). [VERIFIED]

### S2C 0x15 (announcement)
- Grammar: `u8 msg_type; u8 len (<= 88); str text`. msg_type 2 plus "[Announcement]" gives the centre notice. [VERIFIED]
- Must-reply: none. msg_type >= 3 gives a garbage colour. [VERIFIED spec]

### S2C 0x26 / C2S 0x17 / S2C 0x27 (event quests)
- Push: `S2C 0x26 {u16 quest}`. The client writes the first free slot of 5, clears its ready flag, grants the Send items itself and shows the quest's Intro in the log. [VERIFIED spec]
- Turn-in: the player clicks Nicolas while requirements are met, and the client sends `C2S 0x17 {u16 quest}` (FUN_0048a510). [VERIFIED]
- Server reply: `S2C 0x27 {u16 quest}`. The client pays Money, removes Demand items, grants Reward items and prints "[%s] Quest Completed.". Then send `S2C 0x21` for the Exp: the client does not apply quest Exp. [VERIFIED spec]
- Must-reply: 0x17 should always get 0x27 (or nothing on refusal). No client modal waits, so nothing soft-locks. [INFERRED from the 2008 spec]
- Error strings: client-side only (bag space). [VERIFIED spec]

### S2C 0x98 (mileage event)
- Empty. See A7. [VERIFIED]

---

# Part B. Bosses

## B1. How the data marks a boss

- **The hni template has no boss field.** [VERIFIED]
  - FUN_00409150 tokenises each record into fixed positions: idx, title, hsi, NONE, 13 AI ints, item[120], Drop[120], Quest, UI, Illust, Lv, type, HP, MP, Body_Atk, Def, Weak_Atk, Strong_Atk, AtkType, speed, Attri_Atk, Attri_Def, Exp, sprite 3x21, Weak/Strong_Atk_AI_ChkTime, Infor, Cmt, ExtraName.
  - Nothing distinguishes a boss.
  - The KR newer build has extra columns `AiUseItem`, `AiSpawn`, `Right` (PySlayer gamedef). EN 2009 has none of them. [VERIFIED]
- **Three data markers single out the field bosses:** [VERIFIED data]
  1. The map tile `value_num` is **300** on exactly 11 event-2 monster tiles. No regular monster has it.
  2. A drop entry at rate **99990** for a quest trophy item.
  3. Some have an ExtraName title: Rynx "Forest Hooligans" (358), Monkey King "Giant Monkey" (404). The client never shows monster titles (B4).
- **`value_num` is server data.** [VERIFIED]
  - The map loader (FUN_00407800) stores `event` (0x4084EE), `value_num` (0x4084FF) and `NpcId` (0x408519, only when event == 2) into the tile node (FUN_00408ac0: +0x24 event, +0x28 value, +0x2C NpcId).
  - The only readers are FUN_004073b0's two callers:
    - FUN_00445970 (town NPCs, type <= 2), which copies value to entity+0xEF4;
    - FUN_004296a0, which asks for event **3** tiles (arena).
  - Monster tiles (type > 2) take the free-and-skip branch to 0x445A6F. The client never reads their value.
- **`value_num` distribution over all 288 maps** [VERIFIED data]:

  | Value | Tiles |
  |---|---|
  | 15 | 8 (map 102 Ssiyo) |
  | 20 | 6 |
  | 25 | 194 |
  | 30 | 2109 |
  | 40 | 126 |
  | 50 | 43 |
  | 100 | 1 (Dumpling Pig, Ozi Village Road) |
  | **300** | **11 (bosses)** |
  | 901 | 1 (Crow, Amakusa Main Street) |
  | 9901 | 3 (Shovel Frog, a type-0 merchant NPC in mining areas) |
  | 0 | 718 (town NPC tiles) |

  Reading it as "respawn seconds" is **INFERRED**. The server's `en_content.map_spawns` docstring says the same. It fits a 5-minute boss timer and the video note "Bomb Rat sometimes wasn't there" (HOZqk6jDPIc).

## B2. Field boss catalog (2009 data)

Map codes: stageAA_BB = AA*100+BB. Element = hni type - 3:
- The 0x1A handler stores element byte +0x11F0 = 1..4 and the matching Attri_Def slot (0x456C22..0x456C89). [VERIFIED]
- The names 1 Fire, 2 Water, 3 Earth, 4 Wind come from DAMAGE_FORMULA. [INFERRED]

The card drop (0.01%) is omitted from the trophy column. Drop rate % assumes the /100000 unit (B6).

| Boss (hni) | Title | Lv | HP | Elem | AtkType | AI[13] | Body/Weak/Strong | Def | Exp | ChkW/S ms | Map (tile) | Trophy drop |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Rynx (12) | Forest Hooligans | 8 | 174 | Fire | 4 | 1100001010000 | 18/17/25 | 31 | 100 | 150/240 | Foothill 218 (1609,1835) | 124 Rynx's Chest Fur 99.99% |
| Monkey King (81) | Giant Monkey | 15 | 288 | Earth | 1 | 1101001010000 | 29/24/35 | 57 | 282 | 240/330 | Ascetic Quarter 409 (2400,1489) | 1291 Monkey King's Gold Diadem* 99.99% |
| Wasablanca (95) x2 | - | 34 | 956 | Water | 4 | 1101101010000 | 60/67/103 | 170 | 1044 | 210/360 | Amakusa Bridge 511 (3700,1025),(4300,1025) | 1559 Horseradish 50%, 1582 Sunflower 49.99% |
| Leo Wolf (115) | - | 61 | 3278 | Fire | 2 | 1101001010000 | 163/181/275 | 414 | 2394 | 240/420 | Yanata Exclusion Zone 619 (3100,1674) | 168 Leo Wolf Mane 99.99% |
| Drill Mole (116) | - | 49 | 2400 | Earth | 1 | 1101001010000 | 111/126/172 | 293 | 1734 | 150/360 | Old Coal Mine Station 819 (2000,1473) | 167 Drill 99.99% |
| Wook (137) | - | 73 | 4416 | Fire | 2 | 1000001010000 | 211/249/364 | 558 | 3136 | 300/480 | Ocean's End 920 (2600,1108) | 3104 Wook's Wooden Leg 99.99% |
| King Frog (157) | - | 85 | 5590 | Earth | 2 | 1000001010000 | 526/670/957 | 747 | 3964 | 180/750 | Hidden Prison 1 1017 (2200,900) | 3286 King Frog Crown 99.99% |
| Waterfrog (158) | - | 85 | 5668 | Water | 2 | 1000001010000 | 570/726/1038 | 764 | 4036 | 180/750 | Hidden Prison 2 1018 (800,900) | 3287 Bullfrog Crown 99.99% |
| Firefrog (159) | - | 86 | 5720 | Fire | 2 | 1000001010000 | 605/770/1101 | 781 | 4108 | 180/750 | Hidden Prison 3 1021 (600,1219) | 3288 Giant King Frog Crown 99.99% |
| Blue Shark (170) | - | 97 | 9163 | Water | 2 | 1001101010000 | 672/856/1224 | 970 | 4876 | 360/900 | Bubbalo 1111 (1000,337) | 3456 Blue Dorsal Fin 99.99% |

Status of the table:
- All stats, tiles and drops: VERIFIED data (`boss_catalog_2009.json`).
- The Crow tile with value 901 on Amakusa Main Street is also listed in the JSON. Treating it as a rare spawn is INFERRED; it is not treated as a boss here.

What each boss map also spawns [VERIFIED data]:
- **Foothill 218:** 13 Kamikaze Rat, 5 Poco, 3 Hendove, 2 Pponyang, 2 Cuckoo (all value 30).
- **Ascetic Quarter 409:** 5 Monkey General + 3 Monkey Lord (value 40).
- **Amakusa Bridge 511:** 10 B.H Samurai + 2 B.H Pikeman.
- **Yanata Exclusion Zone 619:** Hyena, Leopang, Skullephant, Rhino Turtle (21 tiles).
- **Old Coal Mine Station 819:** 7 Worm Dragon + 5 Pink Mouldywarp.
- **Ocean's End 920:** 17 tiles of Octoboy/Crab/Gull/Clamon.
- **Hidden Prison 1/2/3:** 3-5 Silica Bat each.
- **Bubbalo 1111:** 2 Sharko + 4 Red Blowfish.

## B3. The three monsters the retail survey names

- **Monkey King (hni 81).** One boss tile on Ascetic Quarter (409). [VERIFIED data]
  - Infor text (NPCLngKo 479): "The king of the Marble Mountain monkeys was once a friendly monkey, but it was transformed into a heinous monster, when a depraved monk placed the golden diadem on its head." [VERIFIED]
  - It is the target of quest 64 (Mei) and quest 182 (Rogue job quest).
  - It is also the final monster of instance dungeon stage indun02_06 "Hades Range top" (SCRIPT_INFOR, B9).
  - Mei's dialog UI 8297: "Monkey King is feared for the double attacks it performs with its club. You'd better equip some good armor, and bring enough potions to fight a monster king." [VERIFIED data]
  - UI 8293: "...bigger than the Monkey General, moves extremely quickly, and carries a huge club." [VERIFIED data]
  - The two attacks are attack A and attack B (B4). [INFERRED]
- **Monkey Lord (hni 140) is not boss-class.** [VERIFIED data]
  - Lv 12, HP 132, Exp 132. AI 1100001000000: attack A, AI[1], counter-jump.
  - Strong_Atk 0 and Strong chk 0, so it has no attack B.
  - 53 tiles on 7 maps, `value_num` 25/30/40/50:
    - Village Byway 406: 4
    - Ascetic Quarter Street 407
    - Ascetic Quarter Gate 408
    - Ascetic Quarter 409: 3
    - Rocky Valley 2 410: 3
    - Marble Mountainside 413
    - Hermit Quarter Gate 414
  - It also appears in instance stages indun02_03/04.
  - Its drop table is Monkey Sergeant's plus Card 3114. No title, no trophy.
  - The video (a party fighting a big ape with a red name box) matches a Lv 12 mob seen by a low-level player: the box turns red when the monster is more than 3 levels above the player (B4). [INFERRED]
- **"Bomb Rat": no such name in Build 14.** [VERIFIED: no "Bomb Rat" in NPC/UI/QST/ITM lng files]
  - The Foothill boss tile is **Rynx** (value 300).
  - Quest 6 "Forest Hooligans" (Gangdalf, Lv 8-20): "Defeat Rynx in Foothill, and bring Rynx's Chest Fur to Gandalf as proof..." (QSTLngKo 1861); reward 90 Guard & Evade.
  - Rynx Infor 463: "...usually followed by Kamikaze Rats". Kamikaze Rat (hni 6, KR 카미카쥐) Infor 462: "Although it isn't Rynx's sidekick by choice...". Foothill has 13 of them.
  - "Bomb Rat" in the O-late video (HOZqk6jDPIc) is either a later rename of the Kamikaze Rat, or the narrator's name for Rynx. [INFERRED]

## B4. What the client shows for a boss

- **No HP bar.** [VERIFIED]
  - FUN_0043c300 (name tag) draws a monster (type-4 entity with +0xF00 != 0) as one text line in 0xFFFFBFFF on a box sprite. No gauge call exists in the function.
  - The only HP gauges for other entities are party frames 0x75..0x78.
  - This matches video b6B7pwhtUSw: "no big boss HP bar, only the blue name tag". [VERIFIED video catalog]
- **No monster title.** [VERIFIED] The second line (template+0x11, ExtraName) is drawn only when +0xF00 == 0 (NPC types). "Giant Monkey" and "Forest Hooligans" are never shown.
- **Box colour = level gap only.** [VERIFIED; colours from RETAIL_COMBAT_FEEL_RE]
  - The rule (0x43C3CC..0x43C41C) compares the player level L (+0x9D, or override +0x985) with the monster level M (entity +0x9D).
  - M in [L-1, L+3]: sprite 0x2D (blue).
  - M > L+3: sprite 0x2A (red).
  - Otherwise: 0x26 (black).
  - The 0x1A handler copies the template Lv byte (+0x650) into entity+0x9D (0x456CAC..0x456CB5).
- **No boss announcement text.** [VERIFIED] No "appeared"/"defeated"-style system string exists in the exe or UILngKo, apart from quest dialogs.
  - `Sound/indunboss_die.wav` appears only in the install file list at 0x4D6F5E. No code references it.
- **Attack animations come from the template's `sprite:` column.** [VERIFIED data; the slot = action_state - 1 mapping is INFERRED from consistency]
  - The column is 3 rows x 21 slots, stored at template+0x158 with stride 0xC (FUN_00409150 cases 0x37-0x39).
  - Slot k (0-based) is action state k+1. Across all templates: slot 7 (state 8) = idle, slot 15 (state 0x10) = the die sprite, slots 3/13 (states 4/0xE) = attack A, slots 0/14 (states 1/0xF) = attack B. These are MONSTER_AGGRO_RE's attack states.
  - Rows 1 and 2 are non-zero only in the attack slots: overlay/effect sprites.

  | Boss | Attack A (state 4/0xE) | Attack B (state 1/0xF) | Dash (state 6) | Sounds in the .hsi |
  |---|---|---|---|---|
  | Monkey King mon023 (16 sprites) | body 2 (724x150, 6 fr) + 7 (360x220, 15 fr, `mon023_attack2.wav`) | body 3 (724x150, 12 fr) + 4 (606x118) + 9 (500x260, `mon023_attack3.wav`) | 6 (780x170, 4 fr) | hurt, attack2, attack3, die, walk |
  | Rynx mon007 | 5 + 8 | 10 + 12 + 11 | (no dash flag) | hurt, die |
  | Wasablanca mon027 | 5 + 6 | 7 + 9 + 8 | 2 | attack1, attack2, hurt, die, walk |
  | Leo Wolf / Drill Mole / Wook / frogs / Blue Shark (mon034/035/040/050/052/053/060) | 3 + 4 | 5 + 6/7 + 7/6 | 2 or 11 | attack1, attack2, die, hurt (+walk) |
  | Monkey Lord mon044 (7 sprites) | 3 + 4 (`mon044_attack1.wav`) | none | none | attack1, hurt, die |

- **Dash.** [VERIFIED] Motion 6 (the AI[3] "skill" input, server lo word 0x19/0x1A) keeps the mob in action state 6 while the input is held. After 500 ms (0x1F4) without motion 6 it goes to state 7 (0x4150F0..0x415115).
- **Weak/Strong_Atk_AI_ChkTime are the AI's hit-frame times.** [VERIFIED code; meaning INFERRED]
  - The 0x1A handler copies Strong -> +0xE84 (0x456BF8) and Weak -> +0xE88 (0x456C04).
  - The chase AI tests reach by setting state 4 with action time = +0xE88 (0x4190D7), or state 1 with +0xE84 (0x4190EF), and calling FUN_0041dc70 for the attack rectangle at that frame time.
  - A server AI that ports the hsi rectangles should sample at these times: Monkey King 240/330 ms.
- **AtkType bits.** [VERIFIED; values in data: 0,1,2,4,5,16]
  - 0x10: no contact damage (0x416C0C; Statue only).
  - 0x02: the counter-hit branch FUN_0041aa80 returns 0 while the mob is in attack A (0x41AB4C), so the player's hit falls back to the ordinary event-7 path.
  - 0x08: the same for attack B (0x41AB35). No template sets it.
  - 0x01 and 0x04: no reader in the client.
  - Bosses with 0x02: Leo Wolf, Wook, the 3 frogs, Blue Shark. Gameplay effect: "cannot be counter-hit while swinging". [INFERRED]
- **Speed.** [VERIFIED] The template `speed` is stored as a float x 0.0001 (FUN_00409150 case 0x2F), then as f64 at entity+0x1280 (0x456C16). Monkey King 80000 gives 8.0.

## B5. Respawn

- The client has no respawn data for monsters. `value_num` is unread for them (B1). [VERIFIED]
- S2C 0x29 `respawn_tick` makes a type-4 entity revive **client-side** when the tick passes, unless map flag [scene+0xFB4]+0x2E0 is set (FUN_00414210 case 0x16). [VERIFIED spec]
  - The server today sends 0x7FFFFFFF (never).
  - It removes the corpse with 0x06 after MOB_CORPSE_SECS 3.0, and respawns with a fresh 0x1A after MOB_RESPAWN_SECS 15.0 (windslayer_server.py:237/238, 8594-8596). [VERIFIED server code]
  - Keep that model for bosses. Only the timer changes.
- **Recommended boss timer:** `value_num` seconds, so 300 s. [INFERRED] Make it configurable. Spawn a boss only when its timer is due, including on a map rebuilt after `MOB_MAP_KEEP_SECS` (300 s, config.py:238). Today a discarded map is rebuilt with every monster alive, so leaving and re-entering gives a free boss respawn. [VERIFIED server config docstring]
- **Regular monsters:** reading `value_num` 25-50 as seconds would triple today's 15 s. The WS2-era videos say mobs "respawn quickly". Keep regulars on MOB_RESPAWN_SECS by default, with an option to use `value_num`. [INFERRED]

## B6. Drops

- **Layout.** [VERIFIED code] `item:` is 120 x %04d ids at template+0x288 (case 0x13). `Drop:` is 120 x %06d rates at template+0x468 (case 0x15). Entry i of Drop is the rate of item i.
- **The client never rolls drops.** Loot arrives as server ground items. [VERIFIED: combat model]
- **Unit: 1/100000.** [INFERRED]
  - Every boss trophy is 99990: 99.99% at /100000, but 10% at /1e6, for single-item quest demands.
  - Monster cards are 10-50.
  - Gathering nodes sum to 65550 against the server's GATHER_RATE_DIVISOR 100000.
- **The server today ignores rates.** [VERIFIED server code] `if mob.drop_items and random.random() < 0.6: item = random.choice(mob.drop_items)` (windslayer_server.py:8632).
  - That gives a boss trophy about 30% of the time for bosses with a card entry, and Wasablanca's two items about 30% each.
  - The trophy quests need per-entry rolls (stage B2).

Boss drop tables (all VERIFIED data): see B2. Monkey Lord drops 128 Monkey Horn 30%, 127 Furry Monkey Tail 20%, 1 Monkey Hat Trinket 10%, potions 3%, Return Stone 0.5%, weapons 0.1%, card 3114 0.05%, and old rings/belts 0.01%.

## B7. Boss quests (2009 hqi)

| Quest | Title | NPC (S/E) | Lv | Condition | Reward | Status |
|---|---|---|---|---|---|---|
| 6 | Forest Hooligans | Gangdalf 9/9 | 8-20 | Demand 124 Rynx's Chest Fur x1 | 90 Guard & Evade, 50 exp | VERIFIED data |
| 64 | Monkey King Subjugation | Mei 21/21 | 15-25 | Demand 1291 Monkey King's Gold Diadem* x1 | 1290 Yellow Stripe Hat*, 100 exp | VERIFIED data |
| 104 | Repel the Disrupters | Dachibana 91/91 | 34-45 | Demand 1559 Horseradish + 1582 Sunflower | 1415 Oath Necklace - Fire, 2977 Elementirium x2, 650 exp | VERIFIED data |
| 137 | Drill Mole Extermination | Billiorc 84/84 | 49-59 | Demand 167 Drill | rough gems x10 each, elixirs, 5000 exp | VERIFIED data |
| 144 | Defeat Leo Wolf | Rara 109/109 | 61-71 | Demand 168 Leo Wolf Mane | 20 Mystic Elixir/Energizer, 5500 exp | VERIFIED data |
| 181 | Way of the Assassin 1 | Pitsher 41/41 | 30+ | kill npccode 12 Rynx x1 (NPC 12, ReqPro 1) | job chain | VERIFIED data |
| 182 | Way of the Assassin 2 | Pitsher | 30+ | kill 81 Monkey King x1 | job chain | VERIFIED data |
| 184 | Way of the Assassin 4 | Pitsher | 30+ | kill 95 Wasablanca x1 | job chain | VERIFIED data |
| 165 | Way of the Berserker 4 | Kawamachri 89/89 | 30+ | kill 95 x2 | 3068 Sugar Cube of Rage | VERIFIED data |
| 205 | Way of Elemental Magic 4 | Estaroje 36/36 | 30+ | kill 95 x2 | 3083 Job Change: Elementalist | VERIFIED data |
| 215 | Way of the Bishop 4 | Fiona 42/42 | 30+ | kill 95 x2 | 3085 Job Change: Bishop | VERIFIED data |
| 210 | Way of the Summoner 5 | Mei 21/21 | 30+ | Demand 1582 Sunflower | job chain | VERIFIED data |

The trophies of Wook, the three frogs and Blue Shark are demanded by no quest. [VERIFIED data]

Dialog anchors [VERIFIED data]:
- Quest 64: 294 > 295 > 296 (Accept) > 297; Ing 299 (UI 8297); Finish 298 (UI 8296 "Did you really defeat the Monkey King?...").
- Quest 6: First 52 (UI 8093), Ing 56 (UI 8097), Finish 57 (UI 8098).
- Quest 137 Ing UI 8505: "Watch out for its powerful drill attack".

## B8. Aggression classes per the data (for the AI)

Flag map from MONSTER_AGGRO_RE [VERIFIED there]:

| Index | Entity field | Meaning |
|---|---|---|
| AI[0] | +0xE50 | attack A (Weak_Atk) |
| AI[3] | +0xE5C | dash |
| AI[5] | +0xE64 | proximity aggro, 200x200 px |
| AI[6] | +0xE68 | counter-jump |
| AI[7] | +0xE6C | no jump |
| AI[8] | +0xE70 | attack B (Strong_Atk) |
| AI[9] | +0xE74 | stationary |

**Unused by the client:** AI[1], AI[2], AI[4], AI[10], AI[11], AI[12].
- An exhaustive asm grep for entity offsets +0xE54/+0xE58/+0xE60/+0xE78/+0xE7C/+0xE80 finds only unrelated objects: keyboard bindings in FUN_0042c310/FUN_0043d870 and writes in FUN_00487b90. [VERIFIED]
- Where they are set [VERIFIED data]:
  - AI[1]: 29 templates, including Rynx, Monkey King, Wasablanca, Leo Wolf and Drill Mole, but not Wook, the frogs or Blue Shark.
  - AI[2]: no template.
  - AI[4]: 15 templates (the Mouldywarps, Atomic Ball, Wasablanca, the sea monsters 166-170, the indun 176-180 and King Golem).
  - AI[10]: only the 4 pet templates 182-185 (Picky, Ulie, ChikaPuka, GuriGuri), so it is probably a pet marker. [INFERRED]
- The retail meaning of AI[1] and AI[4] is unknown. [INFERRED: server-only flags]

| Class | Templates (hni id) | Status |
|---|---|---|
| **Contact-only** (no AI[0]/AI[8]): damage only by body contact, event 6 with Body_Atk | 1 Ssiyo, 2 Koring, 3 Ororing, 4 Pponyang, 5 Poco, 6 Kamikaze Rat, 13 Flying Peach, 14 Dumpling Pig, 26 Iron Ball (+dash), 30 Crow (+dash), 139 Cuckoo, 145 Hendove; indun: 188 Sutah, 189 Ledulgi, 193 Slow Peach, 195 Quick Peach (+dash), 198 Iron Ball MK2 (+dash) | VERIFIED data |
| **Melee, attack A only** | 15 Monkey Soldier, 16 Monkey Sergeant, 17 Monkey General, 140 Monkey Lord (all with counter-jump) | VERIFIED data |
| **Melee A + B** | 12 Rynx, 27-29, 31-34, 71, 81, 85, 92-96, 111-116, 133-137, 141, 142, 153-161, 166-170; indun 176-180, 191, 192, 194, 199-202 | VERIFIED data |
| **Dash (AI[3])** | 26, 27, 30, 81, 85, 92-96, 113-116, 166-170, 176-180, 195, 198, 202 | VERIFIED data |
| **Proximity aggro (AI[5])** | only 193 Slow Peach (instance dungeon). **No field template** | VERIFIED data |
| **Stationary (AI[9])** | 143 Toad Cannon, 190 Statue | VERIFIED data |
| **No jump (AI[7])** | 1 Ssiyo, 13 Flying Peach, 143, 190, 193 | VERIFIED data |

**Pack behaviour: the client has none.** [VERIFIED]
- Aggro is written only by:
  - the hit-apply code (0x41A815, host-gated);
  - the proximity scan (0x41930C, host-gated);
  - two clears.
- No monster assists another. Retail "packs of monkeys clustering" (uGkNIrv1cSw, 2b70F9hk_Lc) is therefore either several monkeys hit individually (AoE and dense tiles: Village Byway has 11 Soldiers + 7 Sergeants + 4 Lords), or a server-side assist rule. Which one is unknown. [INFERRED]
- Default: no assist. Offer an assist option (same template, within a radius, on hit) that is off by default.

## B9. Instance-dungeon bosses (cross-reference)

Instance stages have no event-2 tiles. They spawn through `<SCRIPT_INFOR Action="13" ActMonsAprMonsIndx=..>` (see instance_dungeon.md). [VERIFIED data]

| Stage | Name | Boss spawned by the script |
|---|---|---|
| indun01_06 | "Unknown Forest", entry button hni 187 "Gang of forest" | Rynx x1 with Kamikaze Rats |
| indun02_06 | "Hades Range top", entry button 197 "Monkey Ghost King" | Monkey King x1 with Bogy Monkeys |
| indun03_05 | "Balderan Dungeon", entry button 204 "Weapon Destroy" | Atomic Ball x1 with 16 King Golem |

- Each final stage also has one `Action="15"` and `Action="17"`. Reading them as boss/clear triggers is INFERRED.
- New in 2009: type-4 auto-revive is suppressed on maps with [scene+0xFB4]+0x2E0 set. [VERIFIED spec] That these are instance maps is INFERRED.

## B10. Opcodes used for bosses

No boss-specific opcode exists. [VERIFIED] A boss is an ordinary 0x1A monster. The rules below are from the combat, aggro and item specs.

- **Spawn:** S2C 0x1A with template_index = hni position (1-based; equal to the idx in EN) and cur_hp = the template HP (Monkey King 288). `server_controlled` must be 0 (MONSTER_AGGRO_RE "Do not"). [VERIFIED spec]
- **Chase and attack:** the 0x2A 16-byte self-form with lo = direction | motion<<2 [VERIFIED MONSTER_AGGRO_RE / mobai.py]:
  - walk 01/02, jump 0D/0E, drop 11/12;
  - attack A 05/06, attack B 15/16;
  - dash 19/1A.

  0x9E hands the mob back. Monkey King needs A, B and dash; the frogs, Wook and Blue Shark need A and B. The swinging commands have **not** been live-verified yet (survey item 5). [INFERRED until T-B3]
- **Boss hits the player:** the victim's client sends C2S 0x0D with action event and event_source_uid = boss:
  - 1/6 contact: Body_Atk
  - 7/8 attack A: Weak_Atk
  - 4/9/10 attack B: Strong_Atk

  The server answers S2C 0x28 {u16 hp}, or runs the death flow. The client never subtracts HP itself. [VERIFIED MONSTER_AGGRO_RE]
- **Player hits the boss:** the 61-byte C2S 0x0D report. The server subtracts HP and sends 0x2A (releases the hit-lock), or 0x29 on the kill. [VERIFIED combat model]
- **Kill:** S2C 0x29, then the drop (ground item), 0x21 exp (+ guild points field), 0x59 for kill quests, and 0x06 after the corpse delay. [VERIFIED spec; order INFERRED]

---

## C. State and persistence

**Event definitions** (config JSON, reloadable) [INFERRED design]:
```json
{"events": [{
  "id": "valentine-2009",
  "start": "2009-02-13T00:00:00Z", "end": "2009-02-16T00:00:00Z",
  "exp_mult": 2.0,
  "announce": {"text": "[Announcement] EXP x2 weekend!", "every_min": 30, "on_enter": true},
  "popup_event_news": true,
  "login_gift": [[1282, 5]],
  "push_quests": [158],
  "cash_gift": []
}]}
```

Per character (persisted with the character) [INFERRED design]:
- `event_gifts_claimed: {event_id: iso_time}`, so a relog or reconnect never grants twice.
- `event_quests_pushed: {event_id: [quest ids]}`, so a quest is not pushed again after completion.
- Repeat counts and completions stay in the existing quest store (completed list up to 85 entries; hqi Repeat).

Per session, not persisted: `event_popup_shown`, and announcement timers.

Boss ledger, persisted server-side, per channel [INFERRED design]:
```
key (channel, map_code, tile_x, tile_y)
  -> {npc: 81, alive: bool, next_spawn_at: epoch_s, last_kill_at, last_killer}
```
- It must survive map discard (MOB_MAP_KEEP_SECS) and server restart. Otherwise a restart or map rebuild respawns every boss.

Nothing is persisted client-side for either system. [VERIFIED: the Event News window, tag colours and bubbles are all client-local and stateless]

## D. Server today (read-only check, 2026-09-27)

| Area | Today | Status |
|---|---|---|
| Boss spawn | `en_content.map_spawns` keeps `value_num` as `Spawn.value` but never uses it. Every mob respawns after MOB_RESPAWN_SECS 15 s | VERIFIED |
| Map discard | MOB_MAP_KEEP_SECS 300: a map is rebuilt fresh, bosses included | VERIFIED |
| Drops | 60% chance of one uniform random entry; rates ignored (ws:8632) | VERIFIED |
| AI | mobai.py models attack A/B, skill, proximity, no-jump, stationary. No counter-jump, no assist | VERIFIED |
| Bag grant | `grant_item` sends 0x99 sub 9 | VERIFIED |
| Notices | `_notice` sends 0x15. `_hook_welcome` sends one [Announce] line on the first map load | VERIFIED |
| GM | /notice handled. /expexp audited, not handled (gm.py:133) | VERIFIED |
| Events | no scheduler, no 0x80 popup, no event quest push, no multiplier | VERIFIED (grep) |
| Cash box / 0x6C / 0x6D | P8, in progress in another workflow | VERIFIED (not in master) |

## E. Implementation plan

Each stage can be tested alone. Events first, because E1-E4 are small and independent of P8.

**E1. Event schedule and notices**
- Build: `events.py` loads the event JSON (section C). Active-event query by UTC time. `[Announcement]` 0x15 msg_type 2 on first world entry (extend `_hook_welcome`) and every `every_min` minutes to sessions in world. GM `/notice` stays as is.
- Unit test: time-window selection; the 0x15 bytes (text_len <= 88; the "[Ann" prefix).
- Live test: T-E1.

**E2. EXP multiplier**
- Build: in `_kill_exp`, multiply after the card bonus: `int(exp * mult)`, at least 1. Optional config `EVENT_EXP_QUESTS` to scale quest Exp too. Keep the 0x21 guild_points field rule unchanged.
- Unit test: 10 exp becomes 20; the card +10% then x2 gives 22; the guild-member 0x21 byte length.
- Live test: T-E2.

**E3. Login gift**
- Build: on the first world entry while an event with `login_gift` is active, and not yet in `event_gifts_claimed`, call `grant_item(item, count)` (0x99 sub 9), then record the claim. Filter by the EN catalog and Types 0/1/2.
- Unit test: a second login grants nothing; a full tab logs a warning and keeps the claim unset.
- Live test: T-E3.

**E4. Event News popup**
- Build: config `popup_event_news`. After the enter-world burst (after C2S 0x63 like the channel notice), send `80 01 FB 03 00 00` once per session per event.
- Unit test: byte-exact builder; never sent with ids 0x1F9/0x1FA/0x4B8/0x4CF/0x1A7/0x235.
- Live test: T-E4.

**E5. Event quests (push + Nicolas turn-in)**
- Build:
  - During an event, push S2C 0x26 for `push_quests` on world entry if the quest is not held, not blocked by Repeat, a slot is free (5 slots) and the level range fits.
  - C2S 0x17 for an event quest: validate Demand and ReqPro, send 0x27 then 0x21 Exp, then optionally push the NextQuest (158 > 159 > 160).
  - Default list: 158-160, which are fully English. Quest 63 is excluded (ENPC 0 cannot be turned in).
- Unit test: the push rules; 0x17 validation.
- Live test: T-E5.

**E6. Cash-side events (after P8 lands)**
- Build: event cash items into the box through 0x6C with origin 3 (mileage unchanged). Mileage event: 0x70 + 0x98. The cash-shop "Event Items" tab mapping is P8.
- Live test: T-E6.

**B1. Boss ledger and timers**
- Build: classify spawns with `value_num >= BOSS_VALUE_MIN` (300) as bosses. The respawn is `value_num * BOSS_RESPAWN_SCALE` seconds (default 1.0). The ledger is persisted (section C). A map rebuild spawns a boss only if it is alive or due. Option `MOB_RESPAWN_FROM_MAP` (default false) uses `value_num` for regular mobs.
- Unit test: fake clock (kill > leave > map discarded > return before and after 300 s); restart persistence.
- Live test: T-B1, T-B2.

**B2. Drop rates**
- Build: per-entry independent rolls `rate / DROP_RATE_DIVISOR` (100000) over the hni table. Mode switch `DROP_MODE` = 'rates' or 'legacy' (today's 60%). Trophies drop owned by the killer. EN catalog filter stays.
- Unit test: Monte-Carlo (trophy about 99.99%, card about 0.01%); the Wasablanca pair.
- Live test: T-B2, T-B4.

**B3. Boss combat**
- Build: make sure the mobai decision uses attack A/B/dash for boss templates (AI[0]/AI[8]/AI[3]). Damage from Weak/Strong per event. Hyper-armor (AtkType 2) needs no server work. Optional: per-template reach from the hsi attack rectangles at the ChkTime frame.
- Unit test: the decision table for template 81.
- Live test: T-B3.

**B4. Boss quests**
- Build: kill counters for quests whose NPC field is a boss npccode (181/182/184/165/205/215) send 0x59 progress; the trophy quests (6/64/104/137/144) use the existing Demand path.
- Unit test: progress per kill; ReqPro reached sets ready.
- Live test: T-B4.

**B5. Optional announcements (not retail-verified)**
- Build: `BOSS_ANNOUNCE` (default false). S2C 0x15 msg_type 2 "[Announcement] Monkey King has appeared in Ascetic Quarter." on spawn, and "...was defeated by <name>." on kill, to everyone on the channel.
- Live test: T-B5.

**B6. Optional pack assist (not retail-verified)**
- Build: `MOB_PACK_ASSIST` (default false). On a hit, same-template mobs within 200 px with no target aggro on the attacker.
- Live test: T-B6.

## F. Live test plan (two 2009 clients)

Setup:
- Client A: `WindSlayer_patched.exe`, player PA.
- Client B: `WindSlayer_p2.exe` (separate P2P UDP port), player PB.
- Both on the same channel, server with CLIENT_BUILD 2009.
- One change per run, and restart the server between runs (experiment discipline).

**Events**
- **T-E0 (no server change).**
  - Action: PA clicks Eventina in Popola (1800,1494).
  - Expected: window "Event News" opens with the close-beta art; the server log shows **no** C2S. Clicking Nicolas with no event quest held shows at most his bubble.
  - Confirms: A2, A3.
- **T-E1 (notices).**
  - Action: start an event window; both log in.
  - Expected: each sees the centre notice and a teal "[Announcement] ..." chat line once on entry and every N minutes. Text over 88 bytes is rejected by the server.
- **T-E2 (EXP x2).**
  - Action: PA kills a Ssiyo.
  - Expected: "You've received (+20) experience points." (retail +10). PB in a guild (id >= 2) also sees "(+N) guild points are gained." with no stream desync.
  - After the event ends: +10.
- **T-E3 (login gift).**
  - Action: PA logs in during the event.
  - Expected: the line "You've received Love Potion. (Count:5)", 5 potions in the consumable tab.
  - Relog: no second grant; the potions are still there (0x03).
  - PB with a full consumable tab: no line, the server logs a warning, the claim stays open.
- **T-E4 (popup).**
  - Action: both log in during an event with the popup on.
  - Expected: the Event News window pops once per session; Close works; no "Invalid password" box.
  - Portal to another map: no second popup.
- **T-E5 (event quest).**
  - Action: PA logs in during an event with push 158.
  - Expected: the quest log shows "*Monster Card Challenge" with Intro "Windslayer Event is on-going...".
  - With 20 Blue Mushrooms (item 3), a click on Nicolas sends C2S 0x17 158; the server sends 0x27 and 0x21 (+50 exp, +500 gold); the bag gains Card <Koring>. 159 is pushed next.
  - PB without the mushrooms: the click on Nicolas does nothing.
  - Check what quest 158's Finish window (1195) Accept sends (Q4).
- **T-E6 (after P8).**
  - Action: 0x6C with origin 3 for a cash item.
  - Expected: the popup "Congratulation. You received an event item..", with no "50% bonus mileage" popup; the item is in the SC Item Inventory.
  - Test outside the mall and inside it.

**Bosses**
- **T-B1 (spawn and share).**
  - Action: both enter Foothill (218).
  - Expected: one Rynx at about (1609,1835), the same uid on both clients, among the Kamikaze Rats.
  - Name box: red for a Lv 1-4 player, blue for Lv 7-11 (level rule). No title line, no HP bar.
- **T-B2 (kill, drop, respawn).**
  - Action: PA kills Rynx while PB watches.
  - Expected:
    - both see the death animation (0x29) and the corpse removed about 3 s later (0x06);
    - item 124 Rynx's Chest Fur drops, and only PA can pick it up for 15 s (PB gets "You don't have ownership of this item.");
    - Rynx does **not** reappear for 300 s;
    - PB leaves and returns at 200 s: still absent;
    - server restart at 250 s: still absent until 300 s;
    - at 300 s both see a fresh Rynx.
- **T-B3 (boss attacks).**
  - Action: PA (Lv 15+) aggroes the Monkey King at Ascetic Quarter (409).
  - Expected:
    - it chases and swings: attack A (sprite 2 + effect 7, `mon023_attack2.wav`) and attack B (the bigger club slam, sprite 3 + 9, `mon023_attack3.wav`) with 0x2A words 05/06 and 15/16;
    - dashes 19/1A;
    - PA's C2S 0x0D shows events 7 and 9 with source = the King;
    - HP drops by the Weak (24) and Strong (35) based damage;
    - PB sees the same swings.
  - Repeat with Monkey Lord at Village Byway: attack A only.
- **T-B4 (boss quests).**
  - PA takes quest 64 from Mei (Ozi), kills the Monkey King, picks up item 1291, turns in: 0x27, Yellow Stripe Hat, +100 exp.
  - A Rogue at level 30 with quest 182: the kill gives 0x59 progress 1 and "[Way of the Assassin 2] You are ready to complete the quest."
- **T-B5 (optional announce).** With BOSS_ANNOUNCE on, both clients see "[Announcement] Monkey King has appeared..." on respawn and the defeat line on kill; a client on another channel sees nothing.
- **T-B6 (optional assist).** With MOB_PACK_ASSIST on, hitting one Monkey Soldier on Village Byway pulls same-template Soldiers within 200 px. With it off, only the hit mob chases.

## G. Open questions

1. **`value_num` meaning.** Is 300 really a 300 s respawn, and 15-50 seconds for regular mobs? No retail capture exists. What are 901 (one Crow tile) and 9901 (Shovel Frog NPC tiles)? [INFERRED only]
2. **Drop unit.** Is it /100000 (strongly suggested by the 99990 trophies)? Did the retail server roll every entry independently, or pick one?
3. **HUD Gift icon (window 3 ctrl 6) click in 2009.** Is it inert as in 2008 (premium_cash.md Q16), or does it open the mall? The 2009 UI dispatcher case for window 3 was not traced. The gift popup opener FUN_004646c0 has only 2 callers (end of 0x6A, and the gift confirm), so it does not open gifts directly. [VERIFIED for the callers]
4. **Accept buttons inside Finish chains** (quest 158's Finish window 1195, which offers the next challenge). Which quest id does C2S 0x16 carry there: the current dialog quest or the next one?
5. **AI[1], AI[2], AI[4], AI[10..12]** have no client reader. Were they server-side flags (assist, flee, boss, item use)? KR renumbered its AI columns (19 ints), so a direct comparison fails.
6. **AtkType bits 0x01/0x04** have no client reader either. Are they ranged or projectile markers for the server?
7. **"Bomb Rat"** (HOZqk6jDPIc, O-late): a renamed Kamikaze Rat, or the narrator's name for Rynx? No Build 14 data answers it.
8. **Retail pack behaviour.** Was the monkey clustering server assist or AoE aggro? It needs a retail capture or KR server logic.
9. **Retail event delivery.** Were event quests delivered by an updated hqi (SNPC set) through the patcher, or pushed with 0x26? The Build 14 data only proves that SNPC 0 quests cannot be offered.
10. **0x6C outside the mall.** Does origin 3 show the popup and store the record when the Spark Shop was never opened this session? The gate is mall+0x14/+0x18, set at login (FUN_004604c0). Needs T-E6.
11. **"WSP".** Which currency did "EXP/ WSP X2" double? Victy, reputation, or guild points?
12. **Korean-text event quests (232-291).** How do their titles and log lines render in the EN client? Should the server avoid pushing them, or ship EN overrides (data patch)?
