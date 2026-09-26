"""emotion 模块：拟人化主动关心（人设拓展）——时段问候/天气/倾听陪伴 + 任务三阶段轻量鼓励。

调度由 bridge scheduler 按 module.json 触发：
- window 8-21（拟人线）：限流内"有话题才发"，话题 = 时段问候 / 天气突变 / 近期反馈注入
- schedule_from_settings 三 phase（播报线）：morning/midday/evening 近 2-3 小时任务轻量鼓励
- --inbound（倾听线）：暂停/恢复关心自答；状态反馈转 agent（rc=3）

数据（加密落盘 common.crypto）：state.enc 调度状态；feedback.enc 用户反馈。
shared 仅明文标签摘要（emotion_user_state），权威数据在数据区。
表达口吻由部署用户 agent 人设决定，模块只提供素材与中性约束。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # 项目根：bridge（自持，裸 spawn 可跑）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))         # modules/：common
from common import (  # noqa: E402
    decrypt,
    encrypt,
    get_weather,
    load_token,
    log_event,
    post_push,
    probe_push_event,
    shared_load,
    shared_save,
)

MODULE_DIR = Path(__file__).resolve().parent
DATA_DIR = MODULE_DIR.parent / "modules_data" / MODULE_DIR.name  # modules/modules_data/emotion/
STATE_FILE = DATA_DIR / "state.enc"
FEEDBACK_FILE = DATA_DIR / "feedback.enc"
SETTINGS_FILE = DATA_DIR / "settings.json"
SHARED_STATE = "emotion_user_state"

DAY_END = 21                # 拟人线窗口结束时刻（暂停截止基准）
FEEDBACK_TTL = 48 * 3600    # 用户反馈有效期
FEEDBACK_KEEP = 50          # feedback 上限条数
FEEDBACK_INJECT_MAX = 3     # 素材注入条数上限
PHASE_WINDOW_H = 3          # 播报"近 N 小时"窗口
TASKS_MAX_AGE = 3600        # shared tasks 新鲜度（todo 每分钟 tick 刷新）
DEDUP_KEEP_DAYS = 2         # 防重键保留天数

DEFAULT_SETTINGS = {
    "emotion_on": True,
    "daily_limit": 4,
    "min_interval_hours": 3,
    "tasks_report_on": True,
    "report_morning": "09:00",
    "report_midday": "14:00",
    "report_evening": "18:00",
}

# 时段问候内置规则（拟人线话题；任务内容不在此——任务只走播报线）
PERIOD_CARE = {
    8: "早晨",
    11: "午间",
    14: "午后",
    17: "傍晚",
    20: "夜晚",
}

RAIN_WORDS = ("雨", "雷", "暴")

# 状态反馈分类词表（shared 只存标签，不存原文）
FEEDBACK_TAGS: list[tuple[str, tuple[str, ...]]] = [
    ("sick", ("感冒", "生病", "发烧", "头疼", "头晕", "难受", "不舒服", "嗓子疼", "肚子疼")),
    ("tired", ("累", "困", "疲惫", "加班", "熬夜", "失眠", "没精神")),
    ("busy", ("忙", "赶工", "赶稿", "赶due", "deadline", "连轴转")),
    ("sad", ("难过", "烦", "不开心", "委屈", "焦虑", "压力大", "心情不好")),
]
PREFERENCE_MARKS = ("以后别", "以后不要", "以后都", "别再", "别叫我", "别问", "不喜欢")

PAUSE_REPLY = "好的，不打扰你啦，需要我随时说～"
RESUME_REPLY = "好啦，我回来了～"

PHASE_LABEL = {"morning": "上午", "midday": "下午", "evening": "傍晚到收尾"}


# ---------- 加密状态 IO ----------

def _enc_load(path: Path, default):
    if not path.is_file():
        return default
    try:
        return json.loads(decrypt(path.read_text(encoding="utf-8")))
    except Exception as e:
        log_event("CRIT", "emotion", "decrypt_fail", f"{path.name}: {e}")
        return default


def _enc_save(path: Path, obj) -> bool:
    """用模块数据区的临时文件原子替换加密状态。"""
    tmp = None
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=DATA_DIR)
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encrypt(json.dumps(obj, ensure_ascii=False)))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log_event("ERROR", "emotion", "enc_save_fail", f"{path.name}: {e}")
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _default_state() -> dict:
    return {"pause_until": None, "daily_date": "", "daily_count": 0,
            "last_push_ts": None, "weather": None, "dedup": {}, "pending": {}}


def _load_state(now: datetime) -> dict:
    """读状态 + 例行维护：当日计数跨天归零、防重键只留最近 2 天。"""
    s = _enc_load(STATE_FILE, None)
    if not isinstance(s, dict):
        s = _default_state()
    today = now.date().isoformat()
    if s.get("daily_date") != today:
        s["daily_date"] = today
        s["daily_count"] = 0
    cutoff = (now.date() - timedelta(days=DEDUP_KEEP_DAYS - 1)).isoformat()
    s["dedup"] = {k: v for k, v in (s.get("dedup") or {}).items()
                  if isinstance(k, str) and k.split("|", 1)[0] >= cutoff}
    if not isinstance(s.get("pending"), dict):
        s["pending"] = {}
    cutoff_pending = (now.date() - timedelta(days=30)).isoformat()
    s["pending"] = {event_id: rec for event_id, rec in s["pending"].items()
                    if isinstance(rec, dict) and
                    (rec.get("status") not in {"sent", "failed", "unknown", "expired"}
                     or str(rec.get("business_date") or "") >= cutoff_pending)}
    return s


def _paused(state: dict, now: datetime) -> bool:
    p = state.get("pause_until")
    return isinstance(p, (int, float)) and time.time() < float(p)


def _settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.is_file():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                s.update(data)
        except Exception as e:
            log_event("WARN", "emotion", "settings_read_fail", str(e))
    return s


# ---------- 反馈沉淀（收口：未来记忆模块出现只改这里） ----------

def _record_feedback(text: str, tag: str) -> None:
    now = time.time()
    fb = _enc_load(FEEDBACK_FILE, [])
    if not isinstance(fb, list):
        fb = []
    fb.append({"ts": now, "text": text, "tag": tag, "expires_at": now + FEEDBACK_TTL})
    fb = [x for x in fb if isinstance(x, dict) and x.get("expires_at", 0) > now][-FEEDBACK_KEEP:]
    _enc_save(FEEDBACK_FILE, fb)
    shared_save(SHARED_STATE, {"status": tag, "expires_at": now + FEEDBACK_TTL})


def _recent_feedback(limit: int) -> list[dict]:
    now = time.time()
    fb = _enc_load(FEEDBACK_FILE, [])
    if not isinstance(fb, list):
        return []
    live = [x for x in fb if isinstance(x, dict) and x.get("expires_at", 0) > now]
    return live[-limit:]


# ---------- 天气 ----------

def _parse_weather(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"(-?\d+)\s*°C", text)
    if not m:
        return None
    return {"temp": int(m.group(1)), "rainy": any(k in text for k in RAIN_WORDS)}


def _weather_changed(last, now_text: str) -> bool:
    """突变：温差 ≥3°C 或晴雨翻转；无基线/解析失败不算突变。"""
    if not isinstance(last, dict) or last.get("temp") is None:
        return False
    cur = _parse_weather(now_text)
    if not cur:
        return False
    return abs(cur["temp"] - int(last["temp"])) >= 3 or cur["rainy"] != bool(last.get("rainy"))


# ---------- 素材 ----------

def _collect(now: datetime, state: dict) -> tuple[list[str], dict, dict]:
    """拟人线只汇集时段/天气/反馈事实；表达交给宿主当前人设。"""
    topics: list[str] = []
    wtext = get_weather()
    base = _parse_weather(wtext)
    period = None
    if now.hour in PERIOD_CARE:
        topics.append(f"时段关怀（{now.hour} 点）")
        period = PERIOD_CARE[now.hour]
    changed = _weather_changed(state.get("weather"), wtext)
    if changed:
        topics.append("天气变化")
    fb = _recent_feedback(FEEDBACK_INJECT_MAX)
    if fb:
        topics.append(f"用户近期状态 {len(fb)} 条")
    facts = {"period": now.hour, "evaluated_at": f"{now:%H:%M}",
             "weather": wtext or None, "weather_changed": changed,
             "previous_weather": state.get("weather") if changed else None,
             "period_label": period,
             "recent_feedback": [
                 {"said_at": datetime.fromtimestamp(float(x["ts"])).isoformat(timespec="minutes"),
                  "text": str(x.get("text") or "")[:500]} for x in fb],
             "topic_count": len(topics)}
    return topics, facts, base


# ---------- 播报线 ----------

def _task_trigger_hm(t: dict) -> str | None:
    """窗口筛选时刻：优先 reminder_time（含提前量，今日派生），回退 time。"""
    return t.get("reminder_time") or t.get("time") or None


def _near_tasks(tasks: list, now: datetime) -> list[dict]:
    win_end = now + timedelta(hours=PHASE_WINDOW_H)
    near = []
    for t in tasks:
        if not isinstance(t, dict) or t.get("done"):
            continue
        if t.get("reminder_date") and t.get("reminder_date") != now.date().isoformat():
            continue
        hm = _task_trigger_hm(t)
        if not hm:
            continue
        try:
            h, m = map(int, str(hm).split(":"))
            tt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        except (ValueError, TypeError):
            continue
        if now <= tt <= win_end:
            near.append({**t, "_tt": tt})
    near.sort(key=lambda x: x["_tt"])
    return near


def _phase_facts(phase: str, near: list[dict], now: datetime) -> dict:
    """阶段播报只传当前窗口事实；不抢 todo 的逐任务提醒职责。"""
    return {"phase": phase, "period": PHASE_LABEL[phase], "evaluated_at": f"{now:%H:%M}",
            "hours_ahead": PHASE_WINDOW_H, "task_count": len(near),
            "task_examples": [str(t.get("text") or "") for t in near[:3]]}


def _event_id(now: datetime, kind: str, slot: str) -> str:
    return f"emotion:{now:%Y-%m-%d}:{kind}:{slot}"


def _sync_pending(state: dict, now: datetime, token: str) -> bool:
    """查询状态后再更新限流/防重；只有确认 404 才首次投递冻结的事实。"""
    ok = True
    for event_id, record in list(state["pending"].items()):
        if record.get("status") in {"sent", "failed", "unknown", "expired"}:
            continue
        result = probe_push_event(event_id, token)
        if result["status"] == "unavailable":
            ok = False
            continue
        if result["status"] == "missing":
            if record["business_date"] != now.date().isoformat():
                record["status"] = "expired"
                if not _enc_save(STATE_FILE, state):
                    ok = False
                continue
            if not post_push(record["payload"], token):
                ok = False
            continue
        status = result["state"]
        if status not in {"sent", "failed", "unknown"}:
            continue
        record["status"] = status
        if status == "sent":
            state["dedup"][record["slot"]] = time.time()
            if record["kind"] == "care" and state["daily_date"] == record["business_date"]:
                state["daily_count"] += 1
            state["last_push_ts"] = time.time()
        if not _enc_save(STATE_FILE, state):
            ok = False
    return ok


def _reserve_and_send(state: dict, now: datetime, kind: str, slot: str,
                      facts: dict, intent: str) -> int:
    """先持久化不可变业务事件，再按状态提交；POST 200 不计入已发。"""
    event_id = _event_id(now, kind, slot.split("|", 1)[1])
    if event_id not in state["pending"]:
        body = {"type": "reminder", "event_id": event_id,
                "facts": facts, "intent": intent, "must_preserve": []}
        state["pending"][event_id] = {"slot": slot, "kind": kind,
                                       "business_date": now.date().isoformat(),
                                       "reserved_at": time.time(),
                                       "payload": body, "status": "pending"}
        if not _enc_save(STATE_FILE, state):
            return 1
    return 0 if _sync_pending(state, now, load_token(MODULE_DIR)) else 1


# ---------- 入口分支 ----------

def _window_run(dry: bool) -> int:
    now = datetime.now()
    state = _load_state(now)
    if _paused(state, now):
        print(f"[emotion] 暂停中（至 {state.get('pause_until')}），跳过")
        return 0
    s = _settings()
    if not s.get("emotion_on", True):
        return 0
    pending_care = sum(1 for record in state["pending"].values()
                       if record.get("kind") == "care" and record.get("business_date") == now.date().isoformat()
                       and record.get("status") == "pending")
    if state["daily_count"] + pending_care >= int(s.get("daily_limit", 4)):
        print(f"[emotion] 当日已发 {state['daily_count']} 条达上限，跳过")
        return 0
    if any(record.get("kind") == "care" and record.get("status") in {"pending", "unknown"}
           and time.time() - float(record.get("reserved_at") or 0) <
           int(s.get("min_interval_hours", 3)) * 3600
           for record in state["pending"].values()):
        print("[emotion] 近期关怀事件尚未确认或结果不确定，跳过新窗口")
        return 0
    if state.get("last_push_ts") and time.time() - float(state["last_push_ts"]) < int(s.get("min_interval_hours", 3)) * 3600:
        print("[emotion] 距上次推送不足最小间隔，跳过")
        return 0

    key = f"{now.date()}|{now.hour}"
    if key in state["dedup"] or any(record.get("slot") == key for record in state["pending"].values()):
        print(f"[emotion] 本小时已评估（{key}），跳过")
        return 0

    topics, facts, base = _collect(now, state)
    state["weather"] = base  # 评估后更新基线（不管发没发）

    if not topics:
        print(f"[emotion] 无话题，本小时不发（{key}）")
        if not dry:
            state["dedup"][key] = time.time()  # 无事件时记“已评估”；有事件时只在 sent 后记“已发”
            _enc_save(STATE_FILE, state)
        return 0
    if dry:
        print(f"[emotion][dry] 有话题（{key}）：{topics}")
        print(facts)
        return 0

    return _reserve_and_send(state, now, "care", key, facts,
                             "像朋友自然主动关心，短句为主；仅依据时段、天气和近期反馈，不复述内部事实字段。")


def _phase_run(phase: str | None, dry: bool) -> int:
    if phase not in PHASE_LABEL:
        log_event("WARN", "emotion", "bad_phase", str(phase))
        return 1
    now = datetime.now()
    state = _load_state(now)
    if _paused(state, now):
        print(f"[emotion] 暂停中，{phase} 播报跳过")
        return 0
    s = _settings()
    if not s.get("tasks_report_on", True):
        return 0
    key = f"{now.date()}|{phase}"
    if key in state["dedup"] or any(record.get("slot") == key for record in state["pending"].values()):
        print(f"[emotion] {phase} 今日已播，跳过")
        return 0

    tasks = (shared_load("tasks", max_age=TASKS_MAX_AGE) or {}).get("tasks") or []
    near = _near_tasks(tasks, now)
    if not near:
        print(f"[emotion] {phase} 窗口内无任务，跳过")
        if not dry:
            state["dedup"][key] = time.time()
            _enc_save(STATE_FILE, state)
        return 0
    facts = _phase_facts(phase, near, now)
    if dry:
        print(f"[emotion][dry] {phase} 播报 {len(near)} 条")
        print(facts)
        return 0
    return _reserve_and_send(state, now, "phase", key, facts,
                             "仅用近期任务数量与时段事实给轻量鼓励，不列清单；表达跟随部署人设。")


def _classify(text: str) -> tuple[str, str]:
    if "暂停关心" in text or ("暂停" in text and "关心" in text):
        return "pause", ""
    if "恢复关心" in text or ("恢复" in text and "关心" in text):
        return "resume", ""
    if any(m in text for m in PREFERENCE_MARKS):
        return "preference", ""
    for tag, words in FEEDBACK_TAGS:
        if any(w in text for w in words):
            return "feedback", tag
    return "unknown", ""


def _inbound(text: str, dry: bool = False) -> int:
    kind, tag = _classify(text)
    if dry:
        print(f"[emotion][dry] inbound 分类: {kind}（{tag or '-'}）")
        return 0
    if kind == "pause":
        state = _load_state(datetime.now())
        end = datetime.now().replace(hour=DAY_END, minute=0, second=0, microsecond=0)
        state["pause_until"] = end.timestamp()  # 21 点后写入自然过期 = 忽略，无特判
        _enc_save(STATE_FILE, state)
        log_event("INFO", "emotion", "paused", f"until {end:%Y-%m-%d %H:%M}")
        print(PAUSE_REPLY)
        return 0
    if kind == "resume":
        state = _load_state(datetime.now())
        state["pause_until"] = None
        _enc_save(STATE_FILE, state)
        log_event("INFO", "emotion", "resumed", "")
        print(RESUME_REPLY)
        return 0
    if kind == "feedback":
        _record_feedback(text, tag)
    # feedback / preference / unknown 全部转 agent（人设回应；长期偏好由人设消化）
    return 3


def _inspect() -> int:
    now = datetime.now()
    state = _load_state(now)
    pause = state.get("pause_until")
    print("state:")
    print(f"  pause_until: {datetime.fromtimestamp(pause) if pause else '无'}")
    print(f"  daily: {state['daily_date']} 已发 {state['daily_count']} 条")
    print(f"  last_push: {datetime.fromtimestamp(state['last_push_ts']) if state.get('last_push_ts') else '无'}")
    print(f"  weather 基线: {state.get('weather')}")
    print(f"  dedup: {len(state['dedup'])} 键")
    fb = _enc_load(FEEDBACK_FILE, [])
    print(f"feedback: {len(fb)} 条")
    for x in (fb if isinstance(fb, list) else [])[-10:]:
        print(f"  {datetime.fromtimestamp(float(x['ts'])):%m-%d %H:%M} [{x.get('tag')}] {x.get('text')}")
    return 0


def _unpause() -> int:
    state = _load_state(datetime.now())
    state["pause_until"] = None
    _enc_save(STATE_FILE, state)
    print("[emotion] 已清除暂停状态")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    dry = "--dry-run" in argv
    if "--reconcile" in argv:
        if dry:
            return 0
        now = datetime.now()
        state = _load_state(now)
        if not state["pending"]:
            return 0
        return 0 if _sync_pending(state, now, load_token(MODULE_DIR)) else 1
    if "--inspect" in argv:
        return _inspect()
    if "--unpause" in argv:
        return _unpause()
    if "--inbound" in argv:
        i = argv.index("--inbound")
        text = argv[i + 1] if i + 1 < len(argv) else ""
        return _inbound(text, dry)
    if "--phase" in argv:
        i = argv.index("--phase")
        return _phase_run(argv[i + 1] if i + 1 < len(argv) else None, dry)
    return _window_run(dry)


if __name__ == "__main__":
    raise SystemExit(main())
