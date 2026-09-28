@echo off
title WindSlayer Private Server
echo ============================================
echo  WindSlayer Private Server
echo ============================================
echo.
echo Starting server on ports 7011 (version), 7022 (game) and 127.0.0.1:7099 (admin)...
echo.
cd /d "%~dp0"
python windslayer_server.py
pause
