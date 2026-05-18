#!/usr/bin/env bash
# scripts/publish.sh — 把私有 monorepo 子目录的当前 HEAD 同步到公开镜像目录,
# 并跑回归测试 + 隐私 grep,**不**自动 push。最后请人工 cd 到镜像目录 git add + push。
#
# 流程三段:
#   A. rsync 抽取(--delete,镜像永远反映 HEAD;exclude 私有数据)
#   B. 回归测试(pytest A 层 + 隐私 grep)
#   C. 提示人工 push
#
# 用法:
#   bash scripts/publish.sh
#   bash scripts/publish.sh --skip-tests   # 急用,跳测试(不推荐)
#
# 公开仓库: https://github.com/uxtracer/life-lens
set -euo pipefail

SRC="$HOME/claude/life_lens"
DST="$HOME/claude/life_lens-public"

SKIP_TESTS=0
for arg in "$@"; do
    case "$arg" in
        --skip-tests) SKIP_TESTS=1 ;;
        --help|-h)
            head -20 "$0" | tail -19; exit 0 ;;
    esac
done

# === 阶段 A:抽取 ===
echo "════════════════════════════════════════"
echo "[A] rsync $SRC/ → $DST/"
echo "════════════════════════════════════════"
mkdir -p "$DST"
rsync -a --delete \
    --exclude '.git/' \
    --exclude '.venv_lens/' \
    --exclude '.cache/' \
    --exclude 'sample/' \
    --exclude 'seeds/' \
    --exclude 'seedface/' \
    --exclude 'export/' \
    --exclude 'backups/' \
    --exclude 'chat_log/' \
    --exclude 'tests/eval/ground_truth.yaml' \
    --exclude 'tests/eval/reports/' \
    --exclude 'tests/eval/chat/' \
    --exclude '.claude/' \
    --exclude '.DS_Store' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '*.egg-info/' \
    --exclude '.pytest_cache/' \
    "$SRC/" "$DST/"
echo "✓ rsync 完成"

# === 阶段 B:回归测试 ===
if [[ "$SKIP_TESTS" == "1" ]]; then
    echo "⚠ --skip-tests 跳过回归测试"
else
    echo
    echo "════════════════════════════════════════"
    echo "[B] 在镜像目录跑回归(pytest + 隐私 grep)"
    echo "════════════════════════════════════════"
    cd "$DST"

    # B1: 隐私 grep — 任一命中立刻 fail
    echo "[B1] 隐私 grep..."
    # Self-reference exclusions: test_no_private_data.py 是检测器本身(PRIVATE_NAMES 列表);
    # publish.sh 是 grep 命令本身;这两个文件含 pattern 是设计内,跳过避免假阳性。
    PRIVACY_HITS=$(grep -rn "肖昆\|肖凯骏\|肖萍\|刘玉芳\|肖懿芯\|吴琼\|肖吕国\|/Users/xiaokun\|斯伦贝谢" . \
        --include='*.py' --include='*.md' --include='*.yaml' --include='*.json' \
        --include='*.html' --include='*.css' --include='*.js' --include='*.sh' --include='*.toml' \
        --exclude='test_no_private_data.py' --exclude='publish.sh' \
        --exclude-dir='.git' --exclude-dir='.venv_lens' --exclude-dir='__pycache__' \
        2>/dev/null || true)
    if [[ -n "$PRIVACY_HITS" ]]; then
        echo "❌ 隐私 grep 命中,中止发布:"
        echo "$PRIVACY_HITS"
        echo
        echo "→ 请回 $SRC 改源,然后重跑 publish.sh"
        exit 1
    fi
    echo "  ✓ 隐私 grep 0 命中"

    # B2: pytest 单元 / 集成 / migration / contract
    echo "[B2] pytest ..."
    if [[ -d ".venv_lens" ]]; then
        # 公开镜像里没装 venv,从源目录借
        source "$SRC/.venv_lens/bin/activate"
    else
        source "$SRC/.venv_lens/bin/activate"
    fi
    pytest tests/ -x -q
    echo "  ✓ pytest 全过"
fi

# === 阶段 C:diff + 等手动 push ===
echo
echo "════════════════════════════════════════"
echo "[C] 准备完毕,请人工接管"
echo "════════════════════════════════════════"
echo
cd "$DST"
if [[ ! -d ".git" ]]; then
    echo "⚠ 镜像目录还没初始化 git,首次发布请跑:"
    echo
    echo "    cd $DST"
    echo "    git init"
    echo "    git add ."
    echo "    git commit -m 'Initial public release'"
    echo "    git remote add origin git@github.com:uxtracer/life-lens.git"
    echo "    git branch -M main"
    echo "    git push -u origin main"
    echo
else
    echo "→ cd $DST"
    echo "  git status        # 看变化"
    echo "  git diff          # 详细 diff"
    echo "  git add ."
    echo "  git commit -m '...'"
    echo "  git push          # 你自己决定何时推"
    echo
    git -C "$DST" status -s 2>/dev/null | head -30 || true
fi
echo
echo "✓ publish.sh 完成"
