"""todo 模块业务回归（internal 加载 / 坏 JSON / repeat 展开 / 提醒分组 / dry-run 零副作用 / vault / 共享）。

运行方式（需指定宿主项目以提供 common 公共库）：
    WECHAT_CLAW_HOST=/home/xinyi/wechat-claw-dist \
        python -m pytest wechat-claw_modules_official/todo/tests/
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

MODULE_SRC = Path(__file__).resolve().parent.parent  # todo/
sys.path.insert(0, str(MODULE_SRC))

import todo_worker as tw  # noqa: E402

try:
    import common  # noqa: F401  # 验证宿主注入是否成功
except ImportError:
    pytest.skip("缺少宿主 common 库：请设置 WECHAT_CLAW_HOST=<宿主项目根>", allow_module_level=True)

TODAY = date(2026, 8, 21)
TOMORROW = TODAY + timedelta(days=1)


def _mk(tmp: Path):
    """构造隔离环境（模块目录/任务目录/防重文件 + mock 副作用），返回上下文。"""
    tw.MODULE_DIR = tmp / "todo"
    tw.MODULE_DIR.mkdir()
    tw.TASKS_DIR = tmp / "tasks"
    tw.TASKS_DIR.mkdir()
    tw.SENT_FILE = tmp / "sent.json"
    tw.SCAN_CACHE_FILE = tmp / "scan_cache.json"
    ctx = {"shared": []}
    tw.shared_save = lambda name, data: (ctx["shared"].append((name, data)), True)[1]
    tw.post_push = lambda body, token: True
    tw.load_token = lambda d: "mock"
    return ctx


def _write(tasks: list[dict]):
    (tw.TASKS_DIR / TODAY.strftime("%Y-%m.json")).write_text(json.dumps({"tasks": tasks}))


def _task(rid: str, due: str, time_: str | None = "14:00", **kw) -> dict:
    base = {"id": rid, "text": f"任务{rid}", "due": due, "time": time_, "remind_min": None,
            "done": False, "repeat": None, "done_dates": [], "tags": []}
    base.update(kw)
    return base


# ---------- internal 加载 ----------

def test_load_internal_basic_and_dedup():
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    _write([_task("a", TODAY.isoformat()), _task("a", TODAY.isoformat())])  # 同 id 去重
    tasks = tw.load_internal(TODAY)
    assert len(tasks) == 1


def test_load_internal_done_at_preserved():
    """done_at 字段透传（internal 完成时间戳）；旧数据缺省 None。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    _write([_task("a", TODAY.isoformat(), done=True, done_at=f"{TODAY}T09:30:00"),
            _task("b", TODAY.isoformat(), done=True)])  # 旧数据无 done_at
    tasks = {t["id"]: t for t in tw.load_internal(TODAY)}
    assert tasks["a"]["done_at"] == f"{TODAY}T09:30:00"
    assert tasks["b"]["done_at"] is None


def test_load_internal_bad_json_tolerated():
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    _write([_task("a", TODAY.isoformat())])
    (tw.TASKS_DIR / "bad.json").write_text("{bad")  # 坏文件跳过不崩
    assert len(tw.load_internal(TODAY)) == 1


