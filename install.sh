#!/usr/bin/env bash
# life-lens 一键安装脚本
#
# 用法:
#     curl -fsSL https://raw.githubusercontent.com/uxtracer/life-lens/main/install.sh | bash
#
# 这个脚本只负责:
#   1. 检查 Python >= 3.9 和 git
#   2. 克隆代码到 ~/life-lens/(可用 LIFE_LENS_HOME 覆盖)
#   3. 创建 venv 并安装 Python 依赖(包括 ~700MB 的 ML 依赖)
#   4. 启动 lens 服务并打开浏览器
#
# 不负责:
#   - Ollama 安装(打开 web 后,配置卡片会引导你装)
#   - 高德 API key / 对话模型 key(同上,配置卡片里填)

set -euo pipefail

REPO_URL="${LIFE_LENS_REPO:-https://github.com/uxtracer/life-lens.git}"
INSTALL_DIR="${LIFE_LENS_HOME:-$HOME/life-lens}"
PORT="${LIFE_LENS_PORT:-7878}"

G='\033[0;32m'; Y='\033[0;33m'; R='\033[0;31m'; B='\033[0;36m'; N='\033[0m'
say()  { printf "${B}==>${N} %s\n" "$*"; }
ok()   { printf "${G}✓${N} %s\n" "$*"; }
warn() { printf "${Y}⚠${N} %s\n" "$*"; }
err()  { printf "${R}✗${N} %s\n" "$*" 1>&2; }

# ---- 1. 前置检查 ----
say "检查 Python 版本..."
if ! command -v python3 >/dev/null; then
    err "找不到 python3。请先安装 Python ≥ 3.9 再重试"
    err "  macOS:  brew install python@3.11  或  xcode-select --install"
    err "  Linux:  用包管理器装 python3 + python3-venv"
    exit 1
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_OK=$(python3 -c "import sys; print(1 if sys.version_info >= (3, 9) else 0)")
if [ "$PY_OK" != "1" ]; then
    err "Python $PY_VER 太旧,需要 ≥ 3.9"
    exit 1
fi
ok "Python $PY_VER"

say "检查 git..."
command -v git >/dev/null || { err "找不到 git,请先装 git"; exit 1; }
ok "git OK"

# ---- 2. 克隆 / 拉取代码 ----
if [ -d "$INSTALL_DIR/.git" ]; then
    say "已存在 $INSTALL_DIR,拉取最新..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    say "克隆到 $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
ok "代码就绪"

# ---- 3. venv + 依赖 ----
cd "$INSTALL_DIR"
if [ ! -d .venv_lens ]; then
    say "创建虚拟环境 .venv_lens/..."
    python3 -m venv .venv_lens
fi
# shellcheck disable=SC1091
source .venv_lens/bin/activate
ok "venv 就绪 ($(python --version 2>&1))"

say "升级 pip..."
pip install --upgrade pip -q

say "安装 life_lens 主包..."
pip install -e . -q

# pyproject.toml 没声明这些(避免新手装就拉 GB 级 wheel),手动装
say "安装机器学习依赖(numpy / insightface / onnxruntime / fastembed,~700MB,慢一会)..."
pip install numpy insightface onnxruntime opencv-python fastembed -q

# fastembed 会把 pillow 降到 10.4,但 pillow-heif 要 ≥11.1,显式升回
say "修复 pillow 版本 + 装 socks 支持..."
pip install 'pillow>=11.1' 'httpx[socks]' -q
ok "依赖安装完成"

# ---- 4. 启动(端口被占就先 kill,确保跑的是刚刚装好的新代码)----
say "启动 lens (端口 $PORT)..."
OLD_PID=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null | head -1 || true)
if [ -n "$OLD_PID" ]; then
    warn "端口 $PORT 已被 pid=$OLD_PID 占用(可能是上一版的 lens server),先 kill 再启动"
    kill "$OLD_PID" 2>/dev/null || true
    for _ in $(seq 1 10); do
        if ! lsof -nP -iTCP:$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then break; fi
        sleep 0.5
    done
    if lsof -nP -iTCP:$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
        warn "5 秒没退,SIGKILL"
        kill -9 "$OLD_PID" 2>/dev/null || true
        sleep 0.5
    fi
fi
nohup lens serve --port "$PORT" > /tmp/life-lens-install.log 2>&1 &
LAUNCHED=1
# 首次启动较慢(InsightFace bootstrap),最多等 30 秒
for _ in $(seq 1 30); do
    if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/" 2>/dev/null | grep -q 200; then
        break
    fi
    sleep 1
done

if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/" 2>/dev/null | grep -q 200; then
    ok "服务运行中 → http://127.0.0.1:$PORT"
    # 自动开浏览器(macOS / Linux)
    if [ "$LAUNCHED" = "1" ]; then
        if command -v open >/dev/null; then
            open "http://127.0.0.1:$PORT" 2>/dev/null || true
        elif command -v xdg-open >/dev/null; then
            xdg-open "http://127.0.0.1:$PORT" 2>/dev/null || true
        fi
    fi
else
    warn "服务没起来,看日志: tail /tmp/life-lens-install.log"
fi

echo ""
ok "安装完成。在浏览器打开后,配置 tab 会引导你:"
echo "    - 装 Ollama 和拉视觉模型"
echo "    - 填高德 API key"
echo "    - 填对话模型 API key (推荐 DeepSeek)"
echo ""
say "下次手动启动:"
echo "    cd $INSTALL_DIR && source .venv_lens/bin/activate && lens serve"
echo ""
say "以后升级到新版,二选一:"
echo "    A) 在已装好的 venv 里:lens update         # 一行搞定,拉代码 + 装依赖 + kill 旧 server + 起新的"
echo "    B) 重跑本脚本:curl -fsSL https://raw.githubusercontent.com/uxtracer/life-lens/main/install.sh | bash"
