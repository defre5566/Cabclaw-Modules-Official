# Planner 模块 · agent 维护指引

> 你是 Planner 模块的素材管理员。用户说"记个纪念日/倒计时""查倒计时"时，按本指引操作。
> 完整业务语义见同目录 `规范.md`。
> 注意：早晚报推送的文案加工不在本指引（推送渲染走独立链路，不读本文件）。

## 倒计时/纪念日维护（countdown.json）

数据文件：`modules/modules_data/Planner/countdown.json`（用户数据区，明文 JSON）

- 结构：`{"entries": [{"name": "名称", "date": "YYYY-MM-DD", "repeat": true/false}]}`
- 维护两步完成：读 `modules/modules_data/Planner/countdown.json`（不存在则创建骨架）→ 按 name 合并（同名更新，不重复追加）后保存
- 用户说"记个纪念日"：确认日期；生日/周年类 → `repeat: true`（按年循环）；一次性事件（如考试、出行）→ `repeat: false`
- 用户说"查倒计时"：读文件列出未来 30 天内条目（含 repeat 的下一次日期）；注：早报推送窗口 15 天（循环任务提前 15 天报），查询窗口 30 天（用户主动查看更远规划）
- 用户说"删掉 XX"：从 entries 移除该 name
- **日期格式必须 YYYY-MM-DD**；没有日期就问用户，不猜

## 查任务（用户问任务情况时）

- 读共享层 `modules/common/shared/tasks.json`（注意 `ts` 新鲜度；过期提示用户或告知 todo 未运行）
- 按 `done_at`/`done_dates`/`due` 过滤回答（今日待办、逾期、某类标签任务等）
- **不要直接翻 todo 模块内部文件**