def test_load_internal_cross_month_all_scanned():
    """全扫所有月份文件（防跨月漏）。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    _write([_task("a", TODAY.isoformat())])
    (tw.TASKS_DIR / "2099-01.json").write_text(json.dumps({"tasks": [_task("b", "2099-01-01")]}))
    assert {t["id"] for t in tw.load_internal(TODAY)} == {"a", "b"}


def test_norm_task_missing_id_autofilled():
    """agent 直写缺 id → 自动补算 sha1(due|text)[:8]（与 vault 同式，跨源一致）。"""
    t = {"text": "取快递", "due": TODAY.isoformat()}
    n = tw._norm_task(t, TODAY)
    expected = hashlib.sha1(f"{TODAY.isoformat()}|取快递".encode()).hexdigest()[:8]
    assert n is not None and n["id"] == expected


def test_norm_task_missing_text_or_due_still_skipped():
    """兜底只救缺 id：text/due 缺失仍跳过。"""
    assert tw._norm_task({"due": TODAY.isoformat()}, TODAY) is None
    assert tw._norm_task({"text": "无日期"}, TODAY) is None


def test_norm_task_autofill_id_deterministic():
    """同 text+due 补出同 id → load_internal 去重合并，防重键（日期|id）稳定。"""
    t = {"text": "交房租", "due": TOMORROW.isoformat()}
    _mk(Path(tempfile.mkdtemp()))
    _write([t, dict(t)])  # 两条同文同日、均无 id
    tasks = tw.load_internal(TODAY)
    assert len(tasks) == 1
    assert tasks[0]["id"] == hashlib.sha1(f"{TOMORROW.isoformat()}|交房租".encode()).hexdigest()[:8]


# ---------- repeat 展开 ----------

def test_repeat_daily_and_until():
    assert tw.repeat_due({"due": "2026-08-19", "repeat": {"freq": "daily", "interval": 1, "until": "2026-08-20"}},
                         date(2026, 8, 25)) == date(2026, 8, 20)  # until 截断
    assert tw.repeat_due({"due": "2026-08-19", "repeat": {"freq": "daily", "interval": 2}},
                         date(2026, 8, 23)) == date(2026, 8, 23)  # 隔天


def test_repeat_monthly_month_end_clamp():
    assert tw.repeat_due({"due": "2026-08-31", "repeat": {"freq": "monthly", "interval": 1}},
                         date(2026, 9, 30)) == date(2026, 9, 30)  # 8-31 → 9-30 月末收敛


def test_no_repeat_returns_due():
    assert tw.repeat_due({"due": "2026-08-19"}, date(2026, 8, 25)) == date(2026, 8, 19)


# ---------- 提醒分组 ----------

def test_reminders_group_and_skip():
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    now = datetime(2026, 8, 21, 20, 0)
    _write([
        _task("a", TODAY.isoformat(), "14:30", remind_min=15),       # 触发 14:15 已到 → 提醒
        _task("b", TODAY.isoformat(), "23:59"),                      # 未到 → 跳过
        _task("c", TOMORROW.isoformat()),                            # 明天 → 跳过
        _task("d", TODAY.isoformat(), done=True),                    # 已完成 → 跳过
        _task("r", (TODAY - timedelta(days=1)).isoformat(), "14:00",
              repeat={"freq": "daily", "interval": 1}),              # 重复今天到期 → 提醒
    ])
    groups = tw.compute_reminders(tw.load_internal(TODAY), now, {})
    ids = {t["id"] for _, items in groups for t in items}
    assert {"a", "r"} == ids, ids
    assert not ({"b", "c", "d"} & ids)


def test_reminders_dedup_key():
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    now = datetime(2026, 8, 21, 20, 0)
    _write([_task("a", TODAY.isoformat(), "14:00")])
    sent = {f"{TODAY}|a": "x"}
    assert tw.compute_reminders(tw.load_internal(TODAY), now, sent) == []


def test_reminders_repeat_done_dates_skip():
    """重复任务今天在 done_dates → 跳过。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    now = datetime(2026, 8, 21, 20, 0)
    _write([_task("r", (TODAY - timedelta(days=1)).isoformat(), "14:00",
                  repeat={"freq": "daily", "interval": 1}, done_dates=[TODAY.isoformat()])])
    assert tw.compute_reminders(tw.load_internal(TODAY), now, {}) == []


def test_no_time_task_not_reminded():
    """无 time 任务不进提醒（仅共享层）。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    now = datetime(2026, 8, 21, 20, 0)
    _write([_task("a", TODAY.isoformat(), None)])
    assert tw.compute_reminders(tw.load_internal(TODAY), now, {}) == []


# ---------- dry-run 零副作用 / 共享刷新 ----------

def _fixed_now(monkeypatch, hour: int = 20, minute: int = 0):
    """打桩 worker 的 datetime.now（消除时间敏感：任何真实时刻都能测）。"""
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 21, hour, minute)
    monkeypatch.setattr(tw, "datetime", FakeDT)


def test_dry_run_zero_side_effect(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _write([_task("a", TODAY.isoformat(), "14:00")])
    _fixed_now(monkeypatch)
    assert tw.main(["--dry-run"]) == 0
    assert not tw.SENT_FILE.exists()       # 不写防重
    assert not ctx["shared"]               # 不刷共享


def test_run_refresh_shared_and_sent(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    _write([_task("a", TODAY.isoformat(), "14:00")])
    _fixed_now(monkeypatch)
    assert tw.main([]) == 0
    assert tw.SENT_FILE.exists()           # 记防重
    assert ctx["shared"] and ctx["shared"][-1][0] == "tasks"  # 刷共享
    assert any("tasks" in data for _, data in ctx["shared"])


# ---------- vault 模式 ----------

def test_vault_due_only_and_tags():
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    vault = tmp / "vault"
    vault.mkdir()
    (vault / "notes.md").write_text(
        f"- [ ] 买菜 📅 {TODAY} ⏰ 09:00 🔔提前15分钟 #todo/生活\n"
        "- [ ] 无日期任务\n"
        f"- [ ] 明天的事 📅 {TOMORROW} #todo/工作\n"
    )
    tasks = tw.load_vault({"vault_path": str(vault), "tag_prefix": "#todo/", "extract_tags": True}, TODAY)
    assert len(tasks) == 2, [t["text"] for t in tasks]           # 无日期不算
    by_text = {t["text"]: t for t in tasks}
    assert by_text["买菜"]["remind_min"] == 15 and by_text["买菜"]["tags"] == ["生活"]
    assert by_text["明天的事"]["tags"] == ["工作"]


def test_vault_done_at_from_tasks_done_mark():
    """vault 模式：✅ YYYY-MM-DD（Tasks 语法）→ done_at 日期粒度 + done=true。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    vault = tmp / "vault"
    vault.mkdir()
    (vault / "n.md").write_text(
        f"- [ ] 写完周报 📅 {TODAY} ⏰ 18:00\n"
        f"- [x] 已完成任务 📅 {TODAY} ✅ {TODAY}\n"
    )
    tasks = tw.load_vault({"vault_path": str(vault), "tag_prefix": "#todo/", "extract_tags": True}, TODAY)
    by_text = {t["text"]: t for t in tasks}
    assert by_text["已完成任务"]["done"] is True
    assert by_text["已完成任务"]["done_at"] == TODAY.isoformat()   # vault 只有日期粒度
    assert by_text["写完周报"]["done_at"] is None


