"""Planner 模块业务回归（任务分组 / 倒计时 / 素材拼装 / 防重 / dry-run 零副作用）。

运行方式（需指定宿主项目以提供 common 公共库）：
    WECHAT_CLAW_HOST=/home/xinyi/wechat-claw-dist \
        python -m pytest wechat-claw_modules_official/Planner/tests/
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

MODULE_SRC = Path(__file__).resolve().parent.parent  # Planner/
sys.path.insert(0, str(MODULE_SRC))

import planner_worker as pw  # noqa: E402

try:
    import common  # noqa: F401  # 验证宿主注入是否成功
except ImportError:
    pytest.skip("缺少宿主 common 库：请设置 WECHAT_CLAW_HOST=<宿主项目根>", allow_module_level=True)

TODAY = date.today()
TOMORROW = TODAY + timedelta(days=1)
YDAY = TODAY - timedelta(days=1)


def _mk(tmp: Path):
    """构造隔离环境（模块目录/数据区 + mock 副作用），返回上下文。"""
    pw.MODULE_DIR = tmp / "Planner"
    pw.MODULE_DIR.mkdir()
    pw.DATA_DIR = tmp / "data"
    pw.DATA_DIR.mkdir()
    pw.MORNING_SENT = pw.DATA_DIR / "morning_sent.json"
    pw.EVENING_SENT = pw.DATA_DIR / "evening_sent.json"
    pw.COUNTDOWN_FILE = pw.DATA_DIR / "countdown.json"
    pw.BRIEFING_DIR = pw.DATA_DIR / "briefing"
    ctx = {"pushes": []}
    pw.post_push = lambda body, token: (ctx["pushes"].append((body, token)), True)[1]
    pw.load_token = lambda d: "mock"
    pw.shared_load = lambda name: {"tasks": []}
    pw.get_weather = lambda: "集宁 ☀️ 晴 22°C"
    pw.weather_alerts = lambda: []
    pw.get_lunar = lambda d: {"jieqi": None, "month": "七月", "day": "廿八"}
    pw.get_fufu = lambda d: []
    pw.is_holiday = lambda d: None
    pw.get_location = lambda: {"province": "内蒙古", "city": "集宁"}
    pw.localdata_available = lambda loc: []
    pw.localdata_fetch = lambda loc, s: {}
    return ctx


def _task(rid: str, due: str, **kw) -> dict:
    base = {"id": rid, "text": f"任务{rid}", "due": due, "time": "10:00", "remind_min": None,
            "done": False, "done_at": None, "repeat": None, "done_dates": [], "tags": []}
    base.update(kw)
    return base


def _set_tasks(ctx, tasks):
    pw.shared_load = lambda name: {"tasks": tasks}


# ---------- 任务分组 ----------

def test_collect_tasks_groups():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    tasks = [
        _task("a", TODAY.isoformat()),                          # 今日待办
        _task("b", YDAY.isoformat()),                           # 逾期
        _task("c", TODAY.isoformat(), done=True, done_at=f"{TODAY}T09:00:00"),  # 今日完成
        _task("d", TODAY.isoformat(), done=True, done_at=None),  # 旧数据 done 无时间 → 不算今日完成
        _task("e", TOMORROW.isoformat()),                       # 明天 → 不出现
        _task("r", YDAY.isoformat(), repeat={"freq": "daily"}, done_dates=[TODAY.isoformat()]),  # 重复今天完成
    ]
    _set_tasks(ctx, tasks)
    out = pw.collect_tasks(TODAY)
    assert [t["id"] for t in out["today"]] == ["a"]
    assert [t["id"] for t in out["overdue"]] == ["b"]
    assert {t["id"] for t in out["done_today"]} == {"c", "r"}


def test_collect_tasks_no_data():
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    assert pw.collect_tasks(TODAY) == {"today": [], "overdue": [], "done_today": []}


def test_collect_tasks_sort_order():
    """今日待办：有 time 按 time，无 time 排最后。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    tasks = [
        _task("late", TODAY.isoformat(), time=None),
        _task("morn", TODAY.isoformat(), time="08:00"),
        _task("noon", TODAY.isoformat(), time="12:00"),
    ]
    _set_tasks(ctx, tasks)
    out = pw.collect_tasks(TODAY)
    assert [t["id"] for t in out["today"]] == ["morn", "noon", "late"]


