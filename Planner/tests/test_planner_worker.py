"""Planner 模块业务回归（任务分组 / 倒计时 / 素材拼装 / 防重 / dry-run 零副作用）。

运行方式（需指定宿主项目以提供 common 公共库）：
    CABCLAW_HOST=/home/xinyi/cabclaw-dist \
        python -m pytest cabclaw-modules/Planner/tests/
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
    pytest.skip("缺少宿主 common 库：请设置 CABCLAW_HOST=<宿主项目根>", allow_module_level=True)

TODAY = date.today()
TOMORROW = TODAY + timedelta(days=1)
YDAY = TODAY - timedelta(days=1)

REAL_SETTINGS = pw._settings          # 真读 settings.json 的原函数（模块属性桩会互相覆盖，需要真读时用）


def _mk(tmp: Path):
    """构造隔离环境（模块目录/数据区 + mock 副作用），返回上下文。

    复位全部模块属性桩，防前序测试的覆盖泄漏。
    """
    pw.MODULE_DIR = tmp / "Planner"
    pw.MODULE_DIR.mkdir()
    pw.DATA_DIR = tmp / "data"
    pw.DATA_DIR.mkdir()
    pw.MORNING_SENT = pw.DATA_DIR / "morning_sent.json"
    pw.EVENING_SENT = pw.DATA_DIR / "evening_sent.json"
    pw.PUSH_EVENTS = pw.DATA_DIR / "push_events.json"
    pw.COUNTDOWN_FILE = pw.DATA_DIR / "countdown.json"
    pw.BRIEFING_DIR = pw.DATA_DIR / "briefing"
    ctx = {"pushes": [], "events": {}}
    def post_push(body, token):
        ctx["pushes"].append((body, token))
        ctx["events"][body["event_id"]] = "queued"
        return True
    pw.post_push = post_push
    pw.probe_push_event = lambda event_id, token: (
        {"status": "found", "state": ctx["events"][event_id]}
        if event_id in ctx["events"] else {"status": "missing"})
    pw.load_token = lambda d: "mock"
    pw.shared_load = lambda name: {"tasks": []}
    pw._settings = lambda: (dict(pw.DEFAULT_SETTINGS), False)
    pw.get_weather = lambda: "集宁 ☀️ 晴 22°C"
    pw.get_weather_snapshot = lambda **k: {"ok": False}
    pw.weather_alerts = lambda: []
    pw.get_lunar = lambda d: {"jieqi": None, "month": "七月", "day": "廿八"}
    pw.get_fufu = lambda d: []
    pw.get_jiujiu = lambda d: []
    pw.is_holiday = lambda d: None
    pw.get_location = lambda: {"province": "内蒙古", "city": "集宁"}
    pw.localdata_available = lambda loc: []
    pw.localdata_fetch = lambda loc, s: {}
    pw._job_diagnosis = lambda: (False, "未登记（测试桩）")
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


def test_collect_tasks_historical_done_not_overdue():
    """历史已完成任务不进 overdue（issue #2）：done_at 为过去日期 / 仅 done 真。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    hist = (TODAY - timedelta(days=30)).isoformat()
    tasks = [
        _task("h", YDAY.isoformat(), done=True, done_at=f"{hist}T09:00:00"),  # 历史完成（done_at 过去）
        _task("x", YDAY.isoformat(), done=True, done_at=None),                # 旧数据仅 done 真 → 排除且不算今日完成
        _task("y", YDAY.isoformat()),                                         # 真逾期
    ]
    _set_tasks(ctx, tasks)
    out = pw.collect_tasks(TODAY)
    assert [t["id"] for t in out["overdue"]] == ["y"]
    assert not {t["id"] for t in out["done_today"]} & {"h", "x"}


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
    """早报素材（信息条目稿）：问候历法/天气预警/倒计时/逾期/待办/收尾提示，无组织指令。"""
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
    assert "早上好" in out and "今天是" in out          # 问候 + 历法行
    assert "今天天气" in out
    assert "暴雨预警" in out
    assert "纪念日还有 2 天，该准备了" in out
    assert "已逾期 1 条" in out
    assert "任务a" in out
    assert "收尾提示" in out                            # 有逾期 → 逾期方向
    assert "组织成一条口语化消息" not in out            # 素材零指令
    assert "【晨间早报素材】" not in out                # 无标题行


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
    assert "晚上好" in out
    assert "今天完成 1 件" in out
    assert "任务done1" in out
    assert "还有 1 件今日任务未完成" in out
    assert "任务undone" in out
    assert "收尾提示" in out                            # 有未完成 → 温和提醒方向


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
    assert not pw.MORNING_SENT.exists()  # 接受≠已发送
    ctx["events"][f"planner:{TODAY}:morning"] = "sent"
    assert pw.main(["--reconcile"]) == 0
    assert pw.MORNING_SENT.exists()
    assert pw.main(["--phase", "evening"]) == 0
    assert not pw.EVENING_SENT.exists()
    ctx["events"][f"planner:{TODAY}:evening"] = "sent"
    assert pw.main(["--reconcile"]) == 0
    assert pw.EVENING_SENT.exists()


