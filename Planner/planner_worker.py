"""Planner 模块：早晚报聚合（晨间早报 + 晚间复盘）。

调度：bridge scheduler 按 module.json 的 schedule（schedule_from_settings 联动生成）spawn，
--phase morning|evening 区分阶段；失败 rc=1（scheduler 按 retry 配置补发）；--dry-run 零副作用。

链路：worker 拼"素材文本 + 组织指令" → post_push(reminder) → push_server agent 队列
→ agent 按 agents.md 组织成口语化文案 → 用户（话术跟随全局 AGENTS.md，本模块不定制）。

数据依赖（全部由数据可得性决定，非设置项）：
- 任务：todo 的 shared/tasks.json（读到就读，读不到就没有）
- 天气 + 气象预警：common.weather（location 驱动）
- 节假日/农历：common.calendar / holidays
- 花粉/台风：common.localdata（开关 + location 判定）
- 倒计时/纪念日：modules_data/Planner/countdown.json（agent 维护）
- 简报：modules_data/Planner/briefing/*.html（briefing_on + 有产物）
"""
from __future__ import annotations

import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (  # noqa: E402
    load_sent_json,
    save_sent_json,
    prune_state_file,
    shared_load,
    load_token,
    post_push,
    log_event,
    get_weather,
    weather_alerts,
    get_lunar,
    get_fufu,
    is_holiday,
    get_location,
)
from common import available as localdata_available  # noqa: E402
from common import fetch_localdata as localdata_fetch  # noqa: E402

MODULE_DIR = Path(__file__).resolve().parent          # modules/Planner/（代码）
DATA_DIR = MODULE_DIR.parent / "modules_data" / "Planner"  # 用户数据区
MORNING_SENT = DATA_DIR / "morning_sent.json"
EVENING_SENT = DATA_DIR / "evening_sent.json"
COUNTDOWN_FILE = DATA_DIR / "countdown.json"
BRIEFING_DIR = DATA_DIR / "briefing"

SENT_KEY = "date"  # 防重键 = 日期

# 预警 → 处理建议（早报天气段给 agent 参考；无建议的预警不附）
ALERT_ADVICE = {
    "大雨": "出门带伞",
    "暴雨": "减少外出，注意积水路段",
    "大雪": "注意保暖防滑",
    "雷暴": "减少户外活动",
    "雷暴伴冰雹": "减少外出，注意冰雹",
    "大冰雹": "避免外出，注意防护",
}

DEFAULT_SETTINGS = {
    "planner_on": True,
    "morning_time": "08:30",
    "evening_on": True,
    "evening_time": "21:00",
    "briefing_on": False,
    "briefing_topics": ["热点"],
    "pollen_on": False,
    "typhoon_on": False,
}


