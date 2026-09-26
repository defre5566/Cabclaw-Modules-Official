"""三个业务模块的事实载荷对宿主通用 push 契约的隔离测试。"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

if not os.environ.get("CABCLAW_HOST"):
    pytest.skip("须指定 CABCLAW_HOST 注入宿主公共库", allow_module_level=True)

PLANNER_SRC = Path(__file__).resolve().parents[1]
if str(PLANNER_SRC) not in sys.path:
    sys.path.insert(0, str(PLANNER_SRC))

from bridge import main as bridge_main, paths, push_outbox, push_render, push_server, state  # noqa: E402
from bridge.push_outbox import PushOutbox  # noqa: E402
from modules import register as registration  # noqa: E402
import common.push as push_client  # noqa: E402
import planner_worker as planner  # noqa: E402


def _module_worker(name: str):
    """只装载当前模块仓的 worker 源码，不引用安装态或任何账号/token。"""
    source = Path(__file__).resolve().parents[2] / name
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    path = source / f"{name}_worker.py"
    spec = importlib.util.spec_from_file_location(f"contract_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_fact_payloads_and_briefing_dependency(tmp_path, monkeypatch):
    """真实校验/加密/状态机接线；模型、会话、附件都只用合成替身。"""
    today = date.today()
    monkeypatch.setattr(push_outbox, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(push_render, "WORK_ROOT", tmp_path)
    persona = tmp_path / "instructions" / "tier-current.md"
    persona.parent.mkdir()
    persona.write_text("像朋友一样简明说明已给出的事实。", encoding="utf-8")
    briefing = tmp_path / "modules" / "modules_data" / "Planner" / "briefing" / f"{today}.html"
    briefing.parent.mkdir(parents=True)
    briefing.write_text(
        f'<html><head><title>合成简报</title></head><body>{today}'
        '<p>合成新闻</p><a href="https://example.org/story">来源</a></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(planner, "_calendar_facts", lambda d: {"business_date": d.isoformat()})
    monkeypatch.setattr(planner, "_tasks_stale", lambda: False)
    monkeypatch.setattr(planner, "_localdata_section", lambda _settings: [])
    monkeypatch.setattr(planner, "get_weather", lambda: "晴 22°C")
    monkeypatch.setattr(planner, "weather_alerts", lambda: [])
    task = {"text": "准备周会", "due": today.isoformat(), "time": "09:30", "tags": []}
    reminder, attachment = planner._report_payload(
        "morning", today, {"today": [task], "overdue": [], "done_today": []},
        [], dict(planner.DEFAULT_SETTINGS), False, briefing,
    )
    todo = _module_worker("todo")
    emotion = _module_worker("emotion")
    todo_task = {"id": "synthetic", "text": "准备周会", "due": today.isoformat(),
                 "time": "09:30", "remind_min": 15, "reminder_date": today.isoformat(),
                 "reminder_time": "09:15", "repeat": None}
    todo_payload = todo._reminder_payload(today, "09:15", [todo_task])
    emo_payload = {"type": "reminder", "event_id": emotion._event_id(datetime.now(), "phase", "morning"),
                   "facts": emotion._phase_facts("morning", [{"text": "准备周会", "_tt": datetime.now()}],
                                                 datetime.now()),
                   "intent": "依据当前时段事实轻量鼓励", "must_preserve": []}

    box = PushOutbox(tmp_path / "outbox", tmp_path / "master.key")
    try:
        for module, body in (("todo", todo_payload), ("emotion", emo_payload), ("Planner", reminder)):
            assert box.accept(module, body, "synthetic-conversation")["state"] == "queued"
        assert box.accept("Planner", attachment, "synthetic-conversation")["state"] == "queued"
        assert attachment["after_event_id"] == reminder["event_id"]
        assert "text" not in todo_payload and "text" not in reminder and "text" not in emo_payload
        first = box.claim_next()
        second = box.claim_next()
        third = box.claim_next()
        assert {first["module"], second["module"], third["module"]} == {"todo", "emotion", "Planner"}
        assert box.claim_next() is None  # 文件不能抢在早报文字 SDK 返回成功前发送

        messages = []

        async def model(payload, **kwargs):
            messages.extend(payload)
            assert kwargs["tools"] is None
            return {"content": f"准备周会 {today} 09:30，先看合成新闻要点。", "finish_reason": "stop"}

        monkeypatch.setattr(push_render, "chat_completion", model)
        text = asyncio.run(push_render.render_reminder("Planner", third["body"]))
        assert "准备周会" in text
        assert str(briefing) not in json.dumps(messages, ensure_ascii=False)
        box.rendered("Planner", reminder["event_id"], text)
        box.finish("Planner", reminder["event_id"], "sent")
        file_event = box.claim_next()
        assert file_event is not None and file_event["event_id"] == attachment["event_id"]
        assert file_event["target"] == "synthetic-conversation"
        assert box.claim_next() is None
    finally:
        box.close()


def test_local_http_ack_persona_and_attachment_order(tmp_path, monkeypatch):
    """真 HTTP + 公共客户端 + 双 worker；只有假的 SDK/模型和合成 token。"""
    today = date.today()
    monkeypatch.setattr(push_outbox, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(push_render, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(push_server, "PUSH_PORT", 0)
    monkeypatch.setattr(push_server, "load_or_create_token", lambda: "synthetic-host-token")
    module_token = "synthetic-planner-token"
    monkeypatch.setattr(push_server, "build_index", lambda: {
        "Planner": {"token_hash": hashlib.sha256(module_token.encode()).hexdigest()}})
    monkeypatch.setattr(state, "targets_for_text", lambda text: ("test-target", text))
    monkeypatch.setattr(push_client, "RETRY", 1)
    monkeypatch.setattr(push_client, "TIMEOUT", 2)
    (tmp_path / "instructions").mkdir()
    (tmp_path / "instructions" / "tier-current.md").write_text("自然播报", encoding="utf-8")
    brief = tmp_path / "modules" / "modules_data" / "Planner" / "briefing" / f"{today}.html"
    brief.parent.mkdir(parents=True)
    brief.write_text(f'<html><title>合成简报</title><body>{today} 合成事实'
                     '<a href="https://example.org/test">来源</a></body></html>', encoding="utf-8")
    monkeypatch.setattr(planner, "_calendar_facts", lambda d: {"business_date": d.isoformat()})
    monkeypatch.setattr(planner, "get_weather", lambda: "晴")
    monkeypatch.setattr(planner, "weather_alerts", lambda: [])
    monkeypatch.setattr(planner, "_localdata_section", lambda _s: [])
    monkeypatch.setattr(planner, "_tasks_stale", lambda: False)
    reminder, attachment = planner._report_payload(
        "morning", today, {"today": [{"text": "准备周会", "due": today.isoformat(),
                                       "time": "09:30", "tags": []}], "overdue": [], "done_today": []},
        [], dict(planner.DEFAULT_SETTINGS), False, brief,
    )
    calls = []

    async def model(messages, **kwargs):
        assert kwargs["tools"] is None and "自然播报" in messages[0]["content"]
        assert str(brief) not in json.dumps(messages, ensure_ascii=False)
        calls.append("render")
        return {"content": f"准备周会：{today} 09:30 到期。合成事实要点。", "finish_reason": "stop"}

    monkeypatch.setattr(push_render, "chat_completion", model)
    box = PushOutbox(tmp_path / "outbox", tmp_path / "master.key")
    core = bridge_main.BridgeCore()
    core.outbox = box

    async def send_text(target, text):
        assert target == "test-target" and "准备周会" in text
        calls.append("text")

    async def send_media(target, data, _kind, filename):
        assert target == "test-target" and filename == brief.name and data == brief.read_bytes()
        calls.append("file")

    core.send_text = send_text
    core._transport = SimpleNamespace(send_media=send_media)

    async def run():
        core.send_lock = asyncio.Lock()
        core.outbox_wake = asyncio.Event()
        httpd = push_server.start_push_server(asyncio.Queue(), box, core.outbox_wake)
        monkeypatch.setattr(push_client, "PUSH_URL", f"http://127.0.0.1:{httpd.server_port}/push")
        try:
            assert (await asyncio.to_thread(push_client.probe_push_event, reminder["event_id"],
                                             module_token))["status"] == "missing"
            assert await asyncio.to_thread(push_client.post_push, reminder, module_token)
            assert await asyncio.to_thread(push_client.post_push, attachment, module_token)
            assert calls == []  # ACK 不意味着模型/SDK 已调用
            assert box.get("Planner", attachment["event_id"])["state"] == "queued"
            workers = [asyncio.create_task(core.outbox_worker()) for _ in range(2)]
            try:
                for _ in range(100):
                    if box.get("Planner", attachment["event_id"])["state"] == "sent":
                        break
                    await asyncio.sleep(.01)
                assert box.get("Planner", reminder["event_id"])["state"] == "sent"
                assert box.get("Planner", attachment["event_id"])["state"] == "sent"
                assert calls == ["render", "text", "file"]
                assert (await asyncio.to_thread(push_client.probe_push_event, attachment["event_id"],
                                                 module_token))["state"] == "sent"
            finally:
                for task in workers:
                    task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
        finally:
            httpd.shutdown()
            httpd.server_close()

    try:
        asyncio.run(run())
    finally:
        box.close()


@pytest.mark.parametrize("name,settings,expected", [
    ("Planner", {"planner_on": True, "morning_time": "07:45", "evening_time": "20:10"},
     {"morning": "45 7 * * *", "evening": "10 20 * * *"}),
    ("emotion", {"tasks_report_on": True, "report_morning": "08:20",
                 "report_midday": "13:15", "report_evening": "19:00"},
     {"morning": "20 8 * * *", "midday": "15 13 * * *", "evening": "0 19 * * *"}),
])
def test_settings_sync_keeps_reconcile_and_default_schedule(name, settings, expected, monkeypatch):
    """设置联动只覆盖同 phase cron，不抹掉静态对账和情绪 window 规则。"""
    src = Path(__file__).resolve().parents[2] / name / "module.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    before = {entry.get("id"): entry.get("cron") for entry in data["schedule"] if entry.get("cron")}
    assert set(before) == set(expected)
    monkeypatch.setattr(registration, "_load_module_json", lambda _name: dict(data))
    monkeypatch.setattr(registration, "_load_settings_json", lambda _name: settings)
    saved = []
    monkeypatch.setattr(registration, "_save_module_json", lambda _name, result: saved.append(result) or True)
    registration._sync_schedule_from_settings(name)
    rules = saved[0]["schedule"]
    assert {entry["id"]: entry["cron"] for entry in rules if "cron" in entry} == expected
    assert len([entry for entry in rules if entry.get("id") == "push-reconcile"
                and entry.get("every") == "1m" and entry.get("args") == ["--reconcile"]]) == 1
    if name == "emotion":
        assert len([entry for entry in rules if "window" in entry]) == 1