# ---------- 简报消费 ----------

def test_today_briefing_exact_match():
    """简报按当天日期文件名精确匹配；只有昨天的文件 → 视为未生成（防跨天误用旧产物）。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    assert pw._today_briefing(TODAY) is None            # 无目录
    pw.BRIEFING_DIR.mkdir()
    (pw.BRIEFING_DIR / f"{YDAY.isoformat()}.html").write_text("昨天的")
    assert pw._today_briefing(TODAY) is None            # 只有昨天 → None
    (pw.BRIEFING_DIR / f"{TODAY.isoformat()}.html").write_text("今天的")
    assert pw._today_briefing(TODAY).name == f"{TODAY.isoformat()}.html"


def test_morning_briefing_file_attach():
    """briefing_on + 当天有产物 → 提交事实与依赖文件，sent 前不记送达。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _set_tasks(ctx, [])
    pw.BRIEFING_DIR.mkdir()
    (pw.BRIEFING_DIR / f"{TODAY.isoformat()}.html").write_text("<html>简报</html>")
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    assert pw.morning(TODAY, dry=False) == 0
    types = [b[0]["type"] for b in ctx["pushes"]]
    assert types == ["reminder", "file"]
    reminder, file = [b[0] for b in ctx["pushes"]]
    assert "text" not in reminder and reminder["context"]["business_date"] == TODAY.isoformat()
    assert file["after_event_id"] == reminder["event_id"]
    assert not pw.MORNING_SENT.exists()
    ctx["events"][reminder["event_id"]] = "sent"
    ctx["events"][file["event_id"]] = "sent"
    assert pw.main(["--reconcile"]) == 0
    sent = json.loads(pw.MORNING_SENT.read_text(encoding="utf-8"))
    assert sent.get(f"{TODAY.isoformat()}|briefing")


def test_morning_briefing_file_fail_degraded():
    """附件提交失败不自动换键、不另发素材说明；持续以同一 event_id 对账。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _set_tasks(ctx, [])
    pw.BRIEFING_DIR.mkdir()
    briefing = pw.BRIEFING_DIR / f"{TODAY.isoformat()}.html"
    briefing.write_text("<html>简报</html>")
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    def submit(body, token):
        ctx["pushes"].append((body, token))
        if body["type"] == "reminder":
            ctx["events"][body["event_id"]] = "queued"
            return True
        return False
    pw.post_push = submit
    assert pw.morning(TODAY, dry=False) == 1       # file 失败 → rc=1
    assert [b[0]["type"] for b in ctx["pushes"]] == ["reminder", "file"]
    file_id = ctx["pushes"][1][0]["event_id"]
    ctx["events"][file_id] = "unknown"
    assert pw.main(["--reconcile"]) == 0
    assert json.loads(pw.PUSH_EVENTS.read_text(encoding="utf-8"))[f"{TODAY}:morning"]["file_state"] == "unknown"
    assert [b[0]["type"] for b in ctx["pushes"]] == ["reminder", "file"]
    assert not pw.MORNING_SENT.exists()


# ---------- dry 不被防重短路（防重只对真跑生效） ----------

def test_dry_morning_not_blocked_by_sent():
    """已发过日期 dry-run 仍有素材输出且零推送（防重短路加 not dry 前提）。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    assert pw.morning(TODAY, dry=False) == 0
    assert pw.PUSH_EVENTS.exists()
    ctx["pushes"] = []
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert pw.morning(TODAY, dry=True) == 0
    assert "早上好" in buf.getvalue()
    assert ctx["pushes"] == []


