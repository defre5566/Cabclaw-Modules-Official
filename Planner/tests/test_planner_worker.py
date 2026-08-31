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
    pw.COUNTDOWN_FILE = pw.DATA_DIR / "countdown.json"
    pw.BRIEFING_DIR = pw.DATA_DIR / "briefing"
    ctx = {"pushes": []}
    pw.post_push = lambda body, token: (ctx["pushes"].append((body, token)), True)[1]
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
    assert pw.MORNING_SENT.exists()
    assert pw.main(["--phase", "evening"]) == 0
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
    """briefing_on + 当天有产物 → 素材含简报段 + file 双发 + 防重。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _set_tasks(ctx, [])
    pw.BRIEFING_DIR.mkdir()
    (pw.BRIEFING_DIR / f"{TODAY.isoformat()}.html").write_text("<html>简报</html>")
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    assert pw.morning(TODAY, dry=False) == 0
    types = [b[0]["type"] for b in ctx["pushes"]]
    assert types == ["reminder", "file"]           # 素材 + 原件双发
    assert ctx["pushes"][0][0]["text"].find("信息简报") != -1
    sent = json.loads(pw.MORNING_SENT.read_text(encoding="utf-8"))
    assert sent.get(f"{TODAY.isoformat()}|briefing")      # 晚报兜底判定标记


def test_morning_briefing_file_fail_degraded():
    """P7：file 双发重试 3 次仍失败 → 补发说明 reminder + 不记已发 + return 1。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _set_tasks(ctx, [])
    pw.BRIEFING_DIR.mkdir()
    briefing = pw.BRIEFING_DIR / f"{TODAY.isoformat()}.html"
    briefing.write_text("<html>简报</html>")
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    # reminder 成功 / file 失败；sleep 打桩防拖慢（重试 3 次每次 3s）
    pw.time.sleep = lambda s: None
    pw.post_push = lambda body, token: (
        ctx["pushes"].append((body, token)), body.get("type") != "file")[1]
    assert pw.morning(TODAY, dry=False) == 1       # file 失败 → rc=1
    types = [b[0]["type"] for b in ctx["pushes"]]
    assert types == ["reminder", "file", "file", "file", "reminder"]  # 素材 + 重试3次 + 补发说明
    assert ctx["pushes"][-1][0]["text"].find("简报原件发送失败") != -1
    assert ctx["pushes"][-1][0]["text"].find(str(briefing)) == -1     # 兜底说明不暴露部署路径
    assert not pw.MORNING_SENT.exists()            # 不记已发（待补发兜底）


# ---------- dry 不被防重短路（防重只对真跑生效） ----------

def test_dry_morning_not_blocked_by_sent():
    """已发过日期 dry-run 仍有素材输出且零推送（防重短路加 not dry 前提）。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    assert pw.morning(TODAY, dry=False) == 0
    assert pw.MORNING_SENT.exists()
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
    assert pw.EVENING_SENT.exists()
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
    """attempt 达上限（默认 retry.max=3）→ 保底发无简报早报 rc=0 + 记防重 + 清 attempt。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _briefing_env(ctx)
    pw.MORNING_SENT.write_text(json.dumps({f"{TODAY.isoformat()}|attempt": 3}), encoding="utf-8")
    assert pw.morning(TODAY, dry=False) == 0
    types = [b[0]["type"] for b in ctx["pushes"]]
    assert types == ["reminder"]                      # 只发素材，无 file
    assert "未生成" in ctx["pushes"][0][0]["text"]    # 保底标注
    sent = json.loads(pw.MORNING_SENT.read_text(encoding="utf-8"))
    assert sent.get(TODAY.isoformat())
    assert not any(k.endswith("|attempt") for k in sent)


def test_briefing_unregistered_push_with_note():
    """简报 job 未登记 → 照发 rc=0 + 素材标注原因。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    pw.BRIEFING_DIR.mkdir(exist_ok=True)
    pw._job_diagnosis = lambda: (False, "模块数据区无 jobs 产物（无 job 声明或从未登记）")
    assert pw.morning(TODAY, dry=False) == 0
    text = ctx["pushes"][0][0]["text"]
    assert "未生成" in text and "无 jobs 产物" in text
    assert json.loads(pw.MORNING_SENT.read_text(encoding="utf-8")).get(TODAY.isoformat())


def test_briefing_diag_unavailable_push():
    """诊断异常（bridge 环境问题）→ 照发 + 标注诊断不可用，不阻塞早报。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    pw.BRIEFING_DIR.mkdir(exist_ok=True)
    pw._job_diagnosis = lambda: None
    assert pw.morning(TODAY, dry=False) == 0
    assert "诊断不可用" in ctx["pushes"][0][0]["text"]


# ---------- 晚报简报兜底 ----------

def test_evening_briefing_fallback_when_morning_missed():
    """早报没带成（无 <date>|briefing 标记）+ 当天有产物 → 晚报补简报段（summary 注入）+ file 附发。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    pw._settings = lambda: ({**pw.DEFAULT_SETTINGS, "briefing_on": True}, False)
    pw.BRIEFING_DIR.mkdir(exist_ok=True)
    (pw.BRIEFING_DIR / f"{TODAY.isoformat()}.html").write_text("<html>简报</html>")
    (pw.BRIEFING_DIR / f"{TODAY.isoformat()}.summary.txt").write_text("1. 要点一\n2. 要点二\n", encoding="utf-8")
    assert pw.evening(TODAY, dry=False) == 0
    types = [b[0]["type"] for b in ctx["pushes"]]
    assert types == ["reminder", "file"]
    text = ctx["pushes"][0][0]["text"]
    assert "信息简报要点" in text and "要点一" in text and "早报时段未送达" in text


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
    assert ctx["pushes"][0][0]["text"].find("信息简报") == -1


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


# ---------- 素材单元 ----------

def test_fmt_overdue_due_inline():
    """逾期行：到期日并入首段（无多余空格），时间/标签空格分隔，坏 due 兜底。"""
    t = {"text": "修路由器", "due": "2026-08-28", "time": None, "tags": []}
    assert pw.fmt_overdue([t]) == "- 修路由器（8 月 28 日到期）"
    t2 = {"text": "x", "due": "bad-date", "time": "09:00", "tags": ["工作"]}
    assert pw.fmt_overdue([t2]) == "- x（bad-date 到期） ⏰ 09:00 #工作"
