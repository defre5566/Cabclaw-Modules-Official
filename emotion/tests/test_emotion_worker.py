"""emotion_worker 测试：全 mock，不触网、不推微信、不碰宿主真实数据区。

mock 面：post_push / load_token / get_weather / shared_load / shared_save / encrypt / decrypt
+ datetime.now / time.time 注入假时钟 + 数据目录指向 tmp_path。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

MODULE_SRC = Path(__file__).resolve().parent.parent  # emotion/
sys.path.insert(0, str(MODULE_SRC))

import emotion_worker as ew  # noqa: E402

try:
    import common  # noqa: F401  # 验证宿主注入是否成功
except ImportError:
    pytest.skip("缺少宿主 common 库：请设置 CABCLAW_HOST=<宿主项目根>", allow_module_level=True)


# ---------- fixtures ----------

@pytest.fixture
def ew_env(tmp_path, monkeypatch):
    d = tmp_path / "emotion"
    calls = {"push": [], "shared": [], "events": {}}
    monkeypatch.setattr(ew, "DATA_DIR", d)
    monkeypatch.setattr(ew, "STATE_FILE", d / "state.enc")
    monkeypatch.setattr(ew, "FEEDBACK_FILE", d / "feedback.enc")
    monkeypatch.setattr(ew, "SETTINGS_FILE", d / "settings.json")
    def post_push(payload, token):
        calls["push"].append(payload)
        calls["events"][payload["event_id"]] = "queued"
        return True
    monkeypatch.setattr(ew, "post_push", post_push)
    monkeypatch.setattr(ew, "probe_push_event", lambda event_id, token: (
        {"status": "found", "state": calls["events"][event_id]}
        if event_id in calls["events"] else {"status": "missing"}))
    monkeypatch.setattr(ew, "load_token", lambda m: "tok")
    monkeypatch.setattr(ew, "shared_save", lambda name, data: calls["shared"].append((name, data)) or True)
    monkeypatch.setattr(ew, "shared_load", lambda name, max_age=None: {"ts": 0, "tasks": []})
    monkeypatch.setattr(ew, "get_weather", lambda: "25°C 晴")
    monkeypatch.setattr(ew, "encrypt", lambda s: "enc:" + s)
    monkeypatch.setattr(ew, "decrypt", lambda s: s[4:] if s.startswith("enc:") else s)
    ew.calls = calls  # 挂到模块对象上，测试经 ew_env.calls 取
    return ew


def set_clock(ew, monkeypatch, dt: datetime):
    class FDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return dt
    monkeypatch.setattr(ew, "datetime", FDT)
    monkeypatch.setattr(ew.time, "time", lambda: dt.timestamp())


def write_state(ew, obj):
    ew._enc_save(ew.STATE_FILE, obj)


def read_state(ew):
    return ew._enc_load(ew.STATE_FILE, None)


# ---------- 拟人线（window） ----------

def test_window_care_topic_push(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    assert ew.main([]) == 0
    assert len(calls["push"]) == 1
    s = read_state(ew)
    assert s["daily_count"] == 0 and not s["dedup"]  # HTTP 200 只表示接受
    calls["events"][calls["push"][0]["event_id"]] = "sent"
    assert ew.main(["--reconcile"]) == 0
    s = read_state(ew)
    assert s["daily_count"] == 1
    assert f"2026-09-04|8" in s["dedup"]
    assert s["weather"] == {"temp": 25, "rainy": False}


def test_window_no_topic_silent(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 10))  # 9 点无时段关怀
    assert ew.main([]) == 0
    assert calls["push"] == []
    s = read_state(ew)
    assert "2026-09-04|9" in s["dedup"]  # 无话题也记防重（本小时不再评估）


def test_window_weather_changed(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 10))
    write_state(ew, {**ew._default_state(), "weather": {"temp": 20, "rainy": False}})
    assert ew.main([]) == 0  # 20→25 温差 5 → 有话题
    assert len(calls["push"]) == 1


def test_window_weather_no_baseline_silent(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 10))
    assert ew.main([]) == 0  # 无基线不突变 → 无话题
    assert calls["push"] == []


def test_window_daily_limit(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    write_state(ew, {**ew._default_state(), "daily_date": "2026-09-04", "daily_count": 4})
    assert ew.main([]) == 0
    assert calls["push"] == []


def test_window_min_interval(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 10))
    write_state(ew, {**ew._default_state(), "last_push_ts": (datetime(2026, 9, 4, 8, 30)).timestamp()})
    assert ew.main([]) == 0  # 距上次 40 分钟 < 3h
    assert calls["push"] == []


def test_window_cross_day_reset(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 5, 8, 30))
    write_state(ew, {**ew._default_state(), "daily_date": "2026-09-04", "daily_count": 4})
    assert ew.main([]) == 0  # 昨天达上限，今天归零可推
    assert len(calls["push"]) == 1
    assert read_state(ew)["daily_count"] == 0
    calls["events"][calls["push"][0]["event_id"]] = "sent"
    assert ew.main(["--reconcile"]) == 0
    assert read_state(ew)["daily_count"] == 1


def test_window_dedup_same_hour(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 50))
    write_state(ew, {**ew._default_state(), "dedup": {"2026-09-04|8": 1.0}})
    assert ew.main([]) == 0
    assert calls["push"] == []


def test_window_feedback_injected(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    write_state(ew, ew._default_state())
    ew._enc_save(ew.FEEDBACK_FILE, [{"ts": (datetime(2026, 9, 3, 21)).timestamp(),
                                     "text": "感冒了", "tag": "sick",
                                     "expires_at": (datetime(2026, 9, 4, 8)).timestamp() + 3600}])
    assert ew.main([]) == 0
    assert calls["push"][0]["type"] == "reminder" and "text" not in calls["push"][0]
    assert calls["push"][0]["facts"]["recent_feedback"][0]["text"] == "感冒了"


def test_window_paused_blocks(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    write_state(ew, {**ew._default_state(), "pause_until": (datetime(2026, 9, 4, 21)).timestamp()})
    assert ew.main([]) == 0
    assert calls["push"] == []


def test_window_dry_no_write(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    assert ew.main(["--dry-run"]) == 0
    assert not ew.STATE_FILE.exists()  # dry 零副作用
    assert calls["push"] == []


def test_window_push_fail_rc1(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    monkeypatch.setattr(ew, "post_push", lambda p, t: False)
    assert ew.main([]) == 1  # 失败 rc=1（不补发，scheduler 记日志）


# ---------- 任务播报线（phase） ----------

def todo_task(text="看论文", reminder_time="10:00", done=False):
    """todo shared schema 全字段契约样例（字段漂移本测试变红）。"""
    return {"id": "abc12345", "text": text, "due": "2026-09-04",
            "time": reminder_time, "remind_min": 15, "done": done,
            "done_at": None, "repeat": None, "done_dates": [], "tags": [],
            "reminder_date": "2026-09-04", "reminder_time": reminder_time}


def test_phase_push_and_crossline_backoff(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 0))
    monkeypatch.setattr(ew, "shared_load",
                        lambda name, max_age=None: {"ts": 0, "tasks": [todo_task()]})
    assert ew.main(["--phase", "morning"]) == 0
    assert len(calls["push"]) == 1
    s = read_state(ew)
    assert "2026-09-04|morning" not in s["dedup"]
    calls["events"][calls["push"][0]["event_id"]] = "sent"
    assert ew.main(["--reconcile"]) == 0
    s = read_state(ew)
    assert "2026-09-04|morning" in s["dedup"]
    assert s["last_push_ts"] is not None  # 播报后拟人线退避


def test_phase_fallback_time_field(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 0))
    t = todo_task()
    t["reminder_time"] = None  # 无派生触发时刻，回退 time
    monkeypatch.setattr(ew, "shared_load", lambda n, max_age=None: {"ts": 0, "tasks": [t]})
    assert ew.main(["--phase", "morning"]) == 0
    assert len(calls["push"]) == 1


def test_phase_no_task_skip(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 0))
    assert ew.main(["--phase", "morning"]) == 0
    assert calls["push"] == []
    assert "2026-09-04|morning" in read_state(ew)["dedup"]


def test_phase_done_excluded(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 0))
    monkeypatch.setattr(ew, "shared_load",
                        lambda n, max_age=None: {"ts": 0, "tasks": [todo_task(done=True)]})
    assert ew.main(["--phase", "morning"]) == 0
    assert calls["push"] == []


def test_phase_window_boundary(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 0))
    far = todo_task(text="远任务", reminder_time="13:00")  # 3h 窗口外
    monkeypatch.setattr(ew, "shared_load", lambda n, max_age=None: {"ts": 0, "tasks": [far]})
    assert ew.main(["--phase", "morning"]) == 0
    assert calls["push"] == []


def test_phase_dedup_same_day(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 5))
    write_state(ew, {**ew._default_state(), "dedup": {"2026-09-04|morning": 1.0}})
    monkeypatch.setattr(ew, "shared_load",
                        lambda n, max_age=None: {"ts": 0, "tasks": [todo_task()]})
    assert ew.main(["--phase", "morning"]) == 0
    assert calls["push"] == []


def test_phase_paused_blocks(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 0))
    write_state(ew, {**ew._default_state(), "pause_until": (datetime(2026, 9, 4, 21)).timestamp()})
    monkeypatch.setattr(ew, "shared_load",
                        lambda n, max_age=None: {"ts": 0, "tasks": [todo_task()]})
    assert ew.main(["--phase", "morning"]) == 0  # 全停语义
    assert calls["push"] == []


def test_phase_bad_rc1(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 0))
    assert ew.main(["--phase", "noon"]) == 1


# ---------- 倾听线（inbound） ----------

def test_inbound_pause(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 15, 0))
    assert ew.main(["--inbound", "暂停关心"]) == 0
    s = read_state(ew)
    assert s["pause_until"] == datetime(2026, 9, 4, 21).timestamp()


def test_inbound_pause_after_21_expires(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 22, 0))
    assert ew.main(["--inbound", "暂停关心"]) == 0
    s = read_state(ew)
    assert not ew._paused(s, datetime(2026, 9, 5, 8, 30))  # 截止已过 = 自然失效（忽略）


def test_inbound_resume_idempotent(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 15, 0))
    assert ew.main(["--inbound", "恢复关心"]) == 0  # 无暂停也幂等确认
    assert read_state(ew)["pause_until"] is None


def test_inbound_feedback_rc3_and_shared_tag(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 15, 0))
    assert ew.main(["--inbound", "今天好累啊"]) == 3  # 转 agent
    fb = ew._enc_load(ew.FEEDBACK_FILE, [])
    assert len(fb) == 1 and fb[0]["tag"] == "tired"
    name, data = calls["shared"][0]
    assert name == "emotion_user_state"
    assert data["status"] == "tired" and "text" not in data  # shared 只存标签不存原话


def test_inbound_preference_rc3_no_record(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 15, 0))
    assert ew.main(["--inbound", "以后别叫我宝"]) == 3
    assert ew._enc_load(ew.FEEDBACK_FILE, []) == []  # 长期偏好不记录，只转交


def test_inbound_unknown_rc3(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 15, 0))
    assert ew.main(["--inbound", "今天天气不错"]) == 3  # 不确定宁转不错


def test_inbound_not_blocked_by_pause(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 15, 0))
    write_state(ew, {**ew._default_state(), "pause_until": (datetime(2026, 9, 4, 21)).timestamp()})
    assert ew.main(["--inbound", "我感冒了"]) == 3  # 暂停不拦 inbound 转发


# ---------- 数据维护 ----------

def test_feedback_prune_expired(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 15, 0))
    old = {"ts": 0, "text": "旧的", "tag": "tired", "expires_at": 1}  # 早已过期
    ew._enc_save(ew.FEEDBACK_FILE, [old])
    ew._record_feedback("又累了", "tired")
    fb = ew._enc_load(ew.FEEDBACK_FILE, [])
    assert len(fb) == 1 and fb[0]["text"] == "又累了"


def test_state_decrypt_fail_closed(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    ew.DATA_DIR.mkdir(parents=True, exist_ok=True)
    ew.STATE_FILE.write_text("garbage-not-encrypted")  # 密文坏
    monkeypatch.setattr(ew, "decrypt", lambda s: (_ for _ in ()).throw(ValueError("bad")))
    assert ew.main([]) == 1  # 不可按空状态重生成业务键，也不能覆盖原文件
    assert ew.STATE_FILE.read_text() == "garbage-not-encrypted"
    assert calls["push"] == []


def test_state_pending_record_corrupt_fails_closed(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    broken = {**ew._default_state(), "pending": {"lost-event": {"status": "pending"}}}
    ew._enc_save(ew.STATE_FILE, broken)
    assert ew.main(["--reconcile"]) == 1
    assert ew._enc_load(ew.STATE_FILE, {}) == broken and calls["push"] == []


def test_enc_roundtrip(ew_env, monkeypatch):
    ew, _ = ew_env, ew_env.calls
    obj = {"a": 1, "b": ["中文"], "c": None}
    ew._enc_save(ew.STATE_FILE, obj)
    assert read_state(ew) == obj


def test_dedup_pruned_2_days(ew_env, monkeypatch):
    ew, _ = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 5, 8, 30))
    write_state(ew, {**ew._default_state(),
                     "dedup": {"2026-09-03|8": 1.0, "2026-09-04|8": 1.0, "2026-09-05|8": 1.0}})
    s = ew._load_state(datetime(2026, 9, 5, 8, 30))
    assert "2026-09-03|8" not in s["dedup"]  # 只留最近 2 天
    assert "2026-09-05|8" in s["dedup"]


def test_lost_post_receipt_probed_before_retry(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))

    def lost(payload, token):
        calls["push"].append(payload)
        calls["events"][payload["event_id"]] = "queued"
        return False

    monkeypatch.setattr(ew, "post_push", lost)
    assert ew.main([]) == 1
    assert ew.main(["--reconcile"]) == 0
    assert len(calls["push"]) == 1 and read_state(ew)["daily_count"] == 0


def test_unknown_status_keeps_business_key_without_sent_counter(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    assert ew.main([]) == 0
    event_id = calls["push"][0]["event_id"]
    calls["events"][event_id] = "unknown"
    assert ew.main(["--reconcile"]) == 0
    assert ew.main([]) == 0
    assert len(calls["push"]) == 1
    assert read_state(ew)["pending"][event_id]["status"] == "unknown"
    assert read_state(ew)["daily_count"] == 0


def test_pause_prevents_pending_event_first_submission(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    state = ew._default_state()
    state["pending"] = {
        "emotion:2026-09-04:care:8": {
            "slot": "2026-09-04|8", "kind": "care", "business_date": "2026-09-04",
            "reserved_at": datetime(2026, 9, 4, 8, 30).timestamp(),
            "payload": {"type": "reminder", "event_id": "emotion:2026-09-04:care:8",
                        "facts": {"period": 8}, "intent": "关怀", "must_preserve": []},
            "status": "pending",
        },
    }
    state["pause_until"] = datetime(2026, 9, 4, 21).timestamp()
    write_state(ew, state)
    assert ew.main(["--reconcile"]) == 0
    assert calls["push"] == []
    assert read_state(ew)["pending"]["emotion:2026-09-04:care:8"]["status"] == "expired"


def test_disabled_care_does_not_first_submit_reserved_event(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    monkeypatch.setattr(ew, "post_push", lambda *_: False)
    assert ew.main([]) == 1
    assert read_state(ew)["pending"]
    ew.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ew.SETTINGS_FILE.write_text(json.dumps({"emotion_on": False}), encoding="utf-8")
    assert ew.main(["--reconcile"]) == 0
    record = next(iter(read_state(ew)["pending"].values()))
    assert record["status"] == "expired" and calls["push"] == []


def test_pause_arrives_during_status_probe_is_not_overwritten(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 8, 30))
    assert ew.main([]) == 0
    event_id = calls["push"][0]["event_id"]
    calls["events"][event_id] = "sent"

    def probe_then_pause(_id, _token):
        assert _id == event_id
        assert ew._inbound("暂停关心") == 0
        return {"status": "found", "state": "sent"}

    monkeypatch.setattr(ew, "probe_push_event", probe_then_pause)
    assert ew.main(["--reconcile"]) == 0
    recovered = read_state(ew)
    assert recovered["pause_until"] == datetime(2026, 9, 4, 21).timestamp()
    assert recovered["pending"][event_id]["status"] == "sent"
    assert recovered["daily_count"] == 1


def test_reconcile_cursor_processes_at_most_two_events_per_tick(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 12, 0))
    state = ew._default_state()
    state["pending"] = {
        f"emotion:2026-09-04:care:{hour}": {
            "slot": f"2026-09-04|{hour}", "kind": "care", "business_date": "2026-09-04",
            "reserved_at": datetime(2026, 9, 4, hour).timestamp(),
            "payload": {"type": "reminder", "event_id": f"emotion:2026-09-04:care:{hour}",
                        "facts": {"period": hour}, "intent": "关怀", "must_preserve": []},
            "status": "pending"} for hour in range(6, 12)
    }
    write_state(ew, state)
    queried = []

    def unavailable(event_id, _token):
        queried.append(event_id)
        return {"status": "unavailable"}

    monkeypatch.setattr(ew, "probe_push_event", unavailable)
    assert ew.main(["--reconcile"]) == 1
    assert len(queried) == 2
    assert ew.main(["--reconcile"]) == 1
    assert len(queried) == 4
    assert ew.main(["--reconcile"]) == 1
    assert len(queried) == 6
    assert set(queried) == set(state["pending"])


def test_phase_facts_never_send_internal_instructions(ew_env, monkeypatch):
    ew, calls = ew_env, ew_env.calls
    set_clock(ew, monkeypatch, datetime(2026, 9, 4, 9, 0))
    monkeypatch.setattr(ew, "shared_load", lambda name, max_age=None: {"tasks": [todo_task()]})
    assert ew.main(["--phase", "morning"]) == 0
    payload = calls["push"][0]
    assert payload["type"] == "reminder" and "text" not in payload
    assert payload["facts"]["task_count"] == 1
    assert "请给用户" not in json.dumps(payload, ensure_ascii=False)


# ---------- 部署形态：裸 spawn（无 PYTHONPATH 注入） ----------

def test_bare_spawn_no_pythonpath(ew_env, monkeypatch, tmp_path):
    """模拟部署裸 spawn：bridge inbound（main.py:368）不注入 PYTHONPATH，
    worker 必须自持 sys.path（项目根 bridge + modules/ common）才能 import 成功。

    搭临时部署树（symlink 宿主 bridge/common + 拷贝 worker），env 剥掉 PYTHONPATH、
    cwd 挪走——修复前此用例必红（ImportError: No module named 'bridge'）。
    --inspect 零副作用（state.enc 不存在时不触碰 crypto、不写文件）。
    """
    ew, _ = ew_env, ew_env.calls
    host = Path(os.environ["CABCLAW_HOST"]).resolve()
    deploy = tmp_path / "deploy"
    (deploy / "modules").mkdir(parents=True)
    (deploy / "bridge").symlink_to(host / "bridge", target_is_directory=True)
    (deploy / "modules" / "common").symlink_to(host / "modules" / "common", target_is_directory=True)
    shutil.copytree(Path(ew.__file__).parent, deploy / "modules" / "emotion",
                    ignore=shutil.ignore_patterns("__pycache__"))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, str(deploy / "modules" / "emotion" / "emotion_worker.py"), "--inspect"],
        capture_output=True, text=True, env=env,
        cwd=str(tmp_path),  # cwd 也挪走：证明不依赖 cwd
        timeout=60,
    )
    assert proc.returncode == 0, f"裸 spawn 失败:\n{proc.stderr[-800:]}"
    assert "state:" in proc.stdout
