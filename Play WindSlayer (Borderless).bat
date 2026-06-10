@echo off
REM One-click WindSlayer launcher: starts the client NON-elevated and an
REM auto-borderless watcher that makes the game borderless-fullscreen once
REM you're in-world (and re-applies it after map changes).
REM   - borderless windowed => alt-tab / app-switching works (unlike exclusive)
REM   - 800x600 backbuffer stretched by D3D9 to fill the monitor; HUD preserved
REM Change "fill" to "fit" below if you want pillarboxed 4:3 (no stretch).

setlocal
set "__COMPAT_LAYER=RunAsInvoker"
cd /d "%~dp0"

start "" "WindSlayer_patched.exe"
start "WS borderless" /min python "server\wsview.py" autoborderless fill

endlocal
