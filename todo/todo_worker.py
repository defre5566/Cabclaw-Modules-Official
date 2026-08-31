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
    shared_load,
    load_token,
    post_push,
    log_event,
)
from task import scan_md_tasks, parse_task_line  # noqa: E402  # Obsidian 私有解析器（模块自带）
from bridge.config import resolve_path  # noqa: E402

MODULE_DIR = Path(__file__).resolve().parent          # modules/<name>/（代码）
DATA_DIR = MODULE_DIR.parent / "modules_data" / "todo"  # modules/modules_data/<name>/（用户数据）
SENT_FILE = DATA_DIR / "todo_sent.json"
TASKS_DIR = DATA_DIR / "tasks"
SETTINGS_FILE = DATA_DIR / "settings.json"
SCAN_CACHE_FILE = DATA_DIR / "scan_cache.json"
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


def _settings() -> dict | None:
    """读数据区 settings.json；文件不存在=用默认（首次安装正常）；文件存在但坏=返回 None（阻塞，T1）。"""
    s = dict(DEFAULT_SETTINGS)
    if not SETTINGS_FILE.is_file():
        return s  # 首次安装/未配置，用默认
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            s.update(data)
        return s
    except Exception as e:
        log_event("ERROR", "todo", "settings_corrupt", f"{SETTINGS_FILE}: {e}")
        return None  # 坏：返回 None 让 main 阻塞 + notification 提示


# ---------- internal 数据源（自包含 JSON） ----------

def _reminder_for_today(t: dict, today: date) -> tuple[str, str] | None:
    """算今日是否是 t 的提醒日。是则返回 (today_iso, reminder_time HH:MM)，否则 None。

    跨日(remind_min > time 分钟数)则今日提醒的是明日到期任务（提前量跨凌晨）。
    重复任务靠 repeat_due 判 target_due 是否到期日；单次直接比 due。
    T2 兜底：due/repeat 非法返回 None（不崩）。
    """
    time_str = t.get("time")
    if not time_str:
        return None  # 无 time 不提醒（仅进共享层供查询）
    try:
        h, m = map(int, str(time_str).split(":"))
    except (ValueError, TypeError):
        return None
    remind_min = int(t.get("remind_min") or 0)
    time_min = h * 60 + m
    crosses = remind_min > time_min
    target_due = today + timedelta(days=1 if crosses else 0)
    if t.get("repeat") and isinstance(t.get("repeat"), dict):
        try:
            if repeat_due(t, target_due) != target_due:
                return None  # target_due 非到期日
        except (ValueError, TypeError):
            return None  # repeat 结构坏
    else:
        try:
            if date.fromisoformat(str(t["due"])) != target_due:
                return None
        except (ValueError, TypeError):
            return None  # T2 兜底：非法 due
    rmin = (time_min - remind_min) % 1440
    return (today.isoformat(), f"{rmin // 60:02d}:{rmin % 60:02d}")


def _norm_task(t: dict, today: date) -> dict | None:
    """补默认字段 + 算 reminder_date/time；缺 text/due 或 due 格式非法跳过。

    id 兜底：缺 id 时自动补算 sha1(f"{due}|{text}")[:8]（与 vault 同式，跨源稳定），
    agent 直写可不生成 id；text/due 缺失仍跳过。
    """
    if not isinstance(t, dict):
        return None
    if not t.get("text") or not t.get("due"):
        return None
    try:
        due = str(t["due"])
        date.fromisoformat(due)
    except (ValueError, TypeError):
        log_event("WARN", "todo", "bad_due", f"{t.get('id')}: {t.get('due')}")
        return None
    tid = str(t["id"]) if t.get("id") else hashlib.sha1(f"{due}|{t['text']}".encode()).hexdigest()[:8]
    r = _reminder_for_today(t, today)
    return {
        "id": tid,
        "text": str(t["text"]),
        "due": due,
        "time": t.get("time") or None,
        "remind_min": t.get("remind_min"),
        "done": bool(t.get("done", False)),
        "done_at": t.get("done_at") or None,   # 完成时间戳（ISO "YYYY-MM-DDTHH:MM:SS"；旧数据 None）
        "repeat": t.get("repeat"),
        "done_dates": list(t.get("done_dates") or []),
        "tags": list(t.get("tags") or []),
        "reminder_date": r[0] if r else None,
        "reminder_time": r[1] if r else None,
    }


def load_internal(today: date) -> list[dict]:
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
            t = _norm_task(item, today)
            if t is None or t["id"] in seen:
                continue
            seen.add(t["id"])
            tasks.append(t)
    return tasks


# ---------- vault 数据源（复用 common/task.py 解析器，零重写） ----------

def _from_parsed(pt, today: date) -> dict:
    """ParsedTask → 统一任务 dict（与 internal 同 schema；id = sha1(due|text)[:8] 跨源稳定）。

    reminder_date/time 内存派生（vault .md 只读不写回）；done_dates 单次任务空（T10：与 internal 一致，完成状态靠 done/done_at）。
    """
    due = pt.due or date.today()
    t = {
        "id": hashlib.sha1(f"{due}|{pt.text}".encode()).hexdigest()[:8],
        "text": pt.text,
        "due": due.isoformat(),
        "time": pt.time.strftime("%H:%M") if pt.time else None,
        "remind_min": pt.remind_min,
        "done": bool(pt.done_date),
        "done_at": pt.done_date.isoformat() if pt.done_date else None,  # vault 只有日期粒度
        "repeat": None,
        "done_dates": [],  # T10：vault 单次任务 done_dates 空（与 internal 一致）
        "tags": list(pt.tags or []),
    }
    r = _reminder_for_today(t, today)
    t["reminder_date"] = r[0] if r else None
    t["reminder_time"] = r[1] if r else None
    return t