def test_dry_evening_not_blocked_by_sent():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    assert pw.evening(TODAY, dry=False) == 0
    assert pw.PUSH_EVENTS.exists()
    ctx["pushes"] = []
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert pw.evening(TODAY, dry=True) == 0
    assert "晚上好" in buf.getvalue()
    assert ctx["pushes"] == []


# ---------- settings 归属语义（不存在=首次正常 / 存在但坏=提示） ----------

def test_settings_missing_first_install_ok():
    """settings.json 不存在（首次安装）→ 默认设置、corrupt=False、素材无异常提示。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = REAL_SETTINGS         # 恢复真读（_mk 默认是桩）
    s, corrupt = pw._settings()
    assert corrupt is False and s["briefing_on"] is False
    _set_tasks(ctx, [])
    assert pw.morning(TODAY, dry=False) == 0
    texts = [b[0].get("text", "") for b in ctx["pushes"]]
    assert not any("配置读取异常" in t for t in texts)


def test_settings_corrupt_noted_in_material():
    """settings.json 存在但坏 → corrupt=True，素材带事实性提示。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = REAL_SETTINGS         # 恢复真读
    (pw.DATA_DIR / "settings.json").write_text("{broken", encoding="utf-8")
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert pw.morning(TODAY, dry=True) == 0
    assert "配置读取异常" in buf.getvalue()


# ---------- 简报未就绪兜底三态 ----------

def _briefing_env(ctx):
    """briefing_on + 无当天产物的公共环境。"""
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    pw.BRIEFING_DIR.mkdir(exist_ok=True)
    pw._job_diagnosis = lambda: (True, "平台定时器已登记")


def test_briefing_wait_registered_rc1():
    """简报 job 已登记但无产物 → rc=1 等待 + attempt 计数 + 等待期零推送。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _briefing_env(ctx)
    assert pw.morning(TODAY, dry=False) == 1
    assert ctx["pushes"] == []                        # 等待期用户零感知
    sent = json.loads(pw.MORNING_SENT.read_text(encoding="utf-8"))
    assert sent.get(f"{TODAY.isoformat()}|attempt") == 1
    assert sent.get(TODAY.isoformat()) is None        # 未记防重


def test_briefing_wait_fallback_after_max():
    """attempt 达上限 → 提交无简报早报，sent 以后才清等待计数。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _briefing_env(ctx)
    pw.MORNING_SENT.write_text(json.dumps({f"{TODAY.isoformat()}|attempt": 3}), encoding="utf-8")
    assert pw.morning(TODAY, dry=False) == 0
    types = [b[0]["type"] for b in ctx["pushes"]]
    assert types == ["reminder"]                      # 只发素材，无 file
    assert "未生成" in ctx["pushes"][0][0]["facts"]["briefing_note"]
    sent = json.loads(pw.MORNING_SENT.read_text(encoding="utf-8"))
    assert not sent.get(TODAY.isoformat())
    assert any(k.endswith("|attempt") for k in sent)