def _settings() -> dict:
    """读数据区 settings.json（缺键兜底默认）。"""
    s = dict(DEFAULT_SETTINGS)
    try:
        import json
        data = json.loads((DATA_DIR / "settings.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            s.update(data)
    except Exception:
        pass
    return s


# ---------- 任务数据（todo shared） ----------

def load_tasks() -> list[dict]:
    """读 todo 共享层 tasks.json；读不到返回 []（planner 是消费者）。"""
    data = shared_load("tasks")
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []


def _is_done(t: dict, today: date) -> bool:
    """完成判定：done_at 今天 / done_dates 含今天（重复任务）/ done（旧数据兜底）。"""
    if t.get("done_dates"):
        return today.isoformat() in t.get("done_dates") or []
    if t.get("done_at"):
        return str(t["done_at"]).startswith(today.isoformat())
    return bool(t.get("done"))


def _due_date(t: dict) -> date | None:
    try:
        return date.fromisoformat(str(t["due"])) if t.get("due") else None
    except (ValueError, TypeError):
        return None


def _task_time(t: dict) -> str:
    """排序键：有 time 用 time，无 time 排最后（23:59）。"""
    return str(t.get("time") or "23:59")


def collect_tasks(today: date) -> dict:
    """任务分组：today（今日待办）/ overdue（逾期）/ done_today（今日完成）。"""
    out = {"today": [], "overdue": [], "done_today": []}
    for t in load_tasks():
        due = _due_date(t)
        if _is_done(t, today):
            if (t.get("done_at") and str(t["done_at"]).startswith(today.isoformat()))                     or (t.get("done_dates") and today.isoformat() in t["done_dates"]):
                out["done_today"].append(t)
            continue
        if due is None:
            continue
        if due == today:
            out["today"].append(t)
        elif due < today:
            out["overdue"].append(t)
    out["today"].sort(key=_task_time)
    out["overdue"].sort(key=lambda t: (today - _due_date(t)).days, reverse=True)
    out["done_today"].sort(key=lambda t: str(t.get("done_at") or ""), reverse=True)
    return out


def fmt_tasks(tasks: list[dict]) -> str:
    """任务原文行（text + 时间/标签），agent 先列原文再给建议。"""
    lines = []
    for t in tasks:
        parts = [str(t.get("text") or "")]
        if t.get("time"):
            parts.append(f"⏰ {t['time']}")
        if t.get("tags"):
            parts.append(f"#{' #'.join(map(str, t['tags']))}")
        lines.append(f"- {' '.join(parts)}")
    return "\n".join(lines) if lines else "（无）"


# ---------- 倒计时/纪念日 ----------

def _load_countdown() -> list[dict]:
    try:
        import json
        data = json.loads(COUNTDOWN_FILE.read_text(encoding="utf-8"))
        return data.get("entries", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _save_countdown(entries: list[dict]) -> bool:
    try:
        import json
        COUNTDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = COUNTDOWN_FILE.with_name(COUNTDOWN_FILE.name + ".tmp")
        tmp.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(COUNTDOWN_FILE)
        return True
    except Exception:
        return False


def _next_repeat_date(d: date, today: date) -> date:
    """repeat（按年循环）的下一次日期：今天之前/当天 → 下一年。"""
    nxt = d.replace(year=today.year)
    if nxt < today:
        try:
            nxt = nxt.replace(year=today.year + 1)
        except ValueError:  # 2-29 非闰年 → 3-1
            nxt = nxt.replace(year=today.year + 1, month=3, day=1)
    return nxt


def collect_countdown(today: date, prune: bool) -> list[dict]:
    """倒计时段素材：[(名称, 距天数或0=今天)]；prune=True 时清理一次性超时 15 天条目。"""
    entries = _load_countdown()
    out: list[dict] = []
    keep: list[dict] = []
    changed = False
    for e in entries:
        try:
            d = date.fromisoformat(str(e["date"]))
        except (ValueError, TypeError):
            continue
        if e.get("repeat"):
            nxt = _next_repeat_date(d, today)
            keep.append(e)
            if (nxt - today).days <= 15:
                out.append({"name": str(e.get("name") or ""), "days": (nxt - today).days})
        else:
            if d >= today:
                keep.append(e)
                out.append({"name": str(e.get("name") or ""), "days": (d - today).days})
            elif (today - d).days > 15 and prune:
                changed = True  # 超时 15 天自动删除
            else:
                keep.append(e)
    if changed and prune:
        _save_countdown(keep)
    return sorted(out, key=lambda x: x["days"])


# ---------- 简报 ----------

def latest_briefing() -> Path | None:
    """最新简报 HTML（按 mtime）；无产物返回 None。"""
    try:
        if not BRIEFING_DIR.is_dir():
            return None
        files = [f for f in BRIEFING_DIR.glob("*.html")]
        return max(files, key=lambda p: p.stat().st_mtime) if files else None
    except Exception:
        return None


# ---------- 天气/节假日/地方数据 ----------

def _weather_section() -> list[str]:
    """天气 + 预警（带处理建议）。"""
    lines = [f"今天天气：{get_weather()}"]
    for alert in weather_alerts():
        name = alert[:-2] if alert.endswith("预警") else alert
        advice = ALERT_ADVICE.get(name)
        lines.append(f"⚠️ {alert}" + (f"（{advice}）" if advice else ""))
    return lines


def _calendar_section(today: date) -> list[str]:
    """节假日/农历/节气/三伏。"""
    lines: list[str] = []
    holiday = is_holiday(today)
    if holiday:
        lines.append(f"今天是法定节假日：{holiday}")
    lunar = get_lunar(today)
    if lunar.get("jieqi"):
        lines.append(f"今日节气：{lunar['jieqi']}")
    else:
        lines.append(f"农历：{lunar.get('month') or ''}{lunar.get('day') or ''}")
    if get_fufu(today):
        lines.append(f"当前三伏：{'、'.join(get_fufu(today))}")
    return lines


def _localdata_section(settings: dict) -> list[str]:
    """花粉/台风（开关 + location 判定）。"""
    lines: list[str] = []
    loc = get_location()
    services = localdata_available(loc)
    if settings.get("pollen_on") and "pollen" in services:
        data = localdata_fetch(loc, "pollen") or {}
        p = data.get("pollen")
        if p:
            lines.append(f"花粉：{p.get('level')}（{p.get('detail')}）")
    if settings.get("typhoon_on") and "typhoon" in services:
        data = localdata_fetch(loc, "typhoon") or {}
        t = data.get("typhoon")
        if t and t.get("active"):
            names = "、".join(f"{x.get('name')}（{x.get('grade') or '强度未知'}）" for x in t.get("list", []))
            lines.append(f"台风动态：{names}")
    return lines


# ---------- 素材拼装 ----------

def morning(today: date, dry: bool) -> int:
    sent = load_sent_json(MORNING_SENT)
    if sent.get(today.isoformat()):
        return 0
    settings = _settings()

    tasks = collect_tasks(today)
    countdown = collect_countdown(today, prune=not dry)
    briefing = latest_briefing() if settings.get("briefing_on") else None

    material: list[str] = ["【晨间早报素材】"]

    # 1. 天气 + 预警
    material.extend(_weather_section())

    # 2. 节假日/农历
    cal = _calendar_section(today)
    if cal:
        material.append("；".join(cal))

    # 3. 倒计时/纪念日
    if countdown:
        lines = [f"{c['name']}" + ("就是今天" if c["days"] == 0 else f"还有 {c['days']} 天") for c in countdown]
        material.append("倒计时/纪念日：" + "；".join(lines))

    # 4. 逾期（提前，催处理）
    if tasks["overdue"]:
        material.append(
            f"已逾期 {len(tasks['overdue'])} 条（原文如下，全部列出一条不漏，请尽快处理）：\n"
            + fmt_tasks(tasks["overdue"])
        )

    # 5. 今日待办
    material.append(
        "今日待办（原文如下，按时间先后、优先级）：\n"
        + (fmt_tasks(tasks["today"]) if tasks["today"] else "（今天没有明确截止的待办）")
    )

    # 6. 简报要点（附原文文件）
    if briefing:
        material.append(
            f"信息简报：请阅读 {briefing} 挑热度高/影响大的要点说几句（不要二次摘要、不要代做判断），"
            "简报原文文件随后单独发送。"
        )

    # 7. 收尾 + 组织指令
    material.append(
        "请按早报风格组织成一条口语化消息：问候 + 天气（带穿衣/带伞等提醒）"
        + (" + 节假日/农历" if cal else "")
        + (" + 倒计时/纪念日（该准备了）" if countdown else "")
        + (" + 逾期（一条不漏全部列出，催尽快处理）" if tasks["overdue"] else "")
        + " + 今日待办（先列原文，再说建议）"
        + (" + 简报要点" if briefing else "")
        + " + 一句鼓励。要求：多用 emoji 让消息活泼不生硬；话术自由发挥，自然连贯，不要逐段罗列。"
    )
    text = "\n".join(material)

    if dry:
        print(f"[Planner][dry] morning 素材:\n{text}")
        if briefing:
            print(f"[Planner][dry] 将发送简报文件: {briefing}")
        return 0

    token = load_token(MODULE_DIR)
    ok = post_push({"type": "reminder", "text": text}, token)

    # 简报原件 file 双发：重试 3 次；仍失败 → 早报补一句说明，不记已发（次日补发兜底）
    file_ok = True
    if briefing:
        for attempt in range(3):
            if post_push({"type": "file", "path": str(briefing)}, token):
                break
            if attempt < 2:
                time.sleep(3)
        else:
            file_ok = False
            log_event("WARN", "Planner", "file_push_fail", f"重试 3 次失败: {briefing}")
            post_push({"type": "reminder",
                       "text": f"（简报原件发送失败，请稍后查看 {briefing}）"}, token)

    if ok and file_ok:
        sent[today.isoformat()] = time.time()
        save_sent_json(MORNING_SENT, sent)
        return 0

    log_event("WARN", "Planner", "morning_push_fail", f"text={ok} file={file_ok}（不记已发，待补发）")
    return 1


def evening(today: date, dry: bool) -> int:
    sent = load_sent_json(EVENING_SENT)
    if sent.get(today.isoformat()):
        return 0
    settings = _settings()

    tasks = collect_tasks(today)
    countdown = collect_countdown(today, prune=not dry)

    material: list[str] = ["【晚间复盘素材】"]
    material.append("今日已完成（原文如下）：\n" + (fmt_tasks(tasks["done_today"]) if tasks["done_today"] else "（今天还没有打勾完成的任务）"))
    material.append("今日未完成（原文如下）：\n" + (fmt_tasks(tasks["today"]) if tasks["today"] else "（今天到期的都办完了）"))

    if countdown:
        lines = [f"{c['name']}" + ("就是今天" if c["days"] == 0 else f"还有 {c['days']} 天") for c in countdown]
        material.append("提醒：明天前要准备的倒计时/纪念日：" + "；".join(lines))

    material.append(
        "请按晚间风格组织成一条口语化复盘：问候 + 今日完成情况（先列原文，再说建议）+ 未完成情况（先列原文）"
        + " + 按完成度给鼓励或提醒"
        + " + 晚间建议（通用化指引：如早睡、泡脚、读 30 分钟书，不写具体私人化例子）。"
        "要求：多用 emoji 让消息活泼不生硬；话术自由发挥。"
    )
    text = "\n".join(material)

    if dry:
        print(f"[Planner][dry] evening 素材:\n{text}")
        return 0

    if post_push({"type": "reminder", "text": text}, load_token(MODULE_DIR)):
        sent[today.isoformat()] = time.time()
        save_sent_json(EVENING_SENT, sent)
        return 0

    log_event("WARN", "Planner", "evening_push_fail", "不记已发，待补发")
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    dry = "--dry-run" in argv
    phase = None
    if "--phase" in argv:
        i = argv.index("--phase")
        if i + 1 < len(argv):
            phase = argv[i + 1]
    today = date.today()
    if not dry:
        prune_state_file(MORNING_SENT)
        prune_state_file(EVENING_SENT)

    if phase == "evening":
        return evening(today, dry)
    return morning(today, dry)


if __name__ == "__main__":
    raise SystemExit(main())
