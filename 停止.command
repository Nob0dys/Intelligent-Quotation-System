#!/bin/zsh
# 智能报价系统 — macOS 一键停止

echo "正在停止智能报价系统..."

# 停止前端 dev 服务器（vinext/vite）
pkill -f "vinext dev" 2>/dev/null
pkill -f "vite" 2>/dev/null && true

# 停止后端 uvicorn
pkill -f "uvicorn app.main:app" 2>/dev/null && true

sleep 1
if lsof -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 || lsof -iTCP:3000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[警告] 仍有端口被占用（8000/3000），可能不是本系统的进程。"
    lsof -iTCP:8000 -sTCP:LISTEN 2>/dev/null
    lsof -iTCP:3000 -sTCP:LISTEN 2>/dev/null
else
    echo "已停止。"
fi

sleep 2
