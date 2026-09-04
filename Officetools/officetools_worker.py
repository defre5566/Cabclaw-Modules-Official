"""Officetools 模块：文档解读能力（pdf/office/图片 → 概览 + 全文 markdown 产物）。

交互（inbound，方案 v3 §五）：
- 微信发文件 → bridge 落 inbox 并在消息流插入 "[收到file: <名>，已存 <路径>]"
  → 模块记录 state.json 后 rc=3 交还 agent（agent 感知完整）；file_auto_on=true 时
  模块直接接管：解析 → 概览回微信。
- 用户说 "解读"（可点名 "解读 文件名.pdf"）→ 定位文件 → 解析 → 概览回微信（rc=0）。
- 后续内容问题由 agent 读产物承接（agents.md + index.json 硬索引链路），模块不参与。

rc 语义（定稿 B）：0=自答（stdout 回微信）/ 3=转 agent / 1=业务失败 / 2=引擎级。
守护：every 5m --bootstrap-check 预热依赖（就绪 <1s 秒退）；--dry-run 零副作用；
--inspect 零副作用自检（依赖就绪/最新产物）。
"""
from __future__ import annotations

import fcntl
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # 项目根：bridge（自持，裸 spawn 可跑）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))         # modules/：common
from common import load_json, load_sent_json, log_event, save_sent_json, shared_save  # noqa: E402

import bootstrap  # noqa: E402
import parsers  # noqa: E402

MODULE_DIR = Path(__file__).resolve().parent
DATA_DIR = MODULE_DIR.parent / "modules_data" / "Officetools"
OUTPUTS_DIR = DATA_DIR / "outputs"
STATE_FILE = DATA_DIR / "state.json"
LOCK_FILE = DATA_DIR / ".parse.lock"

MODULE_NAME = "Officetools"
FRESH_WINDOW_HOURS = 24          # state 最新文件未解读的新鲜窗口
DEFAULT_SETTINGS = {
    "ocr_on": False,
    "file_auto_on": False,
    "max_file_mb": 200,
    "max_pages": 300,
    "ocr_max_pages": 20,
    "overview_max_chars": 800,
    "retention_days": 7,
}

# bridge 媒体提示形如 "[收到file: xxx.pdf，已存 /path/inbox/xxx.pdf]"
_MEDIA_RE = re.compile(r"\[收到\w+: (?P<name>[^\]]*?)?，?已存 (?P<path>[^\]]+)\]")
_INBOX_HINT_RE = re.compile(r"已存 (?P<path>[^\]\s]+)")


def _settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(load_json(DATA_DIR / "settings.json", {}) or {})
    return s


def _ocr_ready(s: dict) -> bool:
    if not s.get("ocr_on"):
        return False
    ok, _ = bootstrap.ensure_ocr()
    return ok


# ---------- S1 路径校验 ----------

def inbox_dir(settings: dict | None = None) -> Path:
    """inbox 真源：OFFICETOOLS_INBOX 环境变量（测试）> 数据根 inbox/（bridge WORK_ROOT）。"""
    env = __import__("os").environ.get("OFFICETOOLS_INBOX", "").strip()
    if env:
        return Path(env).resolve()
    host = __import__("os").environ.get("WECHAT_CLAW_HOST", "").strip()
    if host:
        return (Path(host) / "inbox").resolve()
    return MODULE_DIR.parent.parent / "inbox"


def extract_inbox_path(text: str) -> Path | None:
    """从消息文本提取媒体文件路径；S1：resolve 后必须位于 inbox 内且真实存在。"""
    m = _INBOX_HINT_RE.search(text)
    if not m:
        return None
    try:
        path = Path(m.group("path")).resolve()
    except (OSError, ValueError):
        return None
    try:
        path.relative_to(inbox_dir())
    except ValueError:
        return None
    return path if path.is_file() else None


# ---------- 文件定位 ----------

def _supported_exts(ocr_ready: bool) -> set[str]:
    exts = set(parsers.CORE_EXTS) | set(parsers.DOC_TOOL_EXTS)
    if ocr_ready:
        exts |= parsers.IMAGE_EXTS
    return exts


