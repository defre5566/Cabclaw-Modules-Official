"""任务解析（vault 扫描 + 待办解析）。

正则规则与 Obsidian Dataview 视图、各模块规范.md 保持一致（单一事实源）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path

TASK_LINE = re.compile(r"^-\s+\[( |x|X)\]\s+(.+)$")
DUE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
TIME = re.compile(r"⏰\s*(\d{1,2}):(\d{2})")
REMIND = re.compile(r"🔔提前(\d+)分钟")
DONE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
RECUR = re.compile(r"🔁")
TAG = re.compile(r"#todo/(\S+)")
PRIORITY = {"⏫": 1, "🔺": 2, "🔼": 3, "🔽": 4, "⏬": 5}


@dataclass
class ParsedTask:
    raw_line: str
    text: str
    due: date | None = None
    time: time | None = None
    remind_min: int | None = None
    done_date: date | None = None
    recurring: bool = False
    tags: list[str] = field(default_factory=list)
    priority: int = 3
    file: Path | None = None

    @property
    def key(self) -> str:
        """防重 key：日期|任务正文（同一任务当天只推一次）。"""
        d = self.due or date.today()
        return f"{d.isoformat()}|{self.text}"


def strip_fields(text: str) -> str:
    """去掉任务行里的 emoji 字段，留纯正文。"""
    for pat in (DUE, TIME, REMIND, DONE, RECUR, TAG, re.compile(r"[⏫🔺🔼🔽⏬]"), re.compile(r"\s+")):
        text = pat.sub(" ", text)
    return text.strip()


def parse_task_line(line: str, file: Path | None = None, tag_prefix: str = "#todo/") -> ParsedTask | None:
    """解析一行待办；非任务行返回 None。

    tag_prefix：标签提取前缀（含 #，如 "#todo/"）；"" = 不提取标签（tags=[]）。
    """
    m = TASK_LINE.match(line)
    if not m:
        return None
    mark, body = m.group(1), m.group(2)
    due_m = DUE.search(body)
    time_m = TIME.search(body)
    remind_m = REMIND.search(body)
    done_m = DONE.search(body)

    def _d(match) -> date | None:
        return date.fromisoformat(match.group(1)) if match else None

    def _t(match) -> time | None:
        return time(int(match.group(1)), int(match.group(2))) if match else None

    tag_re = re.compile(f"{re.escape(tag_prefix)}(\\S+)") if tag_prefix else None
    tags = tag_re.findall(body) if tag_re else []
    text = strip_fields(body)
    if tag_re and tag_prefix != "#todo/":
        # 自定义前缀：额外剥离标签文本，保持正文纯净
        text = tag_re.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()

    return ParsedTask(
        raw_line=line.rstrip("\n"),
        text=text,
        due=_d(due_m),
        time=_t(time_m),
        remind_min=int(remind_m.group(1)) if remind_m else None,
        done_date=_d(done_m),
        recurring=bool(RECUR.search(body)),
        tags=tags,
        priority=next((PRIORITY[k] for k in PRIORITY if k in body), 3),
        file=file,
    )


def _scan_dir(directory: Path, glob_pat: str, tag_prefix: str = "#todo/") -> list[ParsedTask]:
    """按 glob 扫描目录下的任务行（含已完成，供筛选）。跳过代码块/注释。"""
    tasks: list[ParsedTask] = []
    if not directory.is_dir():
        return tasks
    for path in sorted(directory.glob(glob_pat)):
        in_code = False
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            if stripped.startswith("<!--"):
                continue
            task = parse_task_line(line.strip(), path, tag_prefix=tag_prefix)
            if task:
                tasks.append(task)
    return tasks


def scan_md_tasks(directory: Path, glob_pat: str = "*.md", tag_prefix: str = "#todo/") -> list[ParsedTask]:
    """扫描目录下所有 .md 的 Tasks 语法行（todo vault 模式用，不限定文件名）。"""
    return _scan_dir(directory, glob_pat, tag_prefix)