#!/usr/bin/env python3
"""
test_records.py - P1 stage 2 offline tests (lc-exp-table, world-player-record
(+party-mp-remote-record), lc-spawn-fields, lc-charlist)

Pure unit tests, no sockets: the exp table against the bytes in WindSlayer.exe, and the
record builders of records.py encoded with the real grammars (packets.build) and decoded
back, so every field is checked on the wire and not in the dict. The flow tests (login
list from the store, own 0x07 at enter world, level-up exactly once) live in
test_handlers.py over the fake client.
"""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import packets as P  # noqa: E402
import progression  # noqa: E402
import records as R  # noqa: E402
import store as storemod  # noqa: E402

# The client the whole protocol was derived from. Only used to re-read the exp tables.
EXE = r'C:\Users\ohdri\Desktop\Windslayer 2\re_tools\WindSlayer.exe'
EXP_INC_VA = 0x6F0C28          # 99 u32 per-level increments (FUN_00440DF0)
EXP_INC_COPY_VA = 0x6F0EB0     # identical copy the exp bar accumulates (FUN_00442AA0)
LEVEL_B4_VA = 0x6F1044         # RegisterLocalPlayer entity+0xB4 (not an exp table)


def read_exe_u32(va, count):
    """`count` u32 at virtual address `va` of WindSlayer.exe, or None when the exe or
    pefile is not available (the table itself is checked by its vectors either way)."""
    try:
        import pefile
    except ImportError:
        return None
    if not os.path.exists(EXE):
        return None
    pe = pefile.PE(EXE, fast_load=True)
    data = pe.get_data(va - pe.OPTIONAL_HEADER.ImageBase, count * 4)
    return struct.unpack(f'<{count}I', data)


