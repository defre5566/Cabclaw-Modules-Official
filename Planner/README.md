# Planner 模块（早晚报聚合）

> 晨间早报 + 晚间复盘：聚合任务/天气/节假日/倒计时/信息简报，agent 加工成口语化文案推送。
> 聚合型 + agent 协作型——worker 拼素材，agent 组织话术。

## 功能

- **早报**（默认 08:30）：天气+气象预警 → 节假日/农历 → 倒计时/纪念日 → 逾期 → 今日待办 → 简报要点 → 收尾
- **晚报**（默认 21:00）：今日完成 → 未完成/逾期 → 按完成度鼓励/提醒 → 晚间建议
- **信息简报**（可选，默认关）：每日按所选方向（预设 6 个 + 自定义）生成 HTML 简报，
  早报时刻前 5 分钟自动生成，早报附要点与原文文件
- **花粉/台风**（可选，按位置显隐）：内蒙古显示花粉开关、沿海省份显示台风开关

## 设置（web 模块参数区）

| 字段 | 默认 | 说明 |
|---|---|---|
| planner_on | 开 | 早晚报总开关（关闭后早晚报都不推送） |
| morning_time | 08:30 | 早报时刻（HH:MM，保存后自动联动调度） |
| evening_on | 开 | 晚报独立开关 |
| evening_time | 21:00 | 晚报时刻 |
| briefing_on | 关 | 简报开关 |
| briefing_topics | 热点 | 简报方向（最多 3 个，预设只读 + 自定义可删） |
| pollen_on | 关 | 花粉浓度（仅内蒙古生效，web 按位置显隐） |
| typhoon_on | 关 | 台风动态（仅沿海省份生效，web 按位置显隐） |

## 数据

- 任务数据消费 todo 的共享层（`shared/tasks.json`），不直接读 todo 文件
- 倒计时/纪念日：`modules/modules_data/Planner/countdown.json`（微信说"记个纪念日"由 agent 维护）
- 简报：`modules/modules_data/Planner/briefing/*.html`（私有，不进 shared）
- 自定义 prompt：`modules/modules_data/Planner/prompts/custom/`（web 导入/删除）

## 依赖

- 宿主平台（wechat-claw）：common 公共库（weather/calendar/holidays/localdata/io/push）
- todo 模块（可选，任务数据源）；opencode scheduler（可选，简报 job 定时生产）
- skills（简报基底要求）：微信公众号检索（重点）> Exa > websearch

## 设计规格

见模块源仓库 `docs/Planner-设计规格.md`。
