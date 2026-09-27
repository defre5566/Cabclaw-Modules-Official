"""隔离 Obsidian 库验证宿主 Agent 通用文件工具能创建确定格式的新文件。

此测试不调用真实 LLM；模型是否按 agents.md 自主生成整行仍是独立验收项。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

if not os.environ.get("CABCLAW_HOST"):
    pytest.skip("须指定 CABCLAW_HOST 注入宿主公共库", allow_module_level=True)

MODULE_SRC = Path(__file__).resolve().parent.parent
if str(MODULE_SRC) not in sys.path:
    sys.path.insert(0, str(MODULE_SRC))

from bridge.native_agent import NativeAgent
from task import format_task_line, parse_task_line


def test_native_agent_creates_new_file_in_isolated_vault(tmp_path):
    vault_file = tmp_path / "vault" / "cabclaw-todo" / "todo-2026-09.md"
    assert not vault_file.exists()

    class AllowVault:
        def __init__(self):
            self.prompts = []

        async def wait(self, conv_id, prompt):
            self.prompts.append((conv_id, prompt))
            return True

    gate = AllowVault()
    agent = NativeAgent(gate=gate)

    async def write_new_file():
        line = format_task_line("准备周会 带上季度材料", "2026-09-28", at="09:30",
                                remind_min=15, tags=["工作"])
        receipt = await agent._execute_tool("synthetic-conv", "write_file", {
            "path": str(vault_file), "content": line + "\n",
        })
        assert "成功写入文件" in receipt

    asyncio.run(write_new_file())
    lines = vault_file.read_text(encoding="utf-8").splitlines()
    assert lines == ["- [ ] 准备周会 带上季度材料 ⏰ 09:30 🔔提前15分钟 "
                     "#todo/工作 📅 2026-09-28"]
    assert parse_task_line(lines[0]).text == "准备周会 带上季度材料"
    assert len(gate.prompts) == 1  # 真实用户 vault 在工作根之外仍受宿主写入确认门管理


def test_long_existing_vault_file_append_safely(tmp_path):
    vault_file = tmp_path / "vault" / "cabclaw-todo" / "todo-2026-09.md"
    vault_file.parent.mkdir(parents=True)
    original = "- [ ] 原有笔记 📅 2026-09-28\n" + "x" * 8100 + "\n原始尾部仍在\n"
    vault_file.write_text(original, encoding="utf-8")

    class AllowVault:
        async def wait(self, _conv_id, _prompt):
            return True

    agent = NativeAgent(gate=AllowVault())
    # 宿主已放宽全量读取限制，能完整读出
    full_content = asyncio.run(agent._execute_tool("synthetic-conv", "read_file", {"path": str(vault_file)}))
    assert "原始尾部仍在" in full_content

    # 通过 write_file(mode="append") 安全追加，不走整份覆写
    new_line = format_task_line("追加新任务", "2026-09-29", at="14:00")
    receipt = asyncio.run(agent._execute_tool("synthetic-conv", "write_file", {
        "path": str(vault_file),
        "content": new_line + "\n",
        "mode": "append",
    }))
    assert "成功追加写入文件" in receipt

    # 验证原文件前部、中间与尾部完全保留，且新任务成功追加在末尾
    updated_content = vault_file.read_text(encoding="utf-8")
    assert updated_content.startswith("- [ ] 原有笔记 📅 2026-09-28\n")
    assert "原始尾部仍在\n" in updated_content
    assert updated_content.endswith(new_line + "\n")

