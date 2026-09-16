#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if ! command -v node >/dev/null 2>&1; then
  echo "未找到 Node.js，请先安装 Node.js 22 或更高版本。"
  read -k 1 "?按任意键退出..."
  exit 1
fi

if [ ! -x backend/.venv/bin/uvicorn ]; then
  echo "首次运行：创建 Python 虚拟环境并安装后端依赖..."
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -r backend/requirements-dev.txt
fi

if [ ! -d node_modules ]; then
  echo "首次运行：安装前端依赖..."
  npm ci --no-audit --no-fund
fi

API_PID=""
cleanup() {
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true
  # 守护循环也可能在 sleep，一并结束本脚本即可
  exit 0
}
trap cleanup EXIT INT TERM

api_healthy() {
  curl -sf -o /dev/null http://127.0.0.1:8000/docs
}

start_api() {
  (cd backend && DATABASE_URL=sqlite:///./data/quote_saitel.db exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000) &
  API_PID=$!
}

# 后端已健康运行则直接复用，否则启动并等待就绪
if api_healthy; then
  echo "检测到后端 API 已在运行（http://127.0.0.1:8000），直接复用。"
else
  start_api
  echo "等待后端 API 就绪（http://127.0.0.1:8000，数据库 quote_saitel）..."
  READY=0
  for i in {1..30}; do
    if api_healthy; then READY=1; break; fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
      echo "后端进程启动失败，正在重试..."
      start_api
    fi
    sleep 1
  done
  if [ "$READY" != "1" ]; then
    echo "后端 API 启动失败，请把上方报错信息截图反馈给开发。"
    read -k 1 "?按任意键退出..."
    exit 1
  fi
  echo "后端 API 已就绪"
fi

# 守护：后端意外退出时自动重启，避免前端报 502
supervise_api() {
  while true; do
    sleep 5
    if [ -z "$API_PID" ]; then
      # 启动时复用了已有后端；它若已不可达则接管重启
      if ! api_healthy; then
        echo "$(date '+%H:%M:%S') 后端 API 不可用，正在自动重启..."
        start_api
      fi
    elif ! kill -0 "$API_PID" 2>/dev/null; then
      echo "$(date '+%H:%M:%S') 后端 API 意外退出，正在自动重启..."
      start_api
    fi
  done
}
supervise_api &
SUP_PID=$!

# 前端已在运行则只打开页面；否则由本脚本启动 dev server
if curl -sf -o /dev/null http://localhost:3000/; then
  echo "检测到前端已在运行（http://localhost:3000），直接打开页面。"
  open "http://localhost:3000/"
  echo "本窗口可最小化保持运行（守护后端）；关闭本窗口将停止守护与后端。"
  wait "$API_PID" 2>/dev/null || true
  wait "$SUP_PID" 2>/dev/null || true
else
  (sleep 3; open "http://localhost:3000/") &
  npm run dev
fi