def load_vault(settings: dict, today: date, save_cache: bool = True) -> list[dict]:
    """vault 模式：扫描 vault_path 下所有 .md 的 Tasks 任务行；带日期（📅）才算 todo。

    增量优化：按文件 mtime 缓存 scan_cache.json，mtime 没变用缓存的解析结果（重算 reminder_date），
    变了才重新解析。缓存坏则全量重扫重建。tag_prefix：设置的前缀（含 #）；留空 = 不提取。
    save_cache=False（dry-run）只扫描不落盘缓存（零副作用）。
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
    tp = prefix if extract else ""

    cache = _load_scan_cache()
    files_cache = cache.get("files", {}) if isinstance(cache.get("files"), dict) else {}
    new_files_cache: dict = {}
    tasks: list[dict] = []
    seen: set[str] = set()

    for path in sorted(vault.glob("*.md")):
        abs_path = str(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cached = files_cache.get(abs_path)
        if cached and cached.get("mtime") == mtime:
            # mtime 没变：用缓存的 base fields，重算 reminder_date/time（today 可能变）
            for t_base in cached.get("tasks", []):
                t = dict(t_base)
                r = _reminder_for_today(t, today)
                t["reminder_date"] = r[0] if r else None
                t["reminder_time"] = r[1] if r else None
                if t["id"] in seen:
                    continue
                seen.add(t["id"])
                tasks.append(t)
            new_files_cache[abs_path] = cached
        else:
            # mtime 变了：重新解析该文件
            file_tasks: list[dict] = []
            for pt in _scan_file(path, tp):
                if pt.due is None:
                    continue
                t = _from_parsed(pt, today)
                if t["id"] in seen:
                    continue
                seen.add(t["id"])
                tasks.append(t)
                # 缓存 base fields（不含 reminder_date/time，每次重算）
                file_tasks.append({k: v for k, v in t.items() if k not in ("reminder_date", "reminder_time")})
            new_files_cache[abs_path] = {"mtime": mtime, "tasks": file_tasks}

    if save_cache:
        _save_scan_cache({"files": new_files_cache})
    return tasks


def _scan_file(path: Path, tag_prefix: str) -> list:
    """解析单个 .md 文件的任务行（跳过代码块/注释），返回 ParsedTask 列表。"""
    tasks: list = []
    in_code = False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return tasks
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if stripped.startswith("<!--"):
            continue
        pt = parse_task_line(line.strip(), path, tag_prefix=tag_prefix)
        if pt:
            tasks.append(pt)
    return tasks


def _load_scan_cache() -> dict:
    try:
        data = json.loads(SCAN_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_scan_cache(cache: dict) -> None:
    try:
        SCAN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SCAN_CACHE_FILE.with_name(SCAN_CACHE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SCAN_CACHE_FILE)
    except Exception:
        pass


def load_tasks(settings: dict, today: date, save_cache: bool = True) -> list[dict]:
    """单数据源：data_source == vault → load_vault；否则 internal（默认）。"""
    if settings.get("data_source") == "vault":
        return load_vault(settings, today, save_cache=save_cache)
    return load_internal(today)


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

def compute_reminders(tasks: list[dict], now: datetime, sent: dict) -> list[tuple[str, list[dict]]]:
    """提醒判定：查 reminder_date == today（加载时算好），按 reminder_time 分组合并。

    reminder_date/time 在 _norm_task/_from_parsed 加载时算好存 dict（跨日/重复统一处理，T11 修复）。
    """
    today = now.date()
    today_str = today.isoformat()
    groups: dict[str, list[dict]] = {}
    for t in tasks:
        if t.get("reminder_date") != today_str:
            continue  # 今日非提醒日
        rtime = t.get("reminder_time")
        if not rtime:
            continue
        # 完成判定
        if t.get("repeat") and isinstance(t.get("repeat"), dict):
            if today_str in (t.get("done_dates") or []):
                continue  # 重复任务：今天已完成
        elif t.get("done"):
            continue
        # 到点判定
        try:
            h, m = map(int, rtime.split(":"))
            if dtime(h, m) > now.time():
                continue  # 未到点
        except (ValueError, TypeError):
            continue
        # 防重键 = 提醒日|id
        if f"{today_str}|{t['id']}" in sent:
            continue
        groups.setdefault(rtime, []).append(t)
    return sorted(groups.items())


# ---------- 共享刷新 ----------

def refresh_shared(tasks: list[dict]) -> bool:
    """shared/tasks.json = {ts, tasks:[全量]}；tasks 内容变了才写（深比较，避免无用全量 I/O）。"""
    existing = shared_load(SHARED_NAME)
    old = existing.get("tasks") if isinstance(existing, dict) else None
    if old == tasks:
        return True  # 无变化，跳过 shared_save
    return shared_save(SHARED_NAME, {"tasks": tasks})


# ---------- 入口 ----------

def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    dry = "--dry-run" in argv
    now = datetime.now()
    today = now.date()
    sent = load_sent_json(SENT_FILE)
    s = _settings()
    if s is None:  # settings 坏 → 阻塞 + notification(原文) 提示 + 防刷屏（T1）；dry 零副作用只打印
        if dry:
            print("[todo][dry] settings.json 损坏（真跑将推 notification 并阻塞 rc=1）")
            return 0
        key = f"{today}|settings_corrupt"
        if key not in sent:
            post_push({"type": "notification",
                       "text": "（原文）todo 配置文件 settings.json 损坏，提醒功能已暂停，请检查或让 agent 修复"},
                      load_token(MODULE_DIR))
            sent[key] = now.strftime("%Y-%m-%d %H:%M:%S")
            save_sent_json(SENT_FILE, sent)
        return 1
    tasks = load_tasks(s, today, save_cache=not dry)

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