def test_briefing_unregistered_push_with_note():
    """简报 job 未登记 → 照发 rc=0 + 素材标注原因。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    pw.BRIEFING_DIR.mkdir(exist_ok=True)
    pw._job_diagnosis = lambda: (False, "模块数据区无 jobs 产物（无 job 声明或从未登记）")
    assert pw.morning(TODAY, dry=False) == 0
    text = ctx["pushes"][0][0]["facts"]["briefing_note"]
    assert "未生成" in text and "无 jobs 产物" in text
    assert not pw.MORNING_SENT.exists()


def test_briefing_diag_unavailable_push():
    """诊断异常（bridge 环境问题）→ 照发 + 标注诊断不可用，不阻塞早报。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    pw.BRIEFING_DIR.mkdir(exist_ok=True)
    pw._job_diagnosis = lambda: None
    assert pw.morning(TODAY, dry=False) == 0
    assert "诊断不可用" in ctx["pushes"][0][0]["facts"]["briefing_note"]


# ---------- 晚报简报兜底 ----------

def test_evening_briefing_fallback_when_morning_missed():
    """早报没带成（无 <date>|briefing 标记）+ 当天有产物 → 晚报补简报段（指示式）+ file 附发。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    pw.BRIEFING_DIR.mkdir(exist_ok=True)
    (pw.BRIEFING_DIR / f"{TODAY.isoformat()}.html").write_text("<html>简报</html>")
    assert pw.evening(TODAY, dry=False) == 0
    types = [b[0]["type"] for b in ctx["pushes"]]
    assert types == ["reminder", "file"]
    reminder, file = [body for body, _token in ctx["pushes"]]
    assert reminder["context"]["html_path"].endswith(f"{TODAY}.html")
    assert file["after_event_id"] == reminder["event_id"]


def test_evening_briefing_skip_if_morning_had_it():
    """早报已带简报（MORNING_SENT 有标记）→ 晚报不重复带。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    pw.BRIEFING_DIR.mkdir(exist_ok=True)
    (pw.BRIEFING_DIR / f"{TODAY.isoformat()}.html").write_text("<html>简报</html>")
    pw.MORNING_SENT.write_text(json.dumps({f"{TODAY.isoformat()}|briefing": 1.0}), encoding="utf-8")
    assert pw.evening(TODAY, dry=False) == 0
    assert all(b[0]["type"] == "reminder" for b in ctx["pushes"])
    assert "context" not in ctx["pushes"][0][0]


def test_same_business_key_probes_frozen_payload_after_lost_receipt():
    """早报 POST 已接受但 200 回执丢失；下轮查询原键，不重新生成当天变化的天气。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _set_tasks(ctx, [_task("a", TODAY.isoformat())])
    push = pw.post_push

    def lost_receipt(payload, token):
        push(payload, token)
        return False

    pw.post_push = lost_receipt
    assert pw.morning(TODAY, dry=False) == 1
    event = ctx["pushes"][0][0]
    assert event["event_id"] == f"planner:{TODAY}:morning"
    pw.get_weather = lambda: "天气已变化"
    assert pw.morning(TODAY, dry=False) == 0
    assert len(ctx["pushes"]) == 1
    ledger = json.loads(pw.PUSH_EVENTS.read_text(encoding="utf-8"))
    assert ledger[f"{TODAY}:morning"]["reminder"] == event


def test_file_uncertain_never_requeued_from_evening():
    """晨报已经接受过附件，文件 unknown 后晚报不另发同一文件。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    pw.BRIEFING_DIR.mkdir(exist_ok=True)
    (pw.BRIEFING_DIR / f"{TODAY}.html").write_text("<html>合成</html>")
    assert pw.morning(TODAY, dry=False) == 0
    morning_text, morning_file = [body for body, _token in ctx["pushes"]]
    ctx["events"][morning_text["event_id"]] = "sent"
    ctx["events"][morning_file["event_id"]] = "unknown"
    assert pw.main(["--reconcile"]) == 0
    ctx["pushes"].clear()
    assert pw.evening(TODAY, dry=False) == 0
    assert [body["type"] for body, _token in ctx["pushes"]] == ["reminder"]


def test_reconcile_before_report_event_does_not_create_report():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    assert pw.main(["--reconcile"]) == 0
    assert ctx["pushes"] == [] and not pw.PUSH_EVENTS.exists()


