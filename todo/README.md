# todo 模块 · 部署者说明

待办提醒：到点推送"今天到期"的任务，支持两种数据源（设置切换，单数据源，不迁移旧数据）。

## 快速开始

1. 安装主项目 cabclaw（本模块依赖其 `modules/common/` 公共库）
2. 拷贝本目录到主项目 `modules/todo/`，执行 `register.py --enable todo`
3. （可选）管理后台 → 模块 → todo → 设置：默认**内置数据源**开箱即用；想接 Obsidian 库则切换数据源并填库目录

## 数据源

| 数据源 | 说明 |
|---|---|
| **内置数据源**（默认） | 任务存 `modules/modules_data/todo/tasks/YYYY-MM.json`（按月），由 agent 在会话中维护（说"记个任务"即可）；worker 全扫所有月文件按到期日过滤 |
| **Obsidian** | 递归扫描你填的库目录下所有 `.md` 的 Tasks 任务行，**带日期（📅）的才算待办**；⏰/🔔 为本产品扩展标记（Tasks 原生无时间字段）；任务由 agent 写入设置指定的 `vault_task_dir`（留空默认库内 `cabclaw-todo/`，文件固定 `todo-YYYY-MM.md`，当月存在则追加） |

## 任务字段

`id`（防重键）· `text` · `due`（到期日）· `time`（到点时刻，无则不提醒只存档）· `remind_min`（提前量）· `done` · `repeat`（每日/每周/每月重复）· `done_dates`（重复任务按天完成）· `tags`（词表标签，设置里可编辑）

## 标签

- 词表默认：工作 / 学习 / 生活 / 家庭 / 购物 / 健康 / 娱乐（设置里可增删；"允许新增分类词"关闭时 agent 不造新词）
- Obsidian 模式：按设置的标签前缀提取（如 `#todo/`）；前缀留空 = 不提取

## 共享数据

模块每次运行刷新 `modules/common/shared/tasks.json`（任务全量 + 时间戳），供其他模块/agent 查询（"今天有什么待办"）。消费方请带 `max_age=600` 校验新鲜度。

## 注意

- 切换数据源当天，已推送任务可能重复提醒一次（防重键不同，属预期）
- 任务文件由 agent 维护；worker 只读（坏文件跳过并告警，不崩）
