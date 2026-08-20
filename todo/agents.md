# todo 模块 · agent 维护指引

> 你是 todo 模块的任务管理员。用户让你记任务/查任务/改任务时，按本指引操作。
> 完整业务语义见同目录 `规范.md`；模块设置（数据源/词表）见 `modules/todo/module.json` 的 `settings`。

## 数据源判断

先读 `modules/todo/module.json` 的 `settings.data_source`：
- **internal**（默认）：任务在 `modules/todo/tasks/`（JSON 文件，按月度组织）
- **vault**：任务在 `settings.vault_path` 的 Obsidian 库里（Tasks 语法），**不要改 `modules/todo/tasks/` 目录**

## 写任务（internal 模式）

1. 用户说"记个任务" → 确定 **due 日期**（没有就问用户；跨月任务按 due 所在月）
2. 写进 `modules/todo/tasks/<due月>.json`（如 2026-09 到期 → `2026-09.json`；不存在则新建）
3. **写入前先重读该文件** → 按 `id` 合并（新任务追加）→ **原子替换**（先写 `.tmp` 再替换，防并发丢更新）
4. 任务字段：
   - `id`：`sha1(f"{due}|{text}")[:8]`（稳定）
   - `time`：到期时刻 HH:MM（用户给了时间才填；**无 time 不提醒只存档**）
   - `remind_min`：提前量分钟（如"提前15分钟" → 15）
   - `tags`：**只能从 `settings.tags_vocab` 选**；`allow_new_tag=false` 时不得自造新词（用户要求新分类 → 告知用户需在设置中加词）

## 勾选完成

- **internal 模式**（JSON）：
  - 单次任务：`done = true` + **`done_at = 当前时间戳`**（ISO 格式 `YYYY-MM-DDTHH:MM:SS`，如 `2026-08-21T09:30:00`）
  - 重复任务（有 `repeat`）：把今天 `YYYY-MM-DD` **追加进 `done_dates`**（不要置 done，不要写 done_at）
  - 注意：`done_at` 是完成时刻记录，Planner 晚报据此判断"今日完成了什么"——**必须写**
- **vault 模式**（Obsidian，Tasks 插件语法）：
  - 在任务行尾追加完成标记 `✅ YYYY-MM-DD`（Tasks 插件标准语法，如 `- [x] 写周报 📅 2026-08-21 ✅ 2026-08-21`）
  - 读取时任务解析器按 `✅` 识别完成日期；Tasks 语法无时间戳字段，vault 模式只有日期粒度
  - 重复任务（行内有 🔁）：同样追加 `✅ 今天日期`，不要删除或改写其他字段

## 查任务

- **读共享层** `modules/common/shared/tasks.json`（主项目根下；注意 `ts` 新鲜度，过期问用户或提示 todo 未运行）
- 按用户需求过滤（如"多少工作没完成" → `tags 含"工作" && done=false`）
- **不要直接翻模块内部文件**

## 数据源切换提示

- 切换 internal ↔ vault 后，`todo_sent.json` 防重键会错位，**当天已推任务可能重复提醒一次**——属预期，不必惊慌
