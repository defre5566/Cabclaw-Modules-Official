"""Planner 模块：早晚报聚合（晨间早报 + 晚间复盘）。

调度：bridge scheduler 按 module.json 的 schedule（schedule_from_settings 联动生成）spawn，
--phase morning|evening 区分阶段；失败 rc=1（scheduler 按 retry 配置补发）；--dry-run 零副作用。

链路：worker 拼信息条目稿（零指令：事实/要点/信息性短语） → post_push(reminder)
→ push_render 单轮渲染（tier 人设语气润色，禁工具、不增不漏） → 用户。
agents.md 只服务入站交互（倒计时维护/查任务），不在推送链路上；素材零指令是硬约束
（渲染器不执行加工指令，指令会被忽略或原样念出）。

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
    get_weather_snapshot,
    weather_alerts,
    get_lunar,
    get_fufu,
    get_jiujiu,
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


def _settings() -> tuple[dict, bool]:
    """读数据区 settings.json；不存在=首次安装正常（默认值）；存在但坏=corrupt=True（早报内附提示，P8）。"""
    s = dict(DEFAULT_SETTINGS)
    f = DATA_DIR / "settings.json"
    if not f.is_file():
        return s, False
    try:
        import json
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            s.update(data)
        return s, False
    except Exception as e:
        log_event("WARN", "Planner", "settings_read_fail", f"{f}: {e}")
        return s, True


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
    except Exception as e:
        log_event("WARN", "Planner", "countdown_read_fail", f"{COUNTDOWN_FILE}: {e}")
        return []


def _save_countdown(entries: list[dict]) -> bool:
    try:
        import json
        COUNTDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = COUNTDOWN_FILE.with_name(COUNTDOWN_FILE.name + ".tmp")
        tmp.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(COUNTDOWN_FILE)
        return True
    except Exception as e:
        log_event("WARN", "Planner", "countdown_save_fail", f"{COUNTDOWN_FILE}: {e}")
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

def _today_briefing(today: date) -> Path | None:
    """今天的简报 HTML（文件名 YYYY-MM-DD.html 精确匹配，防跨天误用旧产物）。"""
    p = BRIEFING_DIR / f"{today.isoformat()}.html"
    return p if p.is_file() else None


def _job_diagnosis() -> tuple[bool, str] | None:
    """简报 job 登记诊断（bridge.jobs.job_registered 三态）；bridge 环境异常 → None（诊断不可用，不阻塞早报）。"""
    try:
        from bridge.jobs import job_registered
        return job_registered("Planner")
    except Exception as e:
        log_event("WARN", "Planner", "job_diag_fail", str(e))
        return None


def _push_file_with_retry(path: Path, token: str) -> bool:
    """简报原件 file 推送：重试 3 次（间隔 3s），全失败 False。"""
    for attempt in range(3):
        if post_push({"type": "file", "path": str(path)}, token):
            return True
        if attempt < 2:
            time.sleep(3)
    log_event("WARN", "Planner", "file_push_fail", f"重试 3 次失败: {path}")
    return False


def prune_briefing() -> None:
    """简报清理（规范.md L52）：>5 每5天清最旧5个；≤5（且>3）清3天前；≤3 不清。"""
    if not BRIEFING_DIR.is_dir():
        return
    try:
        files = sorted(BRIEFING_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime)
    except Exception:
        return
    n = len(files)
    now_ts = time.time()
    if n > 5:
        ts_file = BRIEFING_DIR / ".prune_ts"
        last = 0.0
        try:
            last = float(ts_file.read_text(encoding="utf-8").strip())
        except Exception:
            pass
        if now_ts - last >= 5 * 86400:  # 每 5 天清一次最旧 5 个
            for f in files[:5]:
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                ts_file.write_text(str(now_ts), encoding="utf-8")
            except Exception:
                pass
    elif n > 3:
        cutoff = now_ts - 3 * 86400  # ≤5 且 >3：清 3 天前
        for f in files:
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    # ≤3 不清


# ---------- 天气/节假日/地方数据 ----------

def _weather_section() -> list[str]:
    """天气 + 预警（带处理建议）。"""
    lines = [f"今天天气：{get_weather()}"]
    for alert in weather_alerts():
        name = alert[:-2] if alert.endswith("预警") else alert
        advice = ALERT_ADVICE.get(name)
        lines.append(f"⚠️ {alert}" + (f"（{advice}）" if advice else ""))
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

EVENING_TIPS = [
    "今晚早点休息，睡前少看手机。",
    "睡前用热水泡个脚，放松一天。",
    "抽 20 分钟读会儿书再睡。",
    "做几分钟伸展，缓解一天的疲劳。",
    "把明天的事列个清单，睡得更踏实。",
    "睡前开窗通通风，房间换换气。",
]

TEMP_SWING_THRESHOLD = 8  # 当前与未来数小时温差 ≥8°C 触发提醒


def _address() -> str:
    """用户称呼（web 人设页『怎么称呼你』，真源 .config/agent/identity.json）；未配置/异常返回空串。"""
    try:
        import json
        from bridge.config import WORK_ROOT
        data = json.loads((WORK_ROOT / ".config" / "agent" / "identity.json").read_text(encoding="utf-8"))
        return str(data.get("address") or "").strip()
    except Exception:
        return ""


def _greeting_head(today: date) -> str:
    """早报问候 + 历法行（星期/农历/节气/节假日/三伏数九，事实性）。"""
    addr = _address()
    head = f"{addr}，早上好呀！" if addr else "早上好呀！"
    head += f"今天是 {today.month} 月 {today.day} 日，星期{'一二三四五六日'[today.weekday()]}"
    lunar = get_lunar(today)
    if lunar.get("jieqi"):
        head += f"，今日节气：{lunar['jieqi']}"
    elif lunar.get("month") or lunar.get("day"):
        head += f"，农历{lunar.get('month') or ''}{lunar.get('day') or ''}"
    holiday = is_holiday(today)
    if holiday:
        head += f"，法定节假日：{holiday}"
    fufu = get_fufu(today)
    if fufu:
        head += f"，{'、'.join(fufu)}"
    jiujiu = get_jiujiu(today)
    if jiujiu:
        head += f"，{'、'.join(jiujiu)}"
    return head + "。"


def _evening_greeting() -> str:
    addr = _address()
    return f"{addr}，晚上好呀！" if addr else "晚上好呀！"


def _temp_swing_note() -> str | None:
    """温差提醒（事实性模板）：当前与未来数小时温差 ≥ 阈值给一句；快照不可得/温差小返回 None。"""
    try:
        snap = get_weather_snapshot()
    except Exception:
        return None
    if not snap.get("ok"):
        return None
    temps = [snap.get("current", {}).get("temperature")]
    temps += [p.get("temperature") for p in snap.get("hourly", []) if p.get("temperature") is not None]
    temps = [t for t in temps if t is not None]
    if len(temps) < 2:
        return None
    lo, hi = min(temps), max(temps)
    if hi - lo < TEMP_SWING_THRESHOLD:
        return None
    return f"今日温差较大（{lo}~{hi}°C），注意增减衣物。"


def _briefing_section(briefing: Path, resend_note: bool = False) -> str:
    """简报段（指示式：渲染 agent 读 HTML 挑要点，数量约束防挑多）。"""
    prefix = "信息简报（早报时段未送达，晚报补发）：请阅读 " if resend_note else "信息简报：请阅读 "
    return (f"{prefix}{briefing}，只挑 1-2 条最重要的要点，50 字左右概括"
            "（不要罗列多条），简报原文文件随后单独发送。")


def _tasks_stale() -> bool:
    """todo shared 层新鲜度：超 25h 未刷新视为过期（todo 每 1m 刷新）。"""
    data = shared_load("tasks")
    ts = data.get("ts") if isinstance(data, dict) else 0
    try:
        return not ts or time.time() - float(ts) > 25 * 3600
    except (TypeError, ValueError):
        return True


def fmt_overdue(tasks: list[dict]) -> str:
    """逾期原文行（text＋到期日一体，时间/标签空格分隔）。"""
    lines = []
    for t in tasks:
        text = str(t.get("text") or "")
        due = str(t.get("due") or "")
        if due:
            try:
                d = date.fromisoformat(due)
                text += f"（{d.month} 月 {d.day} 日到期）"
            except ValueError:
                text += f"（{due} 到期）"
        parts = [text]
        if t.get("time"):
            parts.append(f"⏰ {t['time']}")
        if t.get("tags"):
            parts.append(f"#{' #'.join(map(str, t['tags']))}")
        lines.append(f"- {' '.join(parts)}")
    return "\n".join(lines)


def _countdown_lines(countdown: list[dict], due_today_phrase: bool = True) -> str:
    """倒计时条目行（信息性短语）：当天「今天是X的时候了」，未到「X还有 N 天，该准备了」。"""
    lines = []
    for c in countdown:
        if c["days"] == 0:
            lines.append(f"今天是{c['name']}的时候了")
        else:
            tail = "，该准备了" if due_today_phrase else ""
            lines.append(f"{c['name']}还有 {c['days']} 天{tail}")
    return "；".join(lines)


def _closing_hint_morning(tasks: dict, countdown: list[dict], swing: str | None) -> str | None:
    """早报收尾提示（方向句，渲染层结合素材生成一句收尾；命中才拼，无事不硬凑）。
    优先级：逾期 > 倒计时当天 > 温差。"""
    if tasks["overdue"]:
        return "收尾提示：结合逾期任务提醒用户今天处理掉，语气轻松不施压"
    if any(c["days"] == 0 for c in countdown):
        return "收尾提示：结合今天的事项给用户一句打气的收尾"
    if swing:
        return "收尾提示：结合温差关照用户增减衣物"
    return None


def _closing_hint_evening(tasks: dict) -> str:
    """晚报收尾提示：按完成情况选方向。"""
    if tasks["today"]:
        return "收尾提示：温和提醒未完成项明天继续，不指责"
    if tasks["done_today"]:
        return "收尾提示：肯定用户今天的完成情况，轻松收尾"
    return "收尾提示：关照用户好好休息"

def morning(today: date, dry: bool) -> int:
    sent = load_sent_json(MORNING_SENT)
    if not dry and sent.get(today.isoformat()):
        return 0
    settings, settings_corrupt = _settings()

    tasks = collect_tasks(today)
    countdown = collect_countdown(today, prune=not dry)
    briefing_on = bool(settings.get("briefing_on"))
    briefing = _today_briefing(today) if briefing_on else None
    attempt_key = f"{today.isoformat()}|attempt"

    # 简报未就绪兜底（briefing_on 且今天无产物）：诊断 job 三态 → 等待(rc=1 走 retry) / 标注照发。
    # 等待次数上限取部署 retry.max（settings.json，缺省 3），推送失败与等待共享 retry 预算。
    briefing_note = ""
    if briefing_on and briefing is None:
        if dry:
            diag = _job_diagnosis()
            if diag is None:
                briefing_note = "（信息简报今日未生成：任务状态诊断不可用）"
            elif not diag[0]:
                briefing_note = f"（信息简报今日未生成：{diag[1]}；可在模块设置页重新保存以触发任务重登记）"
            else:
                briefing_note = "（信息简报今日未生成：简报任务已登记但未就绪，真跑将等待 retry 补发，超次后保底发送）"
        else:
            diag = _job_diagnosis()
            if diag is None:
                briefing_note = "（信息简报今日未生成：任务状态诊断不可用，已跳过简报段）"
            elif not diag[0]:
                briefing_note = f"（信息简报今日未生成：{diag[1]}；可在模块设置页重新保存以触发任务重登记）"
            else:
                wait_limit = int((settings.get("retry") or {}).get("max") or 3)
                waited = int(sent.get(attempt_key) or 0)
                if waited < wait_limit:
                    sent[attempt_key] = waited + 1
                    save_sent_json(MORNING_SENT, sent)
                    log_event("INFO", "Planner", "briefing_wait",
                              f"简报任务已登记但未就绪，等待 {waited + 1}/{wait_limit}（rc=1 走 retry 补发）")
                    return 1
                briefing_note = ("（信息简报今日未生成：任务已登记但等待多轮仍未就绪，"
                                 "可能是网络或检索故障，可在后台查看简报任务日志）")

    # 段0 运维提示（配置异常时）
    para0 = ["（系统提示：配置读取异常，本次按默认设置生成，请检查 settings.json）"] if settings_corrupt else []

    # 段1 开场：问候历法 + 天气预警 + 温差 + 倒计时
    para1 = [_greeting_head(today), *_weather_section()]
    swing = _temp_swing_note()
    if swing:
        para1.append(swing)
    if countdown:
        para1.append("倒计时：" + _countdown_lines(countdown) + "。")

    # 段2 任务：逾期 + 今日待办
    para2: list[str] = []
    if tasks["overdue"]:
        para2.append(f"已逾期 {len(tasks['overdue'])} 条，记得尽快处理：\n" + fmt_overdue(tasks["overdue"]))
    if tasks["today"]:
        para2.append(f"今天有 {len(tasks['today'])} 件事：\n" + fmt_tasks(tasks["today"]))
    else:
        para2.append("今天没有明确截止的待办。")
    if _tasks_stale():
        para2.append("（todo 数据未更新，以上任务可能不全）")

    # 段3 简报：要点指示 / 未生成说明
    para3: list[str] = []
    if briefing:
        para3.append(_briefing_section(briefing))
    elif briefing_note:
        para3.append(briefing_note)

    # 段4 收尾：收尾提示（末行，渲染层按其方向结合素材生成一句收尾，不播报该行）
    para4: list[str] = []
    hint = _closing_hint_morning(tasks, countdown, swing)
    if hint:
        para4.append(hint)

    text = "\n\n".join("\n".join(p) for p in (para0, para1, para2, para3, para4) if p)

    if dry:
        print(f"[Planner][dry] morning 素材:\n{text}")
        if briefing:
            print(f"[Planner][dry] 将发送简报文件: {briefing}")
        return 0

    token = load_token(MODULE_DIR)
    ok = post_push({"type": "reminder", "text": text}, token)

    # 简报原件 file 双发（重试 3 次）；仍失败 → 早报补一句说明，不记已发（待补发兜底）
    file_ok = True
    if briefing:
        file_ok = _push_file_with_retry(briefing, token)
        if not file_ok:
            post_push({"type": "reminder",
                       "text": "（简报原件发送失败，请稍后在管理后台查看）"}, token)

    if ok and file_ok:
        sent[today.isoformat()] = time.time()
        if briefing:
            sent[f"{today.isoformat()}|briefing"] = time.time()  # 晚报兜底判定标记：简报已随早报送达
        sent.pop(attempt_key, None)  # 等待计数使命完成
        save_sent_json(MORNING_SENT, sent)
        return 0

    log_event("WARN", "Planner", "morning_push_fail", f"text={ok} file={file_ok}（不记已发，待补发）")
    return 1


def evening(today: date, dry: bool) -> int:
    sent = load_sent_json(EVENING_SENT)
    if not dry and sent.get(today.isoformat()):
        return 0
    settings, settings_corrupt = _settings()

    tasks = collect_tasks(today)
    countdown = collect_countdown(today, prune=not dry)
    briefing_on = bool(settings.get("briefing_on"))

    # 晚报简报兜底：早报没带成简报（MORNING_SENT 无 <date>|briefing 标记）且今天的简报已生成
    # → 晚报补简报段 + 原件附发（简报最晚当天送达，不静默丢失）
    briefing = None
    if briefing_on:
        b = _today_briefing(today)
        if b is not None and not load_sent_json(MORNING_SENT).get(f"{today.isoformat()}|briefing"):
            briefing = b

    # 段0 运维提示（配置异常时）
    para0 = ["（系统提示：配置读取异常，本次按默认设置生成，请检查 settings.json）"] if settings_corrupt else []

    # 段1 问候
    para1 = [_evening_greeting()]

    # 段2 任务：今日完成 + 未完成 + 倒计时
    para2: list[str] = []
    if tasks["done_today"]:
        para2.append(f"今天完成 {len(tasks['done_today'])} 件：\n" + fmt_tasks(tasks["done_today"]))
    else:
        para2.append("今天还没有打勾完成的任务。")
    if tasks["today"]:
        para2.append(f"还有 {len(tasks['today'])} 件今日任务未完成：\n" + fmt_tasks(tasks["today"]))
    else:
        para2.append("今天到期的都办完了。")
    if countdown:
        para2.append("倒计时：" + _countdown_lines(countdown, due_today_phrase=False) + "。")

    # 段3 简报兜底（早报时段未送达，晚报补发）
    para3: list[str] = []
    if briefing:
        para3.append(_briefing_section(briefing, resend_note=True))

    # 段4 收尾：晚间建议（内置池轮换）+ 收尾提示（末行）
    para4 = [EVENING_TIPS[today.timetuple().tm_yday % len(EVENING_TIPS)]]
    para4.append(_closing_hint_evening(tasks))

    text = "\n\n".join("\n".join(p) for p in (para0, para1, para2, para3, para4) if p)

    if dry:
        print(f"[Planner][dry] evening 素材:\n{text}")
        if briefing:
            print(f"[Planner][dry] 将发送简报文件: {briefing}")
        return 0

    token = load_token(MODULE_DIR)
    ok = post_push({"type": "reminder", "text": text}, token)

    file_ok = True
    if briefing:
        file_ok = _push_file_with_retry(briefing, token)
        if not file_ok:
            post_push({"type": "reminder",
                       "text": "（简报原件发送失败，请稍后在管理后台查看）"}, token)

    if ok and file_ok:
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
        prune_briefing()

    if phase == "evening":
        return evening(today, dry)
    return morning(today, dry)


if __name__ == "__main__":
    raise SystemExit(main())
