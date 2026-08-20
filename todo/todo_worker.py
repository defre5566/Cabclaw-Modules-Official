"""todo 模块：待办提醒（internal JSON / Obsidian vault 双数据源，共享层同 schema）。

调度：bridge scheduler 按 module.json every 1m 触发；失败 rc=1 让 scheduler 感知（retry 60s×0 记日志下周期再试）。
铁律：--dry-run 零副作用（不推送/不写 sent/不刷 shared）；跨模块数据只走 common.shared。
"""
from __future__ import annotations

import calendar
import hashlib
import json
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (  # noqa: E402
    load_sent_json,
    save_sent_json,
    prune_state_file,
    shared_save,
    load_token,
    post_push,
    log_event,
)
from task import scan_md_tasks  # noqa: E402  # Obsidian 私有解析器（模块自带）
from bridge.config import resolve_path  # noqa: E402

MODULE_DIR = Path(__file__).resolve().parent          # modules/<name>/（代码）
DATA_DIR = MODULE_DIR.parent / "modules_data" / "todo"  # modules/modules_data/<name>/（用户数据）
SENT_FILE = DATA_DIR / "todo_sent.json"
TASKS_DIR = DATA_DIR / "tasks"
SETTINGS_FILE = DATA_DIR / "settings.json"
SHARED_NAME = "tasks"

DEFAULT_TAGS = ["工作", "学习", "生活", "家庭", "购物", "健康", "娱乐"]
DEFAULT_SETTINGS = {
    "data_source": "internal",
    "vault_path": "",
    "tags_vocab": DEFAULT_TAGS,
    "allow_new_tag": False,
    "extract_tags": True,
    "tag_prefix": "",
}


def _settings() -> dict:
    """读数据区 settings.json（用户配置，缺键兜底默认）；module.json 只存声明不存值。"""
    s = dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            s.update(data)
    except Exception:
        pass
    return s


# ---------- internal 数据源（自包含 JSON） ----------

def _norm_task(t: dict) -> dict | None:
    """补默认字段；缺 id/text/due 视为非法跳过。"""
    if not isinstance(t, dict):
        return None
    if not t.get("id") or not t.get("text") or not t.get("due"):
        return None
    return {
        "id": str(t["id"]),
        "text": str(t["text"]),
        "due": str(t["due"]),
        "time": t.get("time") or None,
        "remind_min": t.get("remind_min"),
        "done": bool(t.get("done", False)),
        "done_at": t.get("done_at") or None,   # 完成时间戳（ISO "YYYY-MM-DDTHH:MM:SS"；旧数据 None）
        "repeat": t.get("repeat"),
        "done_dates": list(t.get("done_dates") or []),
        "tags": list(t.get("tags") or []),
    }