def locate_target(text: str, settings: dict, ocr_ready: bool) -> tuple[Path | None, str]:
    """定位待解读文件。返回 (路径, 说明)。策略：点名 > state 最新未解读(24h) > 扫 inbox 最新。"""
    inbox = inbox_dir()
    if not inbox.is_dir():
        return None, "inbox 目录不存在"

    # 1) 点名："解读 合同.pdf" → 文件名匹配（精确 > 前缀，各取 mtime 最新）
    m = re.search(r"解读\s+(?P<name>[^\s]+)", text)
    if m and not m.group("name").startswith("["):
        want = m.group("name").strip()
        candidates = sorted(
            (p for p in inbox.iterdir() if p.is_file() and p.name.startswith(want)),
            key=lambda p: p.stat().st_mtime,
        )
        if candidates:
            return candidates[-1], "点名匹配"
        return None, f"inbox 中没有名为 {want} 的文件"

    # 2) state 最新未解读且新鲜
    state = load_sent_json(STATE_FILE)
    latest = state.get("latest_file") or {}
    try:
        if (
            latest.get("path")
            and not latest.get("interpreted")
            and time.time() - float(latest.get("received_at", 0)) < FRESH_WINDOW_HOURS * 3600
        ):
            p = Path(latest["path"])
            if p.is_file() and p.suffix.lower() in _supported_exts(ocr_ready):
                return p, "最近收到的文件"
    except (TypeError, OSError):
        pass

    # 3) 扫 inbox：支持格式、mtime 最新、未解读
    interpreted = set(state.get("interpreted_files") or [])
    exts = _supported_exts(ocr_ready)
    candidates = sorted(
        (
            p for p in inbox.iterdir()
            if p.is_file() and p.suffix.lower() in exts and str(p) not in interpreted
        ),
        key=lambda p: p.stat().st_mtime,
    )
    if candidates:
        return candidates[-1], "inbox 最新未解读文件"
    return None, "inbox 中没有可解读的新文件"


def _pending_count(settings: dict, ocr_ready: bool, exclude: Path | None) -> int:
    inbox = inbox_dir()
    if not inbox.is_dir():
        return 0
    state = load_sent_json(STATE_FILE)
    interpreted = set(state.get("interpreted_files") or [])
    exts = _supported_exts(ocr_ready)
    return sum(
        1 for p in inbox.iterdir()
        if p.is_file() and p.suffix.lower() in exts and str(p) not in interpreted and p != exclude
    )


# ---------- 产物与概览 ----------

def _overview_text(r: parsers.ParseResult, state_path: str, s: dict) -> str:
    """微信纯文本概览（不用 md 语法）。"""
    lines = [f"[文档解读] {r.kind_label}（{r.units_label}，约 {r.total_chars} 字）"]
    if r.titles:
        lines.append("结构：" + " / ".join(r.titles[:5]) + ("…" if len(r.titles) > 5 else ""))
    if r.excerpt:
        excerpt = r.excerpt.replace("\n", " ").strip()
        budget = int(s.get("overview_max_chars", 800)) - sum(len(l) for l in lines) - 40
        if budget > 20:
            lines.append("摘录：" + excerpt[:budget])
    for t in r.truncated:
        lines.append(f"注意：{t}")
    lines.append(f"全文已存：{state_path}")
    lines.append("可直接说“这个文件里…”继续追问，或说“解读 文件名”解读其他文件")
    text = "\n".join(lines)
    limit = int(s.get("overview_max_chars", 800))
    return text[:limit] + "…（已截断）" if len(text) > limit else text


