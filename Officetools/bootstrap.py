"""Officetools 依赖自举：pip install --target 到模块数据区 pylibs/。

设计（方案 v3 §四）：
- pylibs 目录：环境变量 OFFICETOOLS_PYLIBS 优先（测试/开发），默认数据区 pylibs/
- 标记文件只是快速路径：import 探测优先，标记存在但库损坏 → 重装
- requirements 变更检测：.requirements-hash 与当前 requirements 文件比对，变更 → 增量安装
- 安装失败退避 RETRY_BACKOFF_SECONDS，避免守护 tick 每 5 分钟空跑 pip
- 本模块仅用标准库（不依赖 common），供 worker 与 tests 双向复用
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
DATA_DIR = MODULE_DIR.parent / "modules_data" / "Officetools"

RETRY_BACKOFF_SECONDS = 30 * 60
FAIL_FILE = ".bootstrap-fail"

CORE_IMPORTS = ("pdfplumber", "docx", "openpyxl", "pptx", "xlrd")
OCR_IMPORTS = ("rapidocr", "pypdfium2", "onnxruntime")


def pylibs_dir() -> Path:
    """pylibs 真源：环境变量 OFFICETOOLS_PYLIBS > 数据区 pylibs/。"""
    env = os.environ.get("OFFICETOOLS_PYLIBS", "").strip()
    if env:
        return Path(env).resolve()
    return DATA_DIR / "pylibs"


def _marker(pylibs: Path, layer: str) -> Path:
    return pylibs / f".ready-{layer}"


def _req_file(layer: str) -> Path:
    return MODULE_DIR / ("requirements.txt" if layer == "core" else "requirements-ocr.txt")


def _imports_ok(mods: tuple[str, ...]) -> bool:
    for mod in mods:
        try:
            __import__(mod)
        except Exception:
            return False
    return True


def _hash_requirements(layer: str) -> str:
    return hashlib.sha256(_req_file(layer).read_bytes()).hexdigest()


def _install(layer: str, pylibs: Path) -> tuple[bool, str]:
    """pip install --target pylibs；返回 (成功, 说明)。"""
    req = _req_file(layer)
    if not req.is_file():
        return False, f"缺 {req.name}"
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-input",
        "--target", str(pylibs),
        "-r", str(req),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=280)
    except subprocess.TimeoutExpired:
        return False, "pip 超时（280s），下个周期断点续装"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-400:].strip()
        return False, f"pip rc={proc.returncode}: {tail}"
    return True, "ok"


def _record_fail(pylibs: Path, layer: str, detail: str) -> None:
    pylibs.mkdir(parents=True, exist_ok=True)
    (pylibs / FAIL_FILE).write_text(
        json.dumps({"layer": layer, "ts": time.time(), "detail": detail}), encoding="utf-8"
    )


def _backoff_active(pylibs: Path) -> bool:
    f = pylibs / FAIL_FILE
    if not f.is_file():
        return False
    try:
        rec = json.loads(f.read_text(encoding="utf-8"))
        return time.time() - float(rec.get("ts", 0)) < RETRY_BACKOFF_SECONDS
    except Exception:
        return False


def _clear_fail(pylibs: Path) -> None:
    try:
        (pylibs / FAIL_FILE).unlink()
    except FileNotFoundError:
        pass


def ensure_layer(layer: str, pylibs: Path | None = None) -> tuple[bool, str]:
    """确保某层依赖可用；返回 (就绪, 说明)。

    layer: "core" | "ocr"
    就绪判定：import 探测通过（标记仅加速：标记在且 hash 未变且探测通过 → 秒回）。
    """
    pylibs = pylibs or pylibs_dir()
    inject_sys_path(pylibs)
    mods = CORE_IMPORTS if layer == "core" else OCR_IMPORTS
    if _imports_ok(mods):
        _clear_fail(pylibs)
        _marker(pylibs, layer).touch()
        if layer == "core":
            try:
                (pylibs / ".requirements-hash").write_text(_hash_requirements("core"), encoding="utf-8")
            except OSError:
                pass
        return True, "ok"

    # 探测失败但退避中（除标记+hash 全一致的场景外不再重试 pip）
    req_hash = _hash_requirements(layer)
    hash_file = pylibs / ".requirements-hash"
    hash_ok = hash_file.is_file() and hash_file.read_text(encoding="utf-8").strip() == req_hash
    if _marker(pylibs, layer).is_file() and hash_ok and _backoff_active(pylibs):
        return False, f"{layer} 依赖探测失败且处于安装退避期"

    if _backoff_active(pylibs):
        return False, f"{layer} 安装失败退避中（≤30 分钟后自动重试）"

    ok, detail = _install(layer, pylibs)
    if ok and _imports_ok(mods):
        _clear_fail(pylibs)
        _marker(pylibs, layer).touch()
        (pylibs / ".requirements-hash").write_text(req_hash, encoding="utf-8")
        return True, "ok"
    if ok and not _imports_ok(mods):
        detail = "pip 成功但 import 探测仍失败"
    _record_fail(pylibs, layer, detail)
    return False, detail


def ensure_core(pylibs: Path | None = None) -> tuple[bool, str]:
    return ensure_layer("core", pylibs)


def ensure_ocr(pylibs: Path | None = None) -> tuple[bool, str]:
    return ensure_layer("ocr", pylibs)


def warmup_ocr(pylibs: Path | None = None) -> tuple[bool, str]:
    """OCR 模型预热：1x1 白图 dummy 推理，触发 rapidocr 模型下载/初始化。"""
    pylibs = pylibs or pylibs_dir()
    inject_sys_path(pylibs)
    if not _imports_ok(OCR_IMPORTS):
        return False, "OCR 依赖未就绪"
    try:
        from rapidocr import RapidOCR  # noqa: E402

        engine = RapidOCR()
        import numpy as np  # noqa: E402

        img = np.full((32, 32, 3), 255, dtype="uint8")
        engine(img)
        return True, "ok"
    except Exception as e:  # 模型下载失败/初始化异常
        return False, f"OCR 预热失败: {e}"


def inject_sys_path(pylibs: Path | None = None) -> Path:
    """pylibs 注入 sys.path（worker 入口与 tests 共用）。"""
    p = (pylibs or pylibs_dir()).resolve()
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))
    return p
