@echo off
rem play_2009.bat - start the patched EN 2009 client (Outspark v1.04 Build 14) against the
rem private server (CLIENT_BUILD "2009" in server\config.json).
rem
rem   play_2009.bat [account] [password]        default: test test
rem
rem The 2009 client has no ID/password form: it needs three launcher (SSO) arguments and
rem sends the FIRST one in its C2S 0x01 login. The server reads "<account> <password>"
rem from it (client-2009-login), so the command line is
rem   WindSlayer_patched.exe -<account> <password> -x -x
rem Arguments 2 and 3 only have to be non-empty. The password is visible in the process
rem command line: use this for test accounts only.
rem __COMPAT_LAYER=RunAsInvoker starts it non-elevated (the exe asks for admin otherwise).
setlocal
set "USER=%~1"
if "%USER%"=="" set "USER=test"
set "PASS=%~2"
if "%PASS%"=="" set "PASS=test"
set __COMPAT_LAYER=RunAsInvoker
cd /d "%~dp0"
if not exist "WindSlayer_patched.exe" (
    echo WindSlayer_patched.exe not found in %~dp0 - build it with: python patch_2009.py
    pause
    exit /b 1
)
echo Starting WindSlayer 2009 as "%USER%" (make sure the server is running first)...
start "" "%~dp0WindSlayer_patched.exe" -%USER% %PASS% -x -x
endlocal
