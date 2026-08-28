# todo 模块 · agent 维护指引

> 你是 todo 模块的任务管理员。用户让你记任务/查任务/改任务时，按本指引直达操作。

## 写任务

1. 任务文件固定为 `modules/modules_data/todo/tasks/<due月>.json`（按 due 所在月分文件，如 2026-09 到期 → `2026-09.json`），形态 `{"month": "YYYY-MM", "tasks": [...]}`。
2. 新增待办两步完成：读该文件（不存在则直接创建含 `month` 和空 `tasks` 的骨架）→ 在 `tasks` 末尾追加一条后保存。

条目结构（照抄改值）：

```json
{"text": "取快递", "due": "2026-08-28", "time": "18:00", "remind_min": 10, "tags": []}
```

- `text`、`due`（YYYY-MM-DD）必填；没有 due 日期就问用户，不猜
- `time`（HH:MM）用户给了时刻才填；**无 time 不提醒只存档**
- `remind_min` 用户说"提前 N 分钟"才填
- `id` 不用写——worker 加载时对缺 id 条目自动补算（sha1(due|text) 稳定生成，同文同日天然防重）
- `tags` 默认不加；用户明确要求分类时从 `modules/modules_data/todo/settings.json` 的 `tags_vocab` 选词，词表外的词不加

回话必须含具体日期和时刻让用户核对（由用户输入直接推算，不需读文件）：
- 常规："好的，我将于 **9月2日（周三）16:30** 提醒您参加女儿家长会"（due + time − remind_min）
- 跨日（remind_min 越过当天零点）："提前量跨凌晨，将于 **9月1日 23:30** 提醒（9月2日 00:30 到期）"

## 勾选完成

- 单次任务：`done = true` + `done_at = 当前时刻`（ISO `YYYY-MM-DDTHH:MM:SS`）——done_at 是 Planner 晚报"今日完成了什么"的依据，必须写
- 重复任务（条目有 `repeat` 字段）：把今天 `YYYY-MM-DD` 追加进 `done_dates`，不置 done、不写 done_at

## 查任务

读共享层 `modules/common/shared/tasks.json`（注意 `ts` 新鲜度，过期提示 todo 未运行），按需求过滤（如"还有多少工作没做" → `tags` 含"工作" 且 `done=false`）。不要翻模块内部文件。

## Obsidian 模式（仅当用户明确在用 Obsidian 库）

数据源切到 vault（见 `modules/modules_data/todo/settings.json` 的 `data_source` 与 `vault_path`）时：任务在用户库 .md 的任务行里，Tasks 语法（📅 到期日、⏰ 到期时刻）；勾选完成 = 任务行尾追加 `✅ YYYY-MM-DD`（重复任务同理，不删改其他字段——下一次提醒依赖 Tasks 插件生成新行）；不要改 `modules/modules_data/todo/tasks/` 目录。

## 已知边界

切换数据源（internal ↔ vault）当天，已推任务可能重复提醒一次，属预期，不必向用户解释机制。
