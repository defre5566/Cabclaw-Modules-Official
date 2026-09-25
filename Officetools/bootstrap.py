"""Officetools 依赖自举：pip install --target 到模块数据区 pylibs/。

设计（方案 v3 §四）：
- pylibs 目录：环境变量 OFFICETOOLS_PYLIBS 优先（测试/开发），默认数据区 pylibs/
- 标记文件只是快速路径：import 探测优先，标记存在但库损坏 → 重装
- requirements 变更检测：core/ocr 独立 hash 与对应 requirements 文件比对，变更 → 增量安装
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
# 宿主 scheduler 单次 worker 超时 300s；pip 共享预算并留导入检查余量。
INSTALL_BUDGET_SECONDS = 250
FAIL_FILE = ".bootstrap-fail"
MIRROR_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PYPI_INDEX = "https://pypi.org/simple"

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


def _hash_file(pylibs: Path, layer: str) -> Path:
    return pylibs / f".requirements-{layer}.sha256"


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


def _wheelhouse(layer: str, pylibs: Path) -> Path:
    """本地 wheel 真源：显式开发覆盖，或模块数据区中已预置的分层 wheel。"""
    override = os.environ.get("OFFICETOOLS_WHEELHOUSE", "").strip()
    return (Path(override).expanduser() if override else pylibs.parent / "wheelhouse") / layer


def _pip_output(proc: subprocess.CompletedProcess) -> str:
    """单文件版 pip 输出重定向到工作目录，不在子进程 stdout/stderr 管道。"""
    io_dir = os.environ.get("CABCLAW_WORKER_IO_DIR", "")
    if io_dir and getattr(sys, "frozen", False):
        root = Path(io_dir)
        for name in ("pip-stderr", "pip-stdout"):
            try:
                text = (root / name).read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    return text[-400:]
            except OSError:
                continue
    return (proc.stderr or proc.stdout or "").strip()[-400:]


def _install(layer: str, pylibs: Path) -> tuple[bool, str]:
    """本地 wheel → 国内镜像 → PyPI，均只使用预编译 wheel 安装到数据区。"""
    req = _req_file(layer)
    if not req.is_file():
        return False, f"缺 {req.name}"
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "--only-binary=:all:", "--retries", "1", "--timeout", "10",
            "--upgrade", "--target", str(pylibs), "-r", str(req)]
    sources: list[tuple[str, list[str]]] = []
    wheelhouse = _wheelhouse(layer, pylibs)
    if wheelhouse.is_dir() and any(wheelhouse.glob("*.whl")):
        sources.append(("本地 wheel", ["--no-index", "--find-links", str(wheelhouse)]))
    sources.extend((("国内镜像", ["--index-url", MIRROR_INDEX]),
                    ("PyPI", ["--index-url", PYPI_INDEX])))
    failures = []
    deadline = time.monotonic() + INSTALL_BUDGET_SECONDS
    for label, options in sources:
        remaining = deadline - time.monotonic()
        if remaining < 1:
            failures.append("pip 总预算已用尽")
            break
        try:
            proc = subprocess.run([*base, *options], capture_output=True, text=True,
                                  timeout=min(120, remaining))
        except subprocess.TimeoutExpired:
            failures.append(f"{label} 超时")
            continue
        except OSError as exc:
            failures.append(f"{label} 启动失败: {exc}")
            continue
        if proc.returncode == 0:
            return True, "ok"
        failures.append(f"{label} pip rc={proc.returncode}: {_pip_output(proc)}")
    return False, "; ".join(failures)[-800:]


def _record_fail(pylibs: Path, layer: str, detail: str) -> None:
    pylibs.mkdir(parents=True, exist_ok=True)
    (pylibs / f"{FAIL_FILE}-{layer}").write_text(
        json.dumps({"ts": time.time(), "detail": detail}), encoding="utf-8"
    )


def _backoff_active(pylibs: Path, layer: str) -> bool:
    f = pylibs / f"{FAIL_FILE}-{layer}"
    if not f.is_file():
        return False
    try:
        rec = json.loads(f.read_text(encoding="utf-8"))
        return time.time() - float(rec.get("ts", 0)) < RETRY_BACKOFF_SECONDS
    except Exception:
        return False


def _clear_fail(pylibs: Path, layer: str) -> None:
    try:
        (pylibs / f"{FAIL_FILE}-{layer}").unlink()
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
    req_hash = _hash_requirements(layer)
    hash_file = _hash_file(pylibs, layer)
    try:
        saved_hash = hash_file.read_text(encoding="utf-8").strip()
    except OSError:
        saved_hash = ""
    if _imports_ok(mods) and (not saved_hash or saved_hash == req_hash):
        _clear_fail(pylibs, layer)
        pylibs.mkdir(parents=True, exist_ok=True)
        _marker(pylibs, layer).touch()
        hash_file.write_text(req_hash, encoding="utf-8")
        return True, "ok"

    # 探测失败或需求版本变更但仍在退避中，下个周期再试。
    if _backoff_active(pylibs, layer):
        return False, f"{layer} 安装失败退避中（≤30 分钟后自动重试）"

    ok, detail = _install(layer, pylibs)
    if ok:
        # 首次安装前 pylibs 不存在，入口处的 inject_sys_path 尚未插入该目录。
        inject_sys_path(pylibs)
    if ok and _imports_ok(mods):
        _clear_fail(pylibs, layer)
        _marker(pylibs, layer).touch()
        hash_file.write_text(req_hash, encoding="utf-8")
        return True, "ok"
    if ok and not _imports_ok(mods):
        detail = "pip 成功但 import 探测仍失败"
    _record_fail(pylibs, layer, detail)
    return False, detail


def ensure_core(pylibs: Path | None = None) -> tuple[bool, str]:
    return ensure_layer("core", pylibs)


def ensure_ocr(pylibs: Path | None = None) -> tuple[bool, str]:
    return ensure_layer("ocr", pylibs)


def create_ocr_engine():
    """给 RapidOCR 显式传字符串模型目录，兼容 wheel 所解析的 OmegaConf 版本。"""
    import rapidocr
    from rapidocr import RapidOCR

    models_dir = Path(rapidocr.__file__).resolve().parent / "models"
    return RapidOCR(params={"Global.model_root_dir": str(models_dir)})


def warmup_ocr(pylibs: Path | None = None) -> tuple[bool, str]:
    """OCR 模型预热：1x1 白图 dummy 推理，触发 rapidocr 模型下载/初始化。"""
    pylibs = pylibs or pylibs_dir()
    inject_sys_path(pylibs)
    if not _imports_ok(OCR_IMPORTS):
        return False, "OCR 依赖未就绪"
    try:
        engine = create_ocr_engine()
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
