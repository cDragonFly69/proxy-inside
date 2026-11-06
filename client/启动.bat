@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  双击运行即可：自动请求管理员权限 -> 调用 run-tun.ps1
rem  需要自定义时改下面两行，或直接运行 run-tun.ps1
rem ============================================================
set "TOKEN=s3cret"
set "SERVER=10.200.1.26"

cd /d "%~dp0"

rem 提权：已在管理员会话里就直接往下走
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在请求管理员权限 ...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ============================================================
echo   代理客户端启动器
echo   跳板机: %SERVER%
echo   令牌  : %TOKEN%
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-tun.ps1" -Server %SERVER% -Password %TOKEN%

echo.
echo 客户端已退出。
pause
