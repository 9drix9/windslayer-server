"""
Data tables loaded from the KR Yahoo client dumps.

Lazy-loaded singletons. Import the names you need:

    from data import items, npcs, map_codes, opcodes

    items[1]['title']           # '원숭이모자장식'
    npcs[1]['HP']               # '5'
    map_codes[101]['file_name'] # 'stage01_01'
    opcodes['0x01']             # 'Main_Tcp_Handler'

Keys in items/npcs/map_codes are integers (the JSON files store them as
strings, this module coerces). opcodes keys are kept as '0xNN' strings
to match the source format.

All tables are read-only. Do not mutate.
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


class _LazyTable:
    """Lazy JSON loader. First access reads the file; subsequent accesses are cached."""

    def __init__(self, filename, key_as_int=True):
        self._filename = filename
        self._key_as_int = key_as_int
        self._data = None

    def _ensure(self):
        if self._data is None:
            path = os.path.join(_HERE, self._filename)
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            if self._key_as_int:
                self._data = {int(k): v for k, v in raw.items()}
            else:
                self._data = raw

    def __getitem__(self, key):
        self._ensure()
        return self._data[key]

    def get(self, key, default=None):
        self._ensure()
        return self._data.get(key, default)

    def __contains__(self, key):
        self._ensure()
        return key in self._data

    def __len__(self):
        self._ensure()
        return len(self._data)

    def __iter__(self):
        self._ensure()
        return iter(self._data)

    def items(self):
        self._ensure()
        return self._data.items()

    def keys(self):
        self._ensure()
        return self._data.keys()

    def values(self):
        self._ensure()
        return self._data.values()


items = _LazyTable('items.json', key_as_int=True)
npcs = _LazyTable('npcs.json', key_as_int=True)
map_codes = _LazyTable('map_codes.json', key_as_int=True)
opcodes = _LazyTable('opcodes.json', key_as_int=False)


def map_filename(file_mapcode):
    """Return the .hmi filename the client will try to load for a given mapcode.

    file_mapcode is the integer the server sends in the wire field.
    Returns 'stage01_01' (no extension) or None if unknown.
    """
    entry = map_codes.get(file_mapcode)
    return entry['file_name'] if entry else None


def map_name(file_mapcode):
    """Return the human-readable map name (Korean) or None."""
    entry = map_codes.get(file_mapcode)
    return entry['name'] if entry else None
