"""task.py（Obsidian 私有解析器）回归：parse_task_line / tag_prefix / scan_md_tasks / sort_due_key。

随 task.py 归 todo 模块（模块源），运行方式同 test_todo_worker（需 CABCLAW_HOST 注入宿主 common）。
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date, time
from pathlib import Path

import pytest

MODULE_SRC = Path(__file__).resolve().parent.parent  # todo/
sys.path.insert(0, str(MODULE_SRC))

from task import ParsedTask, parse_task_line, scan_md_tasks  # noqa: E402

try:
    import common  # noqa: F401  # 宿主注入验证
except ImportError:
    pytest.skip("缺少宿主 common 库：请设置 CABCLAW_HOST=<宿主项目根>", allow_module_level=True)


# ---------- parse_task_line ----------

def test_parse_task_line_fields():
    t = parse_task_line("- [ ] 买菜 📅 2026-08-17 ⏰ 09:30 🔔提前15分钟 #todo/生活 🔼")
    assert t is not None
    assert t.due == date(2026, 8, 17)
    assert t.time == time(9, 30)
    assert t.remind_min == 15
    assert t.tags == ["生活"]
    assert t.priority == 3  # 🔼 = 3


def test_parse_task_line_no_time():
    t = parse_task_line("- [ ] 写周报 📅 2026-08-17")
    assert t is not None
    assert t.time is None


def test_parse_task_line_non_task():
    assert parse_task_line("普通文本行") is None
    assert parse_task_line("## 标题") is None


def test_parse_task_line_tag_prefix():
    """tag_prefix 参数：默认 #todo/ 兼容；自定义前缀取最后一段；空串不提取。"""
    assert parse_task_line("- [ ] 还书 ⏰ 14:30 📅 2026-08-19 #todo/工作").tags == ["工作"]
    assert parse_task_line("- [ ] 买菜 📅 2026-08-19 ⏰ 09:00 #家事", tag_prefix="#家").tags == ["事"]
    assert parse_task_line("- [ ] 测试 📅 2026-08-19 #工作", tag_prefix="").tags == []


# ---------- scan_md_tasks ----------

def test_scan_md_tasks_all_md_and_code_skip():
    """scan_md_tasks：扫所有 .md（不限 Todo-* 命名）、跳过代码块与注释。"""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "a.md").write_text(
        "- [ ] 任务A 📅 2026-08-19 ⏰ 09:00\n"
        "普通文本行\n"
        "- [ ] 无日期任务\n"
        "```\n- [ ] 代码块里的假任务 📅 2026-08-19\n"
        "```\n"
    )
    (tmp / "b.md").write_text("- [ ] 任务B 📅 2026-08-20\n")
    tasks = scan_md_tasks(tmp, "*.md")
    assert len(tasks) == 3, [t.raw_line for t in tasks]
    assert tasks[0].due == date(2026, 8, 19)
    assert tasks[1].due is None        # 无日期任务（解析返回，由调用方过滤）
    assert tasks[2].due == date(2026, 8, 20)


def test_scan_md_tasks_empty_dir():
    assert scan_md_tasks(Path(tempfile.mkdtemp()), "*.md") == []
    assert scan_md_tasks(Path("/no/such/dir"), "*.md") == []

