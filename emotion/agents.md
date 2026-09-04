# emotion · agent 维护指引

> agent 涉及 emotion 模块任务时按需读本文件。数据文件加密，直读是密文，操作一律走 worker 命令。

## 路径常量

- 状态文件：`modules/modules_data/emotion/state.enc`（加密：暂停截止/当日计数/上次推送/天气基线/防重）
- 反馈文件：`modules/modules_data/emotion/feedback.enc`（加密：用户状态反馈，48h 过期）
- 设置文件：`modules/modules_data/emotion/settings.json`（明文：部署状态 + 业务设置）

## 常见操作（每条 ≤2 步）

- **查当前状态**（暂停到几点/今日已发几条/最近反馈）：运行
  `python3 modules/emotion/emotion_worker.py --inspect`，输出即明文摘要。
- **手动恢复推送**（用户要求提前结束暂停）：运行
  `python3 modules/emotion/emotion_worker.py --unpause`，输出"已清除暂停状态"即成功。
- **改推送节奏**（每日上限/最小间隔/播报时刻）：编辑
  `modules/modules_data/emotion/settings.json` 对应键（`daily_limit`、`min_interval_hours`、`report_morning/midday/evening`），保存即生效，无需重启。

## 边界

- `state.enc` / `feedback.enc` 不要用文件工具直接编辑（密文，写坏即丢状态）；改动状态走上述命令或让用户发"恢复关心"
- 用户反馈原文在 `feedback.enc`，`--inspect` 只显示最近 10 条；不要把原文贴进其他模块或推送给第三方
- 暂停/恢复的用户语义：用户说"别烦我/安静一会"类诉求，引导发"暂停关心"；人设对话（安慰/倾听）不归本模块，照常由你处理
