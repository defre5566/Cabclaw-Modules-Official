"""模块源测试宿主注入（同 Planner/todo）+ pylibs 自举。

1. WECHAT_CLAW_HOST 注入宿主项目根（common/bridge）；未设置时跳过业务测试。
2. pylibs：OFFICETOOLS_PYLIBS 指定；未指定时开发缓存 .devcache/pylibs（gitignore）。
   核心依赖缺失时调用模块自带的 bootstrap.ensure_core() 现场自举
   （同时即是对自举机制的测试）；无网失败 → skip。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent

HOST = os.environ.get("WECHAT_CLAW_HOST", "").strip()
if HOST:
    host_root = Path(HOST)
    if (host_root / "modules").is_dir():
        sys.path.insert(0, str(host_root / "modules"))  # common 公共库（modules/）
        sys.path.insert(0, str(host_root))              # 主项目根（bridge/ 等）

sys.path.insert(0, str(MODULE_DIR))

import bootstrap  # noqa: E402

PYLIBS = bootstrap.pylibs_dir()
if not os.environ.get("OFFICETOOLS_PYLIBS", "").strip():
    os.environ["OFFICETOOLS_PYLIBS"] = str(PYLIBS)
    PYLIBS = bootstrap.pylibs_dir()
bootstrap.inject_sys_path(PYLIBS)


def _core_available() -> bool:
    return bootstrap._imports_ok(bootstrap.CORE_IMPORTS)


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: OCR 层慢测试（模型加载），CI 默认跳过")


def pytest_collection_modifyitems(config, items):
    core_ok = _core_available()
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(__import__("pytest").mark.skipif(
                not core_ok or os.environ.get("OT_TEST_OCR", "") != "1",
                reason="OCR 慢测试需 OT_TEST_OCR=1 且核心依赖就绪",
            ))
        elif not core_ok:
            item.add_marker(__import__("pytest").mark.skip(reason="核心依赖未就绪且自举失败"))
