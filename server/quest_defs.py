"""Quest content loaded from gamedef.sqlite3 (quests table).
Columns decoded: Demand/Send = 10x u16 ids (4 hex each); Demand_Num/Send_Num =
10x 3-hex counts; Reward = 10x u16 ids; Reward_Num = 10x 6-hex (count = first 3 hex).
"""
import os
import sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))
GAMEDEF = os.path.join(_HERE, 'gamedef.sqlite3')
if not os.path.exists(GAMEDEF):
    GAMEDEF = r'C:\Users\ohdri\Desktop\PySlayer\gamedef.sqlite3'


def _ids(s):
    s = (s or '').ljust(40, '0')
    return [int(s[i:i+4], 16) for i in range(0, 40, 4)]

def _num3(s):
    s = (s or '').ljust(30, '0')
    return [int(s[i:i+3], 16) for i in range(0, 30, 3)]

def _num6(s):
    s = (s or '').ljust(60, '0')
    return [int(s[i:i+6][:3], 16) for i in range(0, 60, 6)]

def _pairs(ids, nums):
    return [(i, n) for i, n in zip(ids, nums) if i and n]


_CACHE = None

def load_quests():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    out = {}
    try:
        c = sqlite3.connect(GAMEDEF); c.row_factory = sqlite3.Row
        for r in c.execute('SELECT * FROM quests'):
            out[r['idx']] = dict(
                idx=r['idx'], snpc=r['SNPC'] or 0, enpc=r['ENPC'] or 0,
                demand=_pairs(_ids(r['Demand']), _num3(r['Demand_Num'])),
                reward=_pairs(_ids(r['Reward']), _num6(r['Reward_Num'])),
                send=_pairs(_ids(r['Send']), _num3(r['Send_Num'])),
                exp=r['Exp'] or 0, money=r['Money'] or 0,
                start_lev=r['Start_Lev'] or 0, end_lev=r['End_Lev'] or 0,
                next_q=r['NextQuest'] or 0, prev_q=r['PrevQuest'] or 0)
        c.close()
    except Exception:
        pass
    _CACHE = out
    return out
