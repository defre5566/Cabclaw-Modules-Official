"""模块源测试宿主注入（同 todo）。

Planner 模块运行时依赖宿主项目的公共库（modules/common/ 与 bridge/config）。
本测试通过环境变量 WECHAT_CLAW_HOST 注入宿主项目根；未设置时跳过业务测试。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HOST = os.environ.get("WECHAT_CLAW_HOST", "").strip()
if HOST:
    host_root = Path(HOST)
    if (host_root / "modules").is_dir():
        sys.path.insert(0, str(host_root / "modules"))  # common 公共库（modules/）
        sys.path.insert(0, str(host_root))              # 主项目根（bridge/ 等）
