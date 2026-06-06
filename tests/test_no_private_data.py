"""隐私污染兜底测试 —— 确保跟踪的源码 / 文档 / 测试里没有真实人名 / 用户路径。

如果这个测试 FAIL,说明 Phase 2 的"开发纪律"被破了,publish.sh 的 grep 兜底会拦,
但最好在本地 pytest 阶段就发现。每次 commit 前跑一遍。

排除:
  - tests/eval/ground_truth.yaml (私有评测,publish.sh rsync 排除)
  - scripts/publish.sh (把名字当 grep pattern 内容,这是工具本职)
  - .venv_lens / sample / seeds / .cache / .git / export / backups / chat_log / reports
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PRIVATE_NAMES = ["肖昆", "肖凯骏", "肖萍", "刘玉芳", "肖懿芯", "吴琼", "肖吕国"]
PRIVATE_PATHS = ["/Users/xiaokun"]
PRIVATE_COMPANIES = ["斯伦贝谢"]

# 文件类型
INCLUDE = ["*.py", "*.md", "*.yaml", "*.json", "*.html", "*.css", "*.js", "*.sh", "*.toml"]
# .claude:Claude Code 本地配置(settings.local.json 会记含绝对路径的权限条目),
# publish.sh rsync 排除整个目录,到不了公开仓 — 和那边的 exclude 清单保持对齐
EXCLUDE_DIRS = [".git", ".venv_lens", "sample", "seeds", "seedface", ".cache",
                "node_modules", "export", "backups", "chat_log", "reports", ".claude"]
# 跟踪文件白名单(出现真名/路径不算泄漏):
#   - tests/eval/ground_truth.yaml:私有评测,publish.sh rsync 会挡公开版
#   - scripts/publish.sh:把名字当 grep pattern 内容,这是工具本职
ALLOWED_FILES = ["tests/eval/ground_truth.yaml", "scripts/publish.sh"]


def _grep_alternation(needles: list[str]) -> list[str]:
    """grep -rn for `n1\\|n2\\|...`, 排除 ALLOWED_FILES 和本测试文件自身。"""
    pattern = "\\|".join(needles)
    args = ["grep", "-rn", pattern]
    for inc in INCLUDE:
        args.extend(["--include", inc])
    for ex in EXCLUDE_DIRS:
        args.extend(["--exclude-dir", ex])
    args.append(str(REPO))
    r = subprocess.run(args, capture_output=True, text=True)
    hits = []
    for line in r.stdout.splitlines():
        if any(allowed in line for allowed in ALLOWED_FILES):
            continue
        if "test_no_private_data.py" in line:
            continue
        hits.append(line)
    return hits


def test_no_real_names_in_tracked_files():
    """真实人名不应出现在跟踪文件里。"""
    hits = _grep_alternation(PRIVATE_NAMES)
    assert not hits, "真实人名泄漏(请用占位符 张三/小明/王女士/... 替换):\n" + "\n".join(hits)


def test_no_xiaokun_paths():
    """'/Users/xiaokun' 不应出现在跟踪文件里。"""
    hits = _grep_alternation(PRIVATE_PATHS)
    assert not hits, "/Users/xiaokun 硬编码(请用 Path.home() 或 ~/...):\n" + "\n".join(hits)


def test_no_private_company_names():
    """公司/园区名(如斯伦贝谢)不应出现在跟踪文件里。"""
    hits = _grep_alternation(PRIVATE_COMPANIES)
    assert not hits, "公司名泄漏(用占位符'某园区'):\n" + "\n".join(hits)


def test_no_subprocess_claude_in_llm():
    """v0.4 起砍掉 claude-p,life_lens/web/llm.py 不应再 import subprocess 或调 claude CLI。

    config.example.json 不应再含 kind="claude-p" 的 provider。
    """
    llm_py = (REPO / "life_lens" / "web" / "llm.py").read_text(encoding="utf-8")
    # llm.py 不应再 import subprocess 也不应 shutil.which("claude")
    assert "import subprocess" not in llm_py, "llm.py 还在 import subprocess(claude-p 已废)"
    assert 'shutil.which("claude")' not in llm_py, "llm.py 还在调 claude CLI"

    # config.example.json 不应含 claude-p provider
    cfg_example = (REPO / "config.example.json").read_text(encoding="utf-8")
    assert '"claude-p"' not in cfg_example, "config.example.json 还有 claude-p provider"