def _cleanup_outputs(retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    for p in OUTPUTS_DIR.glob("*.md"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


# ---------- 解析主流程 ----------

def parse_and_deliver(target: Path, settings: dict, dry: bool = False) -> tuple[int, str]:
    """解析 target → 产物 + 概览。返回 (rc, 回复文本)。"""
    ocr_ready = _ocr_ready(settings)

    size_limit = int(settings.get("max_file_mb", 200)) * 1024 * 1024
    try:
        size = target.stat().st_size
    except OSError:
        return 0, "文件读取失败，请重发一次"
    if size > size_limit:
        return 0, f"文件过大（{size // (1024 * 1024)}MB > 上限 {settings.get('max_file_mb', 200)}MB），暂不支持解析"

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_FILE.open("w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0, "上一个文件正在解析中，请稍候再试"

        ext = target.suffix.lower()
        if ext in parsers.RANGE_EXTS:
            return 0, f"暂不支持 {ext} 文件，支持：{parsers.supported_label()}"

        try:
            result = parsers.parse(target, settings, ocr_ready)
        except parsers.ParseError as e:
            log_event("INFO", MODULE_NAME, "parse_reject", f"{e.kind}: {e.message}")
            if e.kind == "tool_missing":
                return 0, e.message
            if e.kind == "ocr_unavailable":
                return 0, e.message
            return 3, ""  # encrypted/corrupted/unsupported → 转 agent 兜底
        except Exception as e:  # 未预期异常同样转 agent
            log_event("ERROR", MODULE_NAME, "parse_error", str(e)[:300])
            return 3, ""

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = OUTPUTS_DIR / f"{target.stem}-{ts}.md"
        if dry:
            return 0, f"[dry] 将生成产物 {out_path.name} 并回概览（{result.total_chars} 字）"
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(f"# {target.name}\n\n{result.md}\n", encoding="utf-8")

        state = load_sent_json(STATE_FILE)
        state["latest_file"] = {
            "path": str(target), "name": target.name,
            "received_at": state.get("latest_file", {}).get("received_at", time.time()),
            "interpreted": True,
        }
        state["latest_output"] = str(out_path)
        interpreted = list(state.get("interpreted_files") or [])
        if str(target) not in interpreted:
            interpreted.append(str(target))
            state["interpreted_files"] = interpreted[-50:]
        save_sent_json(STATE_FILE, state)
        shared_save("officetools_latest", {
            "file": target.name, "path": str(target), "output": str(out_path),
            "kind": result.kind_label, "overview_excerpt": result.excerpt[:200],
        })
        _cleanup_outputs(int(settings.get("retention_days", 7)))
        log_event("INFO", MODULE_NAME, "parsed", f"{target.name} → {out_path.name}")

        pending = _pending_count(settings, ocr_ready, exclude=target)
        reply = _overview_text(result, str(out_path), settings)
        if pending:
            reply += f"\n（inbox 还有 {pending} 个未解读文件）"
        return 0, reply
    finally:
        lock.close()


def _record_received(target: Path) -> None:
    state = load_sent_json(STATE_FILE)
    state["latest_file"] = {
        "path": str(target), "name": target.name,
        "received_at": time.time(), "interpreted": False,
    }
    save_sent_json(STATE_FILE, state)


# ---------- 入口分支 ----------

def handle_inbound(text: str, conversation_id: str, dry: bool = False) -> tuple[int, str]:
    settings = _settings()
    core_ok, _ = bootstrap.ensure_core()
    if not core_ok:
        return 0, "解析组件准备中（首次启用自动安装），几分钟后重试"

    target = extract_inbox_path(text)
    if target is not None:
        # 范围外格式：无论开关，一律 rc=0 回支持清单（转 agent 无意义）
        if target.suffix.lower() in parsers.RANGE_EXTS:
            return 0, f"暂不支持 {target.suffix} 文件，支持：{parsers.supported_label()}"
        if text.strip().startswith("[收到"):
            _record_received(target)
            if not settings.get("file_auto_on"):
                return 3, ""
        return parse_and_deliver(target, settings, dry=dry)

    if "解读" in text:
        ocr_ready = _ocr_ready(settings)
        target, reason = locate_target(text, settings, ocr_ready)
        if target is None:
            return 0, f"没有找到要解读的文件（{reason}）。先发文件，再说“解读”即可；或用“解读 文件名”点名。"
        return parse_and_deliver(target, settings, dry=dry)

    return 3, ""


def bootstrap_check(settings: dict | None = None) -> int:
    """守护分支：依赖就绪 → 秒退 rc=0；缺 → 自举（可被 300s 杀，pip 断点续装）。"""
    settings = settings or _settings()
    ok, detail = bootstrap.ensure_core()
    if not ok:
        log_event("WARN", MODULE_NAME, "bootstrap_core_fail", detail)
        return 1
    if settings.get("ocr_on"):
        ok, detail = bootstrap.ensure_ocr()
        if ok:
            ok, detail = bootstrap.warmup_ocr()
        if not ok:
            log_event("WARN", MODULE_NAME, "bootstrap_ocr_fail", detail)
            return 1
    return 0


def _inspect() -> int:
    """零副作用自检：依赖就绪状态 + 最新产物（裸 spawn 防回归用例与部署排查共用）。

    只读：不装依赖、不写文件、不触碰 crypto；pylibs 缺失时报告未就绪而非现场自举。
    """
    pylibs = bootstrap.pylibs_dir()
    core_ok = bootstrap._imports_ok(bootstrap.CORE_IMPORTS)
    ocr_ok = bootstrap._imports_ok(bootstrap.OCR_IMPORTS)
    print("state:")
    print(f"  pylibs: {pylibs}")
    print(f"  core: {'就绪' if core_ok else '未就绪（守护周期自动安装）'}")
    s = _settings()
    print(f"  ocr_on: {s.get('ocr_on')} / ocr: {'就绪' if ocr_ok else '未就绪'}")
    print(f"  inbox: {inbox_dir()}")
    state = load_sent_json(STATE_FILE)
    latest = state.get("latest_file") or {}
    print(f"  latest_file: {latest.get('name', '无')}（interpreted={latest.get('interpreted', '-')}）")
    print(f"  latest_output: {state.get('latest_output', '无')}")
    outputs = OUTPUTS_DIR.glob("*.md") if OUTPUTS_DIR.is_dir() else []
    print(f"  outputs: {len(list(outputs))} 个产物")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    dry = "--dry-run" in argv

    if "--inspect" in argv:
        return _inspect()
    if "--bootstrap-check" in argv:
        return bootstrap_check()
    if "--bootstrap" in argv:
        ok, detail = bootstrap.ensure_core()
        if not ok:
            print(f"[Officetools] 核心依赖安装失败: {detail}")
            return 1
        settings = _settings()
        if settings.get("ocr_on"):
            ok, detail = bootstrap.ensure_ocr()
            if ok:
                ok, detail = bootstrap.warmup_ocr()
            if not ok:
                print(f"[Officetools] OCR 层失败: {detail}")
                return 1
        print("[Officetools] 依赖就绪")
        return 0

    if "--inbound" in argv:
        idx = argv.index("--inbound")
        text = argv[idx + 1] if len(argv) > idx + 1 else ""
        conv = ""
        if "--conversation" in argv:
            cidx = argv.index("--conversation")
            conv = argv[cidx + 1] if len(argv) > cidx + 1 else ""
        rc, reply = handle_inbound(text, conv, dry=dry)
        if reply:
            print(reply)
        return rc

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
