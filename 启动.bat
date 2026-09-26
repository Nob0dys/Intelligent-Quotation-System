@echo off
chcp 936 >nul
title 智能报价系统

echo ==========================================
echo   智能报价系统 — 一键启动
echo ==========================================
echo.

:: 检查 Node.js
where node >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Node.js，请先安装 Node.js 22+
    echo 下载地址: https://nodejs.org/
    pause
    exit /b 1
)

:: 检查 Python（使用 py 启动器，避开微软商店占位程序）
where py >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.12+
    echo 下载地址: https://python.org/
    pause
    exit /b 1
)
py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [警告] 未检测到 Python 3.12+，尝试使用默认 Python
)

:: 安装后端 Python 依赖
echo [1/4] 安装后端 Python 依赖...
cd /d "%~dp0demo-app\backend"
py -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo [警告] Python 依赖安装部分失败，后端可能缺少依赖
)

:: 安装前端依赖
echo [2/4] 安装前端依赖（npm ci）...
cd /d "%~dp0demo-app"
call npm ci
if errorlevel 1 (
    echo [错误] npm ci 失败，请检查 Node.js 版本是否 >= 22
    pause
    exit /b 1
)

:: 启动后端（仅监听本机回环，不对局域网暴露；API 由前端代为转发）
echo [3/4] 启动后端 API（127.0.0.1:8000，仅本机）...
cd /d "%~dp0demo-app\backend"
start "报价系统-后端" cmd /k "title 报价系统-后端(API:127.0.0.1:8000) && py -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"

:: 等待后端启动
echo       等待后端就绪（5秒）...
timeout /t 5 /nobreak >nul

:: 启动前端（对局域网开放：同网段其他电脑可通过本机 IP 访问）
echo [4/4] 启动前端（0.0.0.0:3000，局域网可访问）...
cd /d "%~dp0demo-app"
start "报价系统-前端" cmd /k "title 报价系统-前端(LAN) && npm run dev -- --hostname 0.0.0.0"

:: 获取本机局域网 IPv4 地址（排除回环 127.x 与自动专用 169.254.x）
set "LAN_IP="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 ^| Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.IPAddress -notmatch '^169\.254\.' } ^| Select-Object -First 1 -ExpandProperty IPAddress"`) do set "LAN_IP=%%i"

echo.
echo ==========================================
echo   启动完成！
echo.
echo   本机访问:   http://localhost:3000
if defined LAN_IP (
    echo   局域网访问: http://%LAN_IP%:3000
) else (
    echo   局域网访问: 未检测到局域网 IP，可运行 ipconfig 手动查询
)
echo   后端地址:   http://127.0.0.1:8000 （仅本机，API 由前端转发）
echo   默认账号:   admin / admin123
echo ==========================================
echo.
echo   关闭此窗口不影响运行。
echo   要停止系统，关闭「报价系统-前端」和「报价系统-后端」窗口即可。
echo.
pause