def load_internal() -> list[dict]:
    """全扫 tasks/*.json（防跨月漏），按 id 去重合并；坏 JSON 跳过 + 告警（读容错）。"""
    tasks: list[dict] = []
    seen: set[str] = set()
    if not TASKS_DIR.is_dir():
        return tasks
    for path in sorted(TASKS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log_event("WARN", "todo", "bad_json", f"{path.name} 解析失败（可能被并发写或损坏），本次跳过")
            continue
        if isinstance(data, dict):
            data = data.get("tasks", [])
        if not isinstance(data, list):
            continue
        for item in data:
            t = _norm_task(item)
            if t is None or t["id"] in seen:
                continue
            seen.add(t["id"])
            tasks.append(t)
    return tasks


# ---------- vault 数据源（复用 common/task.py 解析器，零重写） ----------

def _from_parsed(pt) -> dict:
    """ParsedTask → 统一任务 dict（与 internal 同 schema；id = sha1(due|text)[:8] 跨源稳定）。"""
    due = pt.due or date.today()
    return {
        "id": hashlib.sha1(f"{due}|{pt.text}".encode()).hexdigest()[:8],
        "text": pt.text,
        "due": due.isoformat(),
        "time": pt.time.strftime("%H:%M") if pt.time else None,
        "remind_min": pt.remind_min,
        "done": bool(pt.done_date),
        "done_at": pt.done_date.isoformat() if pt.done_date else None,  # vault 只有日期粒度
        "repeat": None,
        "done_dates": [pt.done_date.isoformat()] if pt.done_date else [],
        "tags": list(pt.tags or []),
    }


def load_vault(settings: dict) -> list[dict]:
    """vault 模式：扫描 vault_path 下所有 .md 的 Tasks 语法行；带日期（📅）才算 todo。

    tag_prefix：设置的前缀（含 #，如 "#todo/"）；留空 = 不提取（无前缀标签不加）。
    """
    raw = (settings.get("vault_path") or "").strip()
    if not raw:
        log_event("WARN", "todo", "vault_path_missing", "data_source=vault 但未配置 vault_path")
        return []
    vault = resolve_path(raw)
    if not vault.is_dir():
        log_event("WARN", "todo", "vault_path_invalid", f"目录不存在: {vault}")
        return []
    prefix = settings.get("tag_prefix") or ""
    extract = bool(settings.get("extract_tags", True))
    tasks: list[dict] = []
    seen: set[str] = set()
    for pt in scan_md_tasks(vault, "*.md", tag_prefix=prefix if extract else ""):
        if pt.due is None:  # 带日期才算 todo
            continue
        t = _from_parsed(pt)
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        tasks.append(t)
    return tasks


def load_tasks(settings: dict) -> list[dict]:
    """单数据源：data_source == vault → load_vault；否则 internal（默认）。"""
    if settings.get("data_source") == "vault":
        return load_vault(settings)
    return load_internal()


# ---------- repeat 机器展开 ----------

def _next_due(d: date, freq: str, interval: int) -> date:
    """daily=+interval 天 / weekly=+interval 周 / monthly=月推进（月末按 calendar 收敛）。"""
    interval = max(1, int(interval or 1))
    if freq == "daily":
        return d + timedelta(days=interval)
    if freq == "weekly":
        return d + timedelta(weeks=interval)
    total = d.year * 12 + (d.month - 1) + interval
    y, m = divmod(total, 12)
    m += 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def repeat_due(t: dict, today: date) -> date:
    """无 repeat → 返回 due。有 repeat：从 due 推进取 <=today 最近到期日。

    until 早于 due → 仅 due 当天；guard 防死循环；越过 until 回退原 due（不再提醒）。
    """
    due = date.fromisoformat(t["due"])
    rp = t.get("repeat")
    if not rp or not isinstance(rp, dict):
        return due
    try:
        until = date.fromisoformat(rp["until"]) if rp.get("until") else None
    except (ValueError, TypeError):
        until = None
    if until is not None and until < due:
        return due
    freq = rp.get("freq", "daily")
    interval = int(rp.get("interval", 1) or 1)
    cur = due
    guard = 0
    while cur < today and guard < 1000:
        nxt = _next_due(cur, freq, interval)
        if until is not None and nxt > until:
            break
        cur = nxt
        guard += 1
    return cur


# ---------- 提醒计算 ----------

def _trigger_time(t: dict) -> dtime | None:
    """触发时刻 = time - remind_min；无 time → None（不入提醒，仅进共享层供查询）。"""
    if not t.get("time"):
        return None
    try:
        h, m = map(int, str(t["time"]).split(":"))
        minutes = h * 60 + m - int(t.get("remind_min") or 0)
        minutes %= 1440
        return dtime(minutes // 60, minutes % 60)
    except (ValueError, TypeError):
        return None


def compute_reminders(tasks: list[dict], now: datetime, sent: dict) -> list[tuple[str, list[dict]]]:
    """到期判定 → 按触发时刻分组合并，返回 [(HH:MM, [tasks])] 升序。"""
    today = now.date()
    groups: dict[str, list[dict]] = {}
    for t in tasks:
        if repeat_due(t, today) != today:
            continue
        if t.get("repeat") and isinstance(t.get("repeat"), dict):
            if today.isoformat() in (t.get("done_dates") or []):
                continue  # 重复任务：今天已完成
        elif t.get("done"):
            continue
        trig = _trigger_time(t)
        if trig is None:
            continue  # 无 time 不入提醒（避免全天任务深夜打扰）
        if trig > now.time():
            continue  # 未到点
        if f"{today}|{t['id']}" in sent:
            continue  # 防重键 = 日期|id
        groups.setdefault(trig.strftime("%H:%M"), []).append(t)
    return sorted(groups.items())


# ---------- 共享刷新 ----------

def refresh_shared(tasks: list[dict]) -> bool:
    """shared/tasks.json = {ts, tasks:[全量]}；ts 由 shared_save 自动带（原子写）。"""
    return shared_save(SHARED_NAME, {"tasks": tasks})


# ---------- 入口 ----------

def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    dry = "--dry-run" in argv
    s = _settings()
    tasks = load_tasks(s)
    sent = load_sent_json(SENT_FILE)
    now = datetime.now()

    groups = compute_reminders(tasks, now, sent)
    if dry:
        print(f"[todo][dry] 扫描任务 {len(tasks)} 条，待提醒 {sum(len(g) for _, g in groups)} 条")
        for trig, items in groups:
            print(f"[todo][dry] ⏰ {trig}：" + "；".join(t["text"] for t in items))
        return 0  # dry 零副作用：不推送/不写 sent/不刷 shared/不 prune

    try:
        if groups:
            text = "📌 待办提醒\n" + "\n".join(
                f"⏰ {trig}：" + "；".join(t["text"] for t in items) for trig, items in groups
            )
            ok = post_push({"type": "reminder", "text": text}, load_token(MODULE_DIR))
            if not ok:
                log_event("WARN", "todo", "push_fail", "推送失败（未记防重，下次 tick 重试）")
                return 1  # rc=1 → scheduler 感知（retry 60s×0 → 记日志下周期再试）
            for _trig, items in groups:
                for t in items:
                    sent[f"{now.date()}|{t['id']}"] = now.strftime("%Y-%m-%d %H:%M:%S")
            save_sent_json(SENT_FILE, sent)
        prune_state_file(SENT_FILE)
        refresh_shared(tasks)  # 每次运行刷新共享层（含无提醒时）
        return 0
    except Exception as e:
        log_event("ERROR", "todo", "worker_error", str(e))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