# ---------- 倒计时/纪念日 ----------

def test_next_repeat_date():
    assert pw._next_repeat_date(date(1990, 8, 21), date(2026, 8, 20)) == date(2026, 8, 21)  # 明天
    assert pw._next_repeat_date(date(1990, 8, 20), date(2026, 8, 20)) == date(2026, 8, 20)  # 今天
    assert pw._next_repeat_date(date(1990, 8, 19), date(2026, 8, 20)) == date(2027, 8, 19)  # 已过 → 下一年


def test_collect_countdown_repeat_window():
    """repeat：距下一次 ≤15 天才报；窗口外不报。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    (pw.DATA_DIR / "countdown.json").write_text(json.dumps({
        "entries": [
            {"name": "生日", "date": (TODAY + timedelta(days=10)).isoformat(), "repeat": True},   # ≤15 → 报
            {"name": "周年", "date": (TODAY + timedelta(days=30)).isoformat(), "repeat": True},   # >15 → 不报
        ],
    }), encoding="utf-8")
    out = pw.collect_countdown(TODAY, prune=False)
    assert [(c["name"], c["days"]) for c in out] == [("生日", 10)]


def test_collect_countdown_one_shot_and_expiry():
    """一次性：当天/未来报；过期停报；超时 15 天 prune 删除。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    (pw.DATA_DIR / "countdown.json").write_text(json.dumps({
        "entries": [
            {"name": "考试", "date": TODAY.isoformat(), "repeat": False},                              # 今天 → 报 0
            {"name": "出行", "date": (TODAY + timedelta(days=3)).isoformat(), "repeat": False},        # 未来 → 报
            {"name": "过期", "date": (TODAY - timedelta(days=5)).isoformat(), "repeat": False},        # 过期 → 不报
            {"name": "超时", "date": (TODAY - timedelta(days=20)).isoformat(), "repeat": False},       # 超时 → 删除
        ],
    }), encoding="utf-8")
    out = pw.collect_countdown(TODAY, prune=True)
    assert [(c["name"], c["days"]) for c in out] == [("考试", 0), ("出行", 3)]
    kept = json.loads(pw.COUNTDOWN_FILE.read_text(encoding="utf-8"))["entries"]
    assert {e["name"] for e in kept} == {"考试", "出行", "过期"}   # 超时已删


def test_collect_countdown_prune_false_keeps():
    """prune=False（dry-run）不删超时条目。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    (pw.DATA_DIR / "countdown.json").write_text(json.dumps({
        "entries": [{"name": "超时", "date": (TODAY - timedelta(days=20)).isoformat(), "repeat": False}],
    }), encoding="utf-8")
    pw.collect_countdown(TODAY, prune=False)
    kept = json.loads(pw.COUNTDOWN_FILE.read_text(encoding="utf-8"))["entries"]
    assert len(kept) == 1


# ---------- 素材拼装（段序） ----------

def test_morning_material_sections():
    """早报素材包含各段：天气/倒计时/逾期/待办/组织指令。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _set_tasks(ctx, [
        _task("a", TODAY.isoformat()),
        _task("b", YDAY.isoformat()),
    ])
    (pw.DATA_DIR / "countdown.json").write_text(json.dumps({
        "entries": [{"name": "纪念日", "date": (TODAY + timedelta(days=2)).isoformat(), "repeat": True}],
    }), encoding="utf-8")
    pw.weather_alerts = lambda: ["暴雨预警"]
    assert pw.morning(TODAY, dry=True) == 0
    # dry 打印素材（通过捕获 print 验证各段存在）
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        pw.morning(TODAY, dry=True)
    out = buf.getvalue()
    assert "今天天气" in out
    assert "暴雨预警" in out
    assert "纪念日还有 2 天" in out
    assert "已逾期 1 条" in out
    assert "任务a" in out
    assert "组织成一条口语化消息" in out