def test_vault_tag_prefix_empty_not_extract():
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    vault = tmp / "vault"
    vault.mkdir()
    (vault / "n.md").write_text(f"- [ ] 任务 📅 {TODAY} #工作\n")
    tasks = tw.load_vault({"vault_path": str(vault), "tag_prefix": "", "extract_tags": True}, TODAY)
    assert tasks and tasks[0]["tags"] == []


def test_vault_path_missing_safe():
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    assert tw.load_vault({"vault_path": ""}, TODAY) == []
    assert tw.load_vault({"vault_path": "/no/such/dir/xyz"}, TODAY) == []


# ---------- T11 跨日场景（案例 2/4） ----------

def test_reminder_cross_day_single():
    """案例2：明日 00:30 到期提前 60 分钟 → 今日（前一日）23:30 提醒，TOMORROW 当天不再提醒。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    now = datetime(2026, 8, 21, 23, 35)                       # 前一日 23:35
    _write([_task("x", TOMORROW.isoformat(), "00:30", remind_min=60)])
    tasks_today = tw.load_internal(TODAY)                     # 今日=08-21，跨日 target_due=08-22
    by_id = {t["id"]: t for t in tasks_today}
    assert by_id["x"]["reminder_date"] == TODAY.isoformat()   # 提醒日=今日
    assert by_id["x"]["reminder_time"] == "23:30"
    groups = tw.compute_reminders(tasks_today, now, {})
    assert [t["id"] for _, items in groups for t in items] == ["x"]
    # 次日（08-22）扫描：跨日任务的提醒日在前一日，当天不再提醒
    tasks_next = tw.load_internal(TOMORROW)
    by_id_next = {t["id"]: t for t in tasks_next}
    assert by_id_next["x"]["reminder_date"] is None           # 08-22 非提醒日


def test_reminder_cross_day_repeat():
    """案例4：重复任务每日 00:30 到期提前 60 分钟 → 每日前一日 23:30 提醒。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    now = datetime(2026, 8, 21, 23, 35)
    _write([_task("r", (TODAY - timedelta(days=3)).isoformat(), "00:30",
                  remind_min=60, repeat={"freq": "daily", "interval": 1})])
    tasks = tw.load_internal(TODAY)
    by_id = {t["id"]: t for t in tasks}
    assert by_id["r"]["reminder_date"] == TODAY.isoformat()   # 今日提醒明日 00:30 的重复
    assert by_id["r"]["reminder_time"] == "23:30"
    groups = tw.compute_reminders(tasks, now, {})
    assert [t["id"] for _, items in groups for t in items] == ["r"]


def test_reminder_bad_due_skipped():
    """T2 兜底：非法 due（如 abc）跳过不崩，且进 bad_due 日志路径。"""
    tmp = Path(tempfile.mkdtemp())
    _mk(tmp)
    _write([_task("bad", "not-a-date", "14:00"), _task("ok", TODAY.isoformat(), "14:00")])
    tasks = tw.load_internal(TODAY)
    assert {t["id"] for t in tasks} == {"ok"}                 # 坏 due 被跳过


def test_settings_corrupt_blocks(monkeypatch):
    """T1：settings.json 存在但坏 → return 1（阻塞），且当天只推一次 notification。"""
    tmp = Path(tempfile.mkdtemp())
    ctx = _mk(tmp)
    ctx["pushes"] = []
    tw.post_push = lambda body, token: (ctx["pushes"].append(body), True)[1]
    # settings.json 存在但内容坏（_mk 未设 SETTINGS_FILE → 指到 tmp）
    tw.SETTINGS_FILE = tmp / "settings.json"
    tw.SETTINGS_FILE.write_text("{broken")
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 21, 20, 0)
    monkeypatch.setattr(tw, "datetime", FakeDT)
    assert tw.main([]) == 1                                    # 阻塞
    notes = [b for b in ctx["pushes"] if b["type"] == "notification"]
    assert len(notes) == 1 and "settings.json 损坏" in notes[0]["text"]
    assert tw.main([]) == 1                                    # 仍阻塞
    notes = [b for b in ctx["pushes"] if b["type"] == "notification"]
    assert len(notes) == 1                                     # 防刷屏：当天不重复推
