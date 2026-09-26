#!/bin/zsh
# 智能报价系统 — macOS 一键启动
# 双击运行：检查环境 → 安装依赖 → 启动后端(8000) + 前端(3000) → 打开浏览器

set -u

SCRIPT_DIR="${0:A:h}"
BACKEND_DIR="$SCRIPT_DIR/demo-app/backend"
FRONTEND_DIR="$SCRIPT_DIR/demo-app"
VENV_DIR="$BACKEND_DIR/.venv"
BACKEND_LOG="$SCRIPT_DIR/data/backend.log"
FRONTEND_LOG="$SCRIPT_DIR/data/frontend.log"

echo "=========================================="
echo "  智能报价系统 — 一键启动 (macOS)"
echo "=========================================="
echo ""

# --- 检查 Node.js（>= 22） ---
if ! command -v node >/dev/null 2>&1; then
    echo "[错误] 未检测到 Node.js，请先安装 Node.js 22+"
    echo "       下载地址: https://nodejs.org/  或运行: brew install node"
    read -k 1 "?按任意键退出..."
    exit 1
fi
NODE_MAJOR=$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)
if [ "$NODE_MAJOR" -lt 22 ]; then
    echo "[错误] Node.js 版本过低（$(node -v)），需要 >= 22"
    read -k 1 "?按任意键退出..."
    exit 1
fi
echo "[✓] Node.js $(node -v)"

# --- 查找 Python（>= 3.11） ---
PYTHON=""
for cmd in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$cmd" >/dev/null 2>&1; then
        ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
        major=${ver%%.*}
        minor=${ver##*.}
        if [ -n "$ver" ] && [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    echo "[错误] 未检测到 Python 3.11+，请先安装"
    echo "       下载地址: https://python.org/  或运行: brew install python"
    read -k 1 "?按任意键退出..."
    exit 1
fi
echo "[✓] Python $($PYTHON --version | awk '{print $2}') ($PYTHON)"

mkdir -p "$SCRIPT_DIR/data"

# --- 准备虚拟环境并安装后端依赖 ---
echo "[1/4] 安装后端 Python 依赖..."
if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$PYTHON" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q --upgrade pip >/dev/null 2>&1
if ! "$VENV_DIR/bin/pip" install -q -r "$BACKEND_DIR/requirements.txt"; then
    echo "[警告] Python 依赖安装部分失败，后端可能缺少依赖"
fi

# --- 安装前端依赖 ---
echo "[2/4] 安装前端依赖（npm ci）..."
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    if ! (cd "$FRONTEND_DIR" && npm ci --no-audit --no-fund); then
        echo "[错误] npm ci 失败，请检查网络或 Node.js 版本"
        read -k 1 "?按任意键退出..."
        exit 1
    fi
else
    echo "      node_modules 已存在，跳过（如需重装请先删除该目录）"
fi

# --- 启动后端 ---
echo "[3/4] 启动后端 API（端口 8000）..."
if lsof -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "      端口 8000 已被占用，假定后端已在运行"
else
    (cd "$BACKEND_DIR" && nohup "$VENV_DIR/bin/python" -m uvicorn app.main:app \
        --host 127.0.0.1 --port 8000 >> "$BACKEND_LOG" 2>&1 &)
fi

# --- 启动前端 ---
echo "[4/4] 启动前端（端口 3000）..."
if lsof -iTCP:3000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "      端口 3000 已被占用，假定前端已在运行"
else
    (cd "$FRONTEND_DIR" && nohup npm run dev >> "$FRONTEND_LOG" 2>&1 &)
fi

# --- 等待前端就绪并打开浏览器 ---
echo "      等待服务就绪..."
for i in {1..30}; do
    if curl -s -o /dev/null http://127.0.0.1:3000/; then
        break
    fi
    sleep 1
done
open "http://localhost:3000" 2>/dev/null || true

echo ""
echo "=========================================="
echo "  启动完成！"
echo ""
echo "  前端地址: http://localhost:3000"
echo "  后端地址: http://127.0.0.1:8000"
echo "  默认账号: admin / admin123"
echo ""
echo "  后端日志: $BACKEND_LOG"
echo "  前端日志: $FRONTEND_LOG"
echo ""
echo "  本窗口关闭不影响运行。"
echo "  要停止系统，双击运行「停止.command」。"
echo "=========================================="
echo ""

# 让终端窗口停留几秒，方便阅读提示
sleep 3