def test_reconcile_dry_run_does_not_query_or_write():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw.probe_push_event = lambda *_args: pytest.fail("dry-run 不得查询推送状态")
    assert pw.main(["--reconcile", "--dry-run"]) == 0
    assert ctx["pushes"] == [] and not pw.PUSH_EVENTS.exists()


def test_report_switches_block_new_dispatch_but_not_reconciliation():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "planner_on": False}, False)
    assert pw.morning(TODAY, dry=False) == 0
    assert pw.evening(TODAY, dry=False) == 0
    assert ctx["pushes"] == [] and not pw.PUSH_EVENTS.exists()

    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "evening_on": False}, False)
    assert pw.evening(TODAY, dry=False) == 0
    assert ctx["pushes"] == []
    assert pw.morning(TODAY, dry=False) == 0
    event_id = f"planner:{TODAY}:morning"
    ctx["events"][event_id] = "sent"
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "planner_on": False}, False)
    assert pw.main(["--reconcile"]) == 0
    assert json.loads(pw.MORNING_SENT.read_text(encoding="utf-8"))[TODAY.isoformat()]


def test_disabled_report_does_not_first_submit_reserved_event():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS}, False)
    original = pw.post_push
    pw.post_push = lambda *_args: False  # 本地冻结记录，但宿主明确还没有接受
    assert pw.morning(TODAY, dry=False) == 1
    pw.post_push = original
    ctx["pushes"].clear()
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "planner_on": False}, False)
    assert pw.main(["--reconcile"]) == 0
    record = json.loads(pw.PUSH_EVENTS.read_text(encoding="utf-8"))[f"{TODAY}:morning"]
    assert record["text_state"] == "expired" and ctx["pushes"] == []


def test_previous_day_absent_report_blocks_unsent_attachment():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw.BRIEFING_DIR.mkdir(exist_ok=True)
    brief = pw.BRIEFING_DIR / f"{TODAY}.html"
    brief.write_text("<html>合成</html>", encoding="utf-8")
    payload, file_payload = pw._report_payload(
        "morning", TODAY, {"today": [], "overdue": [], "done_today": []},
        [], dict(pw.DEFAULT_SETTINGS), False, brief)
    record = {"business_date": TODAY.isoformat(), "phase": "morning", "reminder": payload,
              "file": file_payload, "text_state": "pending", "file_state": "pending"}
    ledger = {f"{TODAY}:morning": record}
    assert pw._sync_event(ledger, record, TOMORROW, "mock") is True
    assert record["text_state"] == "expired" and record["file_state"] == "blocked"
    assert ctx["pushes"] == []


def test_sent_text_missing_from_host_does_not_resend_file():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    brief = pw.DATA_DIR / "briefing" / f"{TODAY}.html"
    brief.parent.mkdir()
    brief.write_text("<html>合成</html>", encoding="utf-8")
    payload, attached = pw._report_payload(
        "morning", TODAY, {"today": [], "overdue": [], "done_today": []},
        [], dict(pw.DEFAULT_SETTINGS), False, brief)
    record = {"business_date": TODAY.isoformat(), "phase": "morning", "reminder": payload,
              "file": attached, "text_state": "sent", "file_state": "pending"}
    ledger = {f"{TODAY}:morning": record}
    assert pw._sync_event(ledger, record, TODAY, "mock") is True
    assert record["text_state"] == "sent" and record["file_state"] == "blocked"
    assert ctx["pushes"] == []


