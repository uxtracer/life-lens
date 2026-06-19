"""配置页必须明确说明Apple Photos图库的完全磁盘访问授权。"""
from __future__ import annotations

from pathlib import Path


def test_photos_permission_steps_are_visible_in_settings():
    index_path = Path(__file__).parents[1] / "life_lens" / "web" / "static" / "index.html"
    html = index_path.read_text(encoding="utf-8")

    assert "使用Apple Photos前,请先授予完全磁盘访问权限" in html
    assert "系统设置」→「隐私与安全性」→「完全磁盘访问权限" in html
    assert "Terminal或iTerm" in html
    assert "如果从Codex启动,请添加Codex" in html
    assert "不需要添加Ollama" in html
    assert "Ollama本身不打开Photos图库" in html
    assert "命名或归并会立即更新结构化人物查询" in html
    assert "需要重跑vision才会改写其中的称呼" in html
    assert "完全退出并重新打开该程序" in html
    assert "只刷新网页不会让新权限生效" in html
    assert "support.apple.com/zh-cn/guide/mac-help/mchl211c911f/mac" in html
