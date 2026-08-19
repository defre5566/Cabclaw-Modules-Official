"""模块源测试宿主注入。

todo 模块运行时依赖宿主项目的 common 公共库（modules/common/）。
本测试通过环境变量 WECHAT_CLAW_HOST 注入宿主项目根；未设置时跳过业务测试
（模块源仓库 CI 或独立运行时需显式指定宿主）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HOST = os.environ.get("WECHAT_CLAW_HOST", "").strip()
if HOST:
    host_modules = Path(HOST) / "modules"
    if host_modules.is_dir():
        sys.path.insert(0, str(host_modules))