def test_evening_material_done_and_undone():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _set_tasks(ctx, [
        _task("done1", TODAY.isoformat(), done=True, done_at=f"{TODAY}T09:00:00"),
        _task("undone", TODAY.isoformat()),
    ])
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert pw.evening(TODAY, dry=True) == 0
    out = buf.getvalue()
    assert "今日已完成" in out
    assert "任务done1" in out
    assert "今日未完成" in out
    assert "任务undone" in out


# ---------- 防重 ----------

def test_morning_sent_dedup():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _set_tasks(ctx, [_task("a", TODAY.isoformat())])
    assert pw.morning(TODAY, dry=False) == 0
    assert ctx["pushes"]          # 已推送
    ctx["pushes"] = []
    assert pw.morning(TODAY, dry=False) == 0   # 二次运行：防重跳过
    assert ctx["pushes"] == []


def test_evening_sent_dedup():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    assert pw.evening(TODAY, dry=False) == 0
    assert ctx["pushes"]
    ctx["pushes"] = []
    assert pw.evening(TODAY, dry=False) == 0
    assert ctx["pushes"] == []


# ---------- dry-run 零副作用 ----------

def test_dry_run_zero_side_effect():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _set_tasks(ctx, [_task("a", TODAY.isoformat())])
    (pw.DATA_DIR / "countdown.json").write_text(json.dumps({
        "entries": [{"name": "超时", "date": (TODAY - timedelta(days=20)).isoformat(), "repeat": False}],
    }), encoding="utf-8")
    assert pw.main(["--phase", "morning", "--dry-run"]) == 0
    assert pw.main(["--phase", "evening", "--dry-run"]) == 0
    assert ctx["pushes"] == []                        # 不推送
    assert not pw.MORNING_SENT.exists()               # 不写防重
    assert not pw.EVENING_SENT.exists()
    kept = json.loads(pw.COUNTDOWN_FILE.read_text(encoding="utf-8"))["entries"]
    assert len(kept) == 1                             # 不清理


def test_main_run_writes_sent():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _set_tasks(ctx, [_task("a", TODAY.isoformat())])
    assert pw.main(["--phase", "morning"]) == 0
    assert pw.MORNING_SENT.exists()
    assert pw.main(["--phase", "evening"]) == 0
    assert pw.EVENING_SENT.exists()


# ---------- 简报消费 ----------

def test_latest_briefing():
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    assert pw.latest_briefing() is None
    pw.BRIEFING_DIR.mkdir()
    (pw.BRIEFING_DIR / "2026-08-18.html").write_text("x")
    import time as _t
    _t.sleep(0.01)
    (pw.BRIEFING_DIR / "2026-08-20.html").write_text("y")
    assert pw.latest_briefing().name == "2026-08-20.html"


def test_morning_briefing_file_attach():
    """briefing_on + 有产物 → 素材含简报段 + file 双发 + 防重。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _set_tasks(ctx, [])
    pw.BRIEFING_DIR.mkdir()
    (pw.BRIEFING_DIR / "2026-08-20.html").write_text("<html>简报</html>")
    pw._settings = lambda: {**pw.DEFAULT_SETTINGS, "briefing_on": True}
    assert pw.morning(TODAY, dry=False) == 0
    types = [b[0]["type"] for b in ctx["pushes"]]
    assert types == ["reminder", "file"]           # 素材 + 原件双发
    assert ctx["pushes"][0][0]["text"].find("信息简报") != -1
