combo_art - optional art for the combo HUD patch (combo_hud_2009.py), EN 2009 client Build 14 (v2, 2026-09-24)

NOT INCLUDED IN THIS REPO: the four art files below (combo001.hsi/.hsc/.hsc1/.hsc2) are built
locally and are not distributed. Without them combo_hud_2009.py draws its built-in fallback (the
game's own green digits plus text), so --install-assets only applies if you have the art.


  combo001.hsi        sprite index (79 sprites, client SHA-1 footer); image 1 = combo001_a8.hsc,
                      image 2 = ./hs/fx003_a8.hsc (shipped with the game, the green damage digits)
  combo001_a8.hsc     512x256 DXT5 atlas (D3D)
  combo001_a8.hsc1    8-bit BMP fallback (D2D)
  combo001_a8.hsc2    16-bit BMP fallback (D2D)
  combo_art_manifest.json   sprite ids, regions, anchors, grade-word frames and the 800x600 layout

Install (copies the three textures first and combo001.hsi last, never overwrites a different file):
  python combo_hud_2009.py --install-assets
Uninstall: delete hs\combo001.hsi first, then hs\combo001_a8.hsc, .hsc1 and .hsc2.
Upgrading from v1 (32 sprites, 512x128): uninstall the v1 files the same way first; --install-assets
  refuses to overwrite files that differ.  v1 sprites 0..31 keep their ids, regions and texels in
  v2 (only the pop sprites +11..+21 have the retimed 1.45x/0.9x/1.0x pop), so a v1 combo_hud patch
  keeps working with the v2 files.
Without these files the patch draws its built-in fallback (shipped green digits + text).

Ids when combo001.hsi is loaded right after basic.hsi (base 342; offset = id - base):
  342..351  (+0..+9)    green digits 0-9            352 (+10) green "Combo !"
  353..362  (+11..+20)  green digit pop             363 (+21) green word pop
  364..373  (+22..+31)  legacy damage digits x1.4
  374 (+32) GOOD 80x28   375 (+33) BAD 68x28   376 (+34) CRITICAL! 138x32   (3 frames, 860 ms, like 0x14C)
  377..386  (+35..+44)  orange digits 0-9           387 (+45) orange "Combo !"
  388..397  (+46..+55)  orange digit pop            398 (+56) orange word pop
  399..408  (+57..+66)  red digits 0-9              409 (+67) red "Combo !"
  410..419  (+68..+77)  red digit pop               420 (+78) red word pop
  counter set = base + {0 green, 35 orange, 57 red}; inside a set: digit +d, word +10, digit pop +11+d, word pop +21
  (orange from 20 is exact in the 2012 EN client; red from ~50 comes from 2011 KR footage only: opt-in)
  map sprites loaded later start at 421.
GetExtent probes: +0 and +35 and +57 give 40x52, +10/+45/+67 give 76x26, +32 GOOD 80x28.
Grade words: spawn as a kind-10 effect on the attacker (FUN_0042cd90(0x54EBD0, id, uid, 0,0,0, 1, 10, 0));
  their ink centre stays at (0, -120) from the entity's draw point through the 1.5x / 0.9x pop.

The glyphs were rendered from Windows fonts (Agency FB Bold AGENCYB.TTF, Eras Bold ITC ERASBD.TTF, Franklin Gothic Heavy Italic FRAHVIT.TTF).
Check their licence before publishing these files.
