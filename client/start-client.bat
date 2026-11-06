@echo off
setlocal

rem ============================================================
rem  Double-click to run. Self-elevates to Administrator,
rem  then calls run-tun.ps1.
rem
rem  Edit the two lines below if you need a different host/token.
rem ============================================================
set "TOKEN=s3cret"
set "SERVER=10.200.1.26"

cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator privileges ...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ============================================================
echo   Proxy client launcher
echo   Jump host : %SERVER%
echo   Token     : %TOKEN%
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-tun.ps1" -Server %SERVER% -Password %TOKEN%

echo.
echo Client exited.
pause