class ExpTable(unittest.TestCase):
    """lc-exp-table: the server's curve must be the client's own, or 0x21/0x22 and the
    0x07 level disagree with what the player sees (S1-06)."""

    def test_table_is_the_exe_table(self):
        exe = read_exe_u32(EXP_INC_VA, 99)
        if exe is None:
            self.skipTest(f'pefile or {EXE} not available')
        self.assertEqual(progression.EXP_INC, exe)
        self.assertEqual(read_exe_u32(EXP_INC_COPY_VA, 99), exe, '0x6F0EB0 is the same table')
        # 0x6F0C24[level] (the exp-bar denominator) is the same table shifted by one entry.
        shifted = read_exe_u32(EXP_INC_VA - 4, 100)
        self.assertEqual(shifted[1:], exe)
        # 0x6F1044 is 10 + L//10 for levels 1..99: a per-level entity+0xB4 value, not exp.
        b4 = read_exe_u32(LEVEL_B4_VA, 100)
        self.assertEqual(list(b4[1:100]), [10 + level // 10 for level in range(1, 100)])

    def test_thresholds_and_levels(self):
        # login_character.md 1.7 (read from the exe for this revision).
        self.assertEqual(len(progression.EXP_INC), 99)
        self.assertEqual(sum(progression.EXP_INC), 1_200_382_231)
        for level, total in ((1, 0), (2, 56), (3, 177), (4, 415), (5, 840), (10, 10_260),
                             (12, 21_616), (13, 30_485), (99, 1_200_382_230)):
            with self.subTest(level=level):
                self.assertEqual(progression.exp_for_level(level), total)
                self.assertEqual(progression.level_for_exp(total), level)
                self.assertEqual(progression.level_for_exp(total - 1), max(1, level - 1))
        # The kill-to-level-2 case of the phase exit criteria: 30 exp stays Lv1, 56 is Lv2.
        self.assertEqual([progression.level_for_exp(e) for e in (0, 30, 55, 56, 60, 176, 177)],
                         [1, 1, 1, 2, 2, 2, 3])
        # The old per-level-increments-as-thresholds table said 3 / 5 / 11 / 18 here (B1).
        self.assertEqual([progression.level_for_exp(e) for e in (176, 500, 5000, 30000)],
                         [2, 4, 8, 12])
        self.assertEqual(progression.EXP_MAX, 1_200_382_230)
        self.assertEqual(progression.clamp_exp(progression.EXP_MAX + 10**9), progression.EXP_MAX)
        self.assertEqual(progression.clamp_exp(-5), 0)

    def test_exp_bar_and_stat_total(self):
        self.assertEqual(progression.exp_bar(0), (0, 56))
        self.assertEqual(progression.exp_bar(30), (30, 56))
        self.assertEqual(progression.exp_bar(56), (0, 121))
        self.assertEqual(progression.exp_bar(60), (4, 121))
        self.assertEqual(progression.exp_bar(progression.EXP_MAX), (progression.EXP_MAX, 0))
        self.assertEqual([progression.stat_total(n) for n in (1, 2, 13, 29, 30, 99, 100)],
                         [9, 13, 57, 121, 126, 471, 0])


# ------------------------------------------------------------------- records ---
LOOK = [0, 3, 0, 0, 2, 5, 4, 0, 0, 3, 2, 0, 0, 2]     # compose_look(s10=2, s1=3, s6=4, s5=5, s9=3)


def a_char(**over):
    char = {'name': 'Nova', 'created_at': 0, 'class': 0, 'job2': 0, 'exp': 177, 'look': list(LOOK),
            'str': 5, 'dex': 2, 'int': 1, 'spr': 4, 'fame': 0,
            'map': 101, 'x': 1411.0, 'y': 714.0, 'hp': 100, 'mp': 50}
    char.update(over)
    return char


def an_account(**over):
    acc = {'password': 'x', 'uid': 2, 'gender': 0, 'manner': 0, 'characters': []}
    acc.update(over)
    return acc


class PlayerRecord(unittest.TestCase):
    """world-player-record (+party-mp-remote-record) and lc-spawn-fields."""

    def row(self, rec, key='0x07'):
        """Encode one record with its real grammar and decode it back (exact length)."""
        body = P.build(key, R.player_list(rec))
        self.assertEqual(len(body), 369, f'S2C {key} with one idle record')
        return P.parse(key, body, direction='S2C')['repeat[player_count]'][0]

    def test_every_field_comes_from_the_store(self):
        char = a_char(exp=56, str=7, dex=6, int=5, spr=4)
        account = an_account(uid=2, gender=1, manner=-12)
        session = {'uid': 2, 'hp': 63, 'mp': 21}
        row = self.row(R.player_record(session, char, account))
        self.assertEqual(row['name'], 'Nova')
        self.assertEqual(row['uid'], 2)                       # account uid (0x02 -> scene+0x220)
        self.assertEqual(row['karma'], -12)                   # account manner, not the old i32 1
        self.assertEqual((row['job'], row['job2']), (0, 0))   # Novice
        self.assertEqual(row['level'], 2)                     # from exp 56, never stored
        # the fame rank the owner's client derives itself (0 'Trainee', the fist emblem);
        # P6 lc-player-info: 99 (no emblem) broke the Player Info window's rank label
        self.assertEqual(row['rank_icon'], 0)
        self.assertEqual(self.row(R.player_record(session, a_char(fame=30), account))['rank_icon'], 1)
        self.assertEqual(row['gender'], 1)                    # "(M)" items need the flag
        self.assertEqual([e['appearance_part'] for e in row['repeat[14]']], LOOK)
        self.assertEqual((row['stat_str'], row['stat_dex'], row['stat_int'], row['stat_tol']),
                         (7, 6, 5, 4))
        self.assertEqual((row['cur_hp'], row['cur_mp']), (63, 21))     # not 0 (renders dead)
        self.assertEqual((row['pos_x'], row['pos_y']), (1411.0, 714.0))
        self.assertEqual((row['room_id'], row['gm_level'], row['shop_open']), (0, 0, 0))
        self.assertEqual((row['buff_count'], row['skill_count']), (0, 0))

    def test_idle_motion_block_has_no_ip_octets(self):
        row = self.row(R.player_record({'uid': 1}, a_char(), an_account(uid=1)))
        # RegisterLocalPlayer @0x422120 writes exactly this idle state.
        self.assertEqual({k: row[k] for k in R.IDLE_MOTION}, R.IDLE_MOTION)
        # The old builder sent 954=32, 8cf=1, 904=0, e00=501 and 127/0/0/1 at +0x8B3 (S2-27).
        self.assertEqual([row['input_state_8b3'], row['input_state_8b4'],
                          row['input_state_8b5'], row['input_state_8b6']], [8, 0, 0, 0])
        self.assertEqual(row['action_timer_e00'], 0)

    def test_look_is_sent_verbatim_with_nothing_merged(self):
        """The stored look IS the composed appearance (inventory.compose runs at every equip
        and unequip): the legacy `weapon` key - which the old builder merged into slot 11,
        and which a STARTER_WEAPON once filled with an ITEM id - is no input any more."""
        row = self.row(R.player_record({'uid': 1}, a_char(weapon=0xB3), an_account(uid=1)))
        self.assertEqual([e['appearance_part'] for e in row['repeat[14]']], LOOK)
        # A record with no look at all still renders a body (creation defaults, not zeros),
        # and the defaults are those of the gender the record carries.
        naked = a_char()
        del naked['look']
        row = self.row(R.player_record({'uid': 1}, naked, an_account(uid=1)))
        self.assertEqual([e['appearance_part'] for e in row['repeat[14]']], storemod.default_look(0))
        row = self.row(R.player_record({'uid': 1}, naked, an_account(uid=1, gender=1)))
        self.assertEqual((row['gender'], [e['appearance_part'] for e in row['repeat[14]']]),
                         (1, storemod.default_look(1)))

    def test_record_gender_is_the_account_flag_in_2008_and_the_characters_own_in_2009(self):
        self.assertEqual(R.record_gender({'gender': 1}, {'gender': 0}), 0)
        self.assertEqual(R.record_gender({'gender': 0}, {'gender': 1}), 1)
        self.assertEqual(R.record_gender({'gender': 1}, {'gender': 0}, R.BUILD_2009), 1)
        self.assertEqual(R.record_gender({}, {'gender': 1}, R.BUILD_2009), 1)   # migrated 2008 record

    def test_the_2008_cash_half_carries_the_worn_costumes(self):
        """+0x15C u16[9] = grid slots 16..24. The receiving client composes this player's
        later 0x1D / 0x1E from its copy of them (the costume beats the regular item), so a
        costume the model holds must be in the row - a grid entry wins over the legacy list."""
        char = a_char(equipped={17: {'id': 3126, 'w': [0] * 6}}, cash_equip=[0, 55, 0, 0, 0, 0, 0, 0, 9])
        row = self.row(R.player_record({'uid': 1}, char, an_account(uid=1)))
        self.assertEqual([e['cash_equip_item_id'] for e in row['repeat[9]']],
                         [0, 3126, 0, 0, 0, 0, 0, 0, 9])

    def test_equip_grid_carries_ids_and_option_words(self):
        session = {'uid': 1, 'equip_grid': {5: 179}}
        char = a_char(equipped={4: {'id': 200, 'w': [7, 9, 0, 0, 0, 0]}})
        row = self.row(R.player_record(session, char, an_account(uid=1)))
        grid = [(e['equip_item_id'], [w['equip_item_attr'] for w in e['repeat[6]']])
                for e in row['repeat[16]']]
        self.assertEqual(len(grid), 16)
        self.assertEqual(grid[5], (179, [0] * 6))                      # live equip handler
        self.assertEqual(grid[4], (200, [7, 9, 0, 0, 0, 0]))           # persisted option block
        self.assertEqual(grid[0], (0, [0] * 6))
        self.assertEqual([e['cash_equip_item_id'] for e in row['repeat[9]']], [0] * 9)

    def test_buffs_and_skills(self):
        session = {'uid': 1, 'buffs': [{'id': 0x100, 'remaining_ms': 5000},
                                       {'id': 0x0A31, 'remaining_ms': 9000, 'x': 300, 'y': 400}]}
        char = a_char(skills=[2188, 194])
        body = P.build('0x07', R.player_list(R.player_record(session, char, an_account(uid=1))))
        row = P.parse('0x07', body, direction='S2C')['repeat[player_count]'][0]
        self.assertEqual(row['buff_count'], 2)
        self.assertEqual(row['repeat[buff_count]'][0],
                         {'buff_skill_id': 0x100, 'buff_duration': 5000})
        # A ground-point buff id carries its x,y (grammar gate 0x0A31..0x0A3B).
        self.assertEqual(row['repeat[buff_count]'][1],
                         {'buff_skill_id': 0x0A31, 'buff_duration': 9000,
                          'buff_param_a': 300, 'buff_param_b': 400})
        self.assertEqual([s['skill_id'] for s in row['repeat[skill_count]']], [2188, 194])
        self.assertEqual(len(body), 369 + 2 * 6 + 4 + 2 * 2)

    def test_position_falls_back_from_argument_to_char_to_session(self):
        char, account = a_char(), an_account(uid=1)
        row = self.row(R.player_record({'uid': 1}, char, account, pos=(10.5, -3.0)))
        self.assertEqual((row['pos_x'], row['pos_y']), (10.5, -3.0))
        drifting = a_char()
        del drifting['x'], drifting['y']
        row = self.row(R.player_record({'uid': 1, 'pos': (77.0, 88.0)}, drifting, account))
        self.assertEqual((row['pos_x'], row['pos_y']), (77.0, 88.0))

    def test_0x04_is_the_same_record_and_0x05_is_the_key_map(self):
        session = {'uid': 2, 'hp': 41, 'mp': 7, 'equip_grid': {5: 179}}
        char = a_char(exp=30485, skills=[194])
        rec = R.player_record(session, char, an_account(uid=2, manner=3))
        self.assertEqual(P.build('0x04', R.player_list(rec)), P.build('0x07', R.player_list(rec)))
        body = P.build('0x05', R.to_0x05(rec))
        row = P.parse('0x05', body, direction='S2C')
        self.assertEqual(len(body), 369 - 1 - 1 + 2)   # no count byte, no shop_open, +1 skill
        self.assertEqual((row['name'], row['uid'], row['manner_points']), ('Nova', 2, 3))
        self.assertEqual((row['job1'], row['job2'], row['level'], row['rank_emblem']), (0, 0, 13, 0))
        self.assertEqual((row['cur_hp'], row['cur_mp']), (41, 7))
        self.assertEqual((row['motion_id'], row['attack_dir_8cf'], row['facing_dir'],
                          row['input_dir_8b3']), (8, 8, 2, 8))
        self.assertEqual([e['appearance'] for e in row['repeat[14]']], LOOK)
        self.assertEqual(row['repeat[16]'][5]['equip_item_id'], 179)
        self.assertEqual([e['extra_equip_item_id'] for e in row['repeat[9]']], [0] * 9)
        self.assertEqual([s['skill_id'] for s in row['repeat[skill_count]']], [194])

    def test_remote_record_uses_the_last_relayed_motion(self):
        session = {'uid': 3, 'hp': 10, 'mp': 10,
                   'motion': {'action_state_904': 3, 'direction_8bd': 1, 'input_state_8b3': 4}}
        local = self.row(R.player_record(session, a_char(), an_account(uid=3)))
        self.assertEqual((local['action_state_904'], local['direction_8bd']), (8, 2))  # own = idle
        remote = self.row(R.player_record(session, a_char(), an_account(uid=3), remote=True))
        self.assertEqual((remote['action_state_904'], remote['direction_8bd'],
                          remote['input_state_8b3']), (3, 1, 4))
        self.assertEqual(remote['anim_substate_8cf'], 8)          # untouched keys stay idle

    def test_gm_flag_adds_the_hidden_byte(self):
        # gm_level 1 is the only value that makes the client read gm_hidden (gate 0x44F093);
        # chat_mail_gm-gm-flag-manner sets char['gm'] / char['gm_hidden'].
        rec = R.player_record({'uid': 1}, a_char(gm=True, gm_hidden=True), an_account(uid=1))
        body = P.build('0x07', R.player_list(rec))
        row = P.parse('0x07', body, direction='S2C')['repeat[player_count]'][0]
        self.assertEqual(len(body), 370)
        self.assertEqual((row['gm_level'], row['gm_hidden']), (1, True))

    def test_a_packet_never_carries_more_than_five_records(self):
        rec = R.player_record({'uid': 1}, a_char(), an_account(uid=1))
        self.assertEqual(P.build('0x04', R.player_list([rec] * 5))[:1], b'\x05')
        with self.assertRaises(ValueError):
            R.player_list([rec] * 6)


class CharacterList(unittest.TestCase):
    """lc-charlist: S2C 0x02 from the store, 13 + 75n bytes and nothing after it."""

    def build(self, account, uid=2):
        body = P.build('0x02', R.character_list(uid, account))
        return body, P.parse('0x02', body, direction='S2C')

    def test_fields_and_length(self):
        account = an_account(uid=2, gender=1, manner=-7, cash_first_purchase=1, characters=[
            a_char(name='Nova', exp=0, look=list(LOOK)),
            a_char(name='Tier2', **{'class': 1, 'job2': 2, 'exp': 30_485}),
        ])
        body, rec = self.build(account)
        self.assertEqual(len(body), 13 + 75 * 2)          # S2-46: no 16 trailing bytes
        self.assertEqual((rec['result'], rec['account_id']), (1, 2))
        self.assertEqual(rec['cash_first_purchase_flag'], 1)
        self.assertEqual(rec['account_gender_flag'], 1)   # was hard-coded 0 (B15)
        self.assertEqual(rec['manner_points'], -7)
        self.assertEqual(rec['transfer_status'], 3)
        nova, tier2 = rec['repeat[char_count]']
        self.assertEqual((nova['name'], nova['class_id'], nova['job_branch']), ('Nova', 0, 0))
        self.assertEqual(nova['total_exp'], 0)            # select screen shows Lv.1
        self.assertEqual([e['appearance'] for e in nova['repeat[14]']], LOOK)
        # job_branch is the job tier, not the level (B5), and the level comes from total_exp.
        self.assertEqual((tier2['class_id'], tier2['job_branch'], tier2['total_exp']),
                         (1, 2, 30_485))
        self.assertEqual(progression.level_for_exp(tier2['total_exp']), 13)
        for row in (nova, tier2):
            self.assertEqual([row[k] for k in ('rank_1', 'rank_2', 'rank_3',
                                               'rank_change_1', 'rank_change_2', 'rank_change_3')],
                             [0] * 6)

    def test_empty_account_and_the_five_character_cap(self):
        body, rec = self.build(an_account())
        self.assertEqual((len(body), rec['char_count']), (13, 0))
        account = an_account(characters=[a_char(name=f'C{i}') for i in range(7)])
        body, rec = self.build(account)
        self.assertEqual(rec['char_count'], R.MAX_CHARACTERS)
        self.assertEqual([c['name'] for c in rec['repeat[char_count]']], ['C0', 'C1', 'C2', 'C3', 'C4'])
        self.assertEqual(len(body), 13 + 75 * 5)

    def test_a_long_name_stays_inside_its_field(self):
        account = an_account(characters=[a_char(name='X' * 40)])
        body, rec = self.build(account)
        self.assertEqual(len(body), 13 + 75)
        self.assertEqual(rec['repeat[char_count]'][0]['name'], 'X' * 16)   # name17: 16 + NUL
        # The first character row starts after the 12-byte account header.
        self.assertEqual(body[12:12 + 17], b'X' * 16 + b'\x00')

    def test_exp_is_clamped_to_the_client_range(self):
        account = an_account(characters=[a_char(exp=progression.EXP_MAX * 4)])
        _, rec = self.build(account)
        self.assertEqual(rec['repeat[char_count]'][0]['total_exp'], progression.EXP_MAX)


if __name__ == '__main__':
    unittest.main(verbosity=1)
