#!/usr/bin/env python3
"""chatsay.py "<text>" - put one line into the game's chat input and send it.

The chat input only accepts keys while it is focused, and a single click on the bar
does not always take (the "Detail of chatting" tooltip eats it). This clicks, verifies
focus by looking for the "To All" scope label that only appears while the input is
open, retries, then types the text and presses Enter.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wsview as V  # noqa: E402

CHAT_BAR = (250, 590)
CHAT_INPUT = (300, 588)                   # inside the open edit field (right of the chip)
SCOPE_LABEL = (10, 580, 95, 598)          # "To All" chip, only drawn while the input is open


def _focused():
    from PIL import ImageGrab
    h = V._find_hwnd()
    x, y, _, _ = V._client_rect_screen(h)
    box = (x + SCOPE_LABEL[0], y + SCOPE_LABEL[1], x + SCOPE_LABEL[2], y + SCOPE_LABEL[3])
    im = ImageGrab.grab(bbox=box).convert('L')
    # the chip is bright text on dark; the closed bar shows the darker "Menu" button
    px = list(im.getdata())
    return sum(1 for p in px if p > 170) > 40


def say(text, tries=4):
    h = V._find_hwnd()
    if not h:
        print('no game window'); return 2
    V._foreground(h)
    V.clear_modifiers()
    for attempt in range(tries):
        if _focused():
            break
        # Enter opens the input when it is closed (and the click alone is unreliable:
        # the "Detail of chatting" tooltip eats it). Alternate Enter and a click, and
        # never type unless the "To All" chip proves the input is really open - stray
        # letters otherwise reach the game as hotkeys and open windows.
        if attempt % 2 == 0:
            V.key_tap(V._vk_of('enter'))
        else:
            x, y, _, _ = V._client_rect_screen(h)
            V.mouse_click_screen(x + CHAT_BAR[0], y + CHAT_BAR[1])
        time.sleep(0.45)
    if not _focused():
        print('could not focus the chat input'); return 1
    # The chip only proves the input is OPEN, not that it has the keyboard: after a modal
    # dialog (e.g. the death/revive box) it stays drawn while keys go to the game as
    # hotkeys (live: "!job 3076" opened the Card Deck and the system menu). A click inside
    # the open edit field takes the focus without closing it.
    x, y, _, _ = V._client_rect_screen(h)
    V.mouse_click_screen(x + CHAT_INPUT[0], y + CHAT_INPUT[1])
    time.sleep(0.3)
    subprocess.run([sys.executable, os.path.join(HERE, 'wsview.py'), 'type', '--enter', text], check=False)
    time.sleep(0.3)
    print(f'sent: {text}')
    return 0


if __name__ == '__main__':
    sys.exit(say(' '.join(sys.argv[1:])))