def test_reconcile_rotates_large_pending_report_backlog():
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    ledger = {}
    for idx in range(5):
        slot = f"{TODAY}:slot{idx}"
        ledger[slot] = {"business_date": TODAY.isoformat(), "phase": "morning",
                        "reminder": {"event_id": f"planner:{TODAY}:slot{idx}",
                                     "facts": {"phase": "morning"}, "type": "reminder"},
                        "file": None, "text_state": "pending", "file_state": None}
    assert pw.save_sent_json(pw.PUSH_EVENTS, ledger)
    checked = []

    def unavailable(event_id, token):
        checked.append(event_id)
        return {"status": "unavailable"}

    pw.probe_push_event = unavailable
    for n in range(3):
        assert pw._reconcile_events(TODAY) is False
        assert len(checked) == (n + 1) * 2
    assert set(checked) == {rec["reminder"]["event_id"] for rec in ledger.values()}
    assert isinstance(pw._load_push_events()["__cursor__"], int)
    assert ctx["pushes"] == []


# ---------- 收尾提示（方向选择） ----------

def test_closing_hint_morning_priority():
    """早报收尾提示：逾期 > 倒计时当天 > 温差；无事缺席不硬凑。"""
    assert "逾期" in pw._closing_hint_morning({"overdue": [1]}, [], None)
    assert "打气" in pw._closing_hint_morning({"overdue": []}, [{"days": 0, "name": "x"}], None)
    assert "温差" in pw._closing_hint_morning({"overdue": []}, [{"days": 3, "name": "x"}], "温差句")
    assert pw._closing_hint_morning({"overdue": []}, [{"days": 3, "name": "x"}], None) is None


def test_closing_hint_evening_directions():
    """晚报收尾提示：未完成温和提醒 / 全完成肯定 / 无任务关照休息。"""
    assert "未完成" in pw._closing_hint_evening({"today": [1], "done_today": []})
    assert "完成情况" in pw._closing_hint_evening({"today": [], "done_today": [1]})
    assert "休息" in pw._closing_hint_evening({"today": [], "done_today": []})


# ---------- 温差提醒（阈值边界） ----------

def test_temp_swing_threshold():
    """当前与未来数小时温差 <8°C 不提醒；≥8°C 提醒；快照失败返回 None。"""
    orig = pw.get_weather_snapshot
    pw.get_weather_snapshot = lambda **k: {"ok": True, "current": {"temperature": 20}, "hourly": [{"temperature": 26}]}
    assert pw._temp_swing_note() is None              # 差 6 → 无
    pw.get_weather_snapshot = lambda **k: {"ok": True, "current": {"temperature": 15}, "hourly": [{"temperature": 23}]}
    note = pw._temp_swing_note()                      # 差 8 → 提醒
    assert note and "15~23" in note and "增减衣物" in note
    pw.get_weather_snapshot = lambda **k: {"ok": False}
    assert pw._temp_swing_note() is None              # 快照失败 → 无
    pw.get_weather_snapshot = orig


# ---------- 称呼（identity.json address） ----------

def test_greeting_address():
    """有 address → 带称呼；无 → 不空挂；晚报对称。"""
    orig = pw._address
    pw._address = lambda: "鑫"
    assert pw._greeting_head(TODAY).startswith("鑫，早上好呀！")
    pw._address = lambda: ""
    head = pw._greeting_head(TODAY)
    assert head.startswith("早上好呀！")
    assert "，早上好" not in head.split("！")[0]
    pw._address = lambda: "老板"
    assert pw._evening_greeting() == "老板，晚上好呀！"
    pw._address = orig


def test_lunar_month_keeps_month_character():
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    pw.get_lunar = lambda _day: {"jieqi": None, "month": "八", "day": "十五"}
    assert "农历八月十五" in pw._greeting_head(TODAY)
    assert pw._calendar_facts(TODAY)["lunar_month"] == "八月"


# ---------- 素材单元 ----------

def test_fmt_overdue_due_inline():
    """逾期行：到期日并入首段（无多余空格），时间/标签空格分隔，坏 due 兜底。"""
    t = {"text": "修路由器", "due": "2026-08-28", "time": None, "tags": []}
    assert pw.fmt_overdue([t]) == "- 修路由器（8 月 28 日到期）"
    t2 = {"text": "x", "due": "bad-date", "time": "09:00", "tags": ["工作"]}
    assert pw.fmt_overdue([t2]) == "- x（bad-date 到期） ⏰ 09:00 #工作"
