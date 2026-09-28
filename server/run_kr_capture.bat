@echo off
REM One-click admin capture launcher. Double-click this file.
REM It'll pop a UAC prompt for admin — click Yes — and the capture runs.

setlocal

REM Check if already running as admin
net session >nul 2>&1
if %errorLevel% == 0 (
    goto :ADMIN
)

REM Not admin — re-launch this script as admin via PowerShell
echo Requesting administrator privileges...
powershell -Command "Start-Process '%~f0' -Verb RunAs"
exit /b

:ADMIN
echo Running as administrator.
cd /d "%~dp0"
echo.
echo ============================================================
echo   Korean WindSlayer packet capture
echo ============================================================
echo   1. Launch WindSlayer Korean client in another window
echo   2. Log in, pick a character, enter the world
echo   3. Wait until you are in-game (see your character moving)
echo   4. Come back here and press Ctrl+C to stop capture
echo ============================================================
echo.
python kr_capture.py
pause
