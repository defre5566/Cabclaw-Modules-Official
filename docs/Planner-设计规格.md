# 开发文档 05 · Planner 模块设计规格

> 2026-08-20 逐块讨论定稿（鑫拍板）。Planner = 早晚报聚合模块（晨间早报 + 晚间复盘），
> 聚合型 + agent 协作型。本规格为产品化重做设计——生产库的 planner 是个人实现，不作为设计依据，
> 仅个别经验（如信源分层方法论）提炼为通用能力。
> 关联：common 层改造见模块化方案 §十三（weather 纯天气化 + localdata 新建）。

## 1. 定位与链路

- **职责**：每天按用户设定时刻，聚合"计划/回顾/信息"数据，生成晨间早报 + 晚间复盘
- **链路**：worker 拼素材 → `post_push(reminder)` → push_server agent 队列（PUSH_AGENT_TYPES）→ agent 按全局 AGENTS.md 加工成口语化文案 → 用户
- **模块名**：Planner（P 大写）；目录 `modules/Planner/`，数据 `modules/modules_data/Planner/`

## 2. 数据依赖（全部由数据可得性决定，非设置项）

| 段 | 来源 | 规则 |
|---|---|---|
| 任务（待办/完成/逾期） | todo 的 `shared/tasks.json` | **读到就读，读不到就没有**（planner 是消费者，任务数据归属 todo） |
| 节假日/农历节气 | `common.calendar` / `common.holidays` | 同上（归属 common） |
| 天气 + 气象预警 | `common.weather` | location 驱动；预警（暴雨/洪水/大风等）同源 |
| 花粉/台风 | `common.localdata` | location 符合 → 拉取 → **进 shared**（客观数据） |
| 倒计时/纪念日 | `modules_data/Planner/countdown.json` | planner 自己生产，agent 维护；有条目才有段 |
| 简报 | `modules_data/Planner/briefing/`（HTML 原文） | 开关 + 有产物才段 |

## 3. 设置结构（全部带 desc 说明文字）

```
planner_on        # 早晚报总开关（默认开）——关闭后早晚报都不推送
morning_time      # 早报时刻
evening_on        # 晚报独立开关（默认开）——可单独关闭晚间复盘
evening_time      # 晚报时刻
briefing_on       # 简报开关（默认关）
briefing_topics   # 简报方向多选（最多 3 个，默认热点）
pollen_on         # 花粉开关（默认关；location 内蒙才生效，web 按 location 显隐）
typhoon_on        # 台风开关（默认关；location 沿海才生效）
```

原则：
- **核心内容段无开关**（任务/节假日/天气/倒计时）——由数据可得性决定；用户不想要 → 关整个模块
- **可选增值段有开关且默认关**（简报/花粉/台风）
- 设置保存走 `register.update_module(settings=...)`（settings.json），校验器按 schema 清洗

## 4. 简报块

### 4.1 产出与消费
- **HTML 原文** → `modules_data/Planner/briefing/YYYY-MM-DD.html`（**私有，不进 shared**——外部信息，私人助理不代做判断）
- 时序：`morning_time - 5min` 生产（settings 联动）
- 早报消费：热度高/影响大要点几句 + 原文 file 附发（**不二次摘要，防失真**）

### 4.2 基底 prompt（平台预设）
- 存储：`modules/Planner/prompts/base.prompt.md`，明文随模块发布；内容变化纳入 job 定义 revision，旧执行不得继续提交
- 内容：昨日 24h · 三层信源（搜索 → 核实 → 评论解读检索）· 事实/观点分层 + 来源标注 · 通用三段（要点 / 评论与解读 / 延伸关注）· 字数参数化 · 失败重试 · HTML 输出
- 工具：使用宿主提供的 `web_search`、`fetch_url`、`write_file`；不依赖 Exa、webfetch、Jina、curl 或外部 skill。搜索/抓取失败时不得凭模型记忆补造联网事实

### 4.3 方向（预设只读，不可编辑）
| 方向 | 类型 | 信源 |
|---|---|---|
| 时政 | 深度（生产库提取基底） | 人民网/新华网/央视网（含新闻联播核实） |
| 经济 | 深度 | 新华社财经/经济日报/央视财经 |
| 民生 | 深度 | 人民日报/政府发布 |
| 娱乐 | 深度 | 主流文娱媒体 |
| 热点 | 轻量 | **微博热搜 + 百度热点** |
| 抖音飙升榜 | 轻量 | 抖音热榜/飙升榜 |

每方向 = `{keywords, comment_words, sources}`；默认勾选：热点。

### 4.4 自定义 prompt
- web 粘贴纯文本 + 方向名 → `modules_data/Planner/prompts/custom/`（明文，用户可看可改）
- 词条胶囊旁 ✕ 可删；走同一 job 路径
- 方向选择 UI：词条选择器（预设只读 + 自定义可删），勾选 ≤3 个

### 4.5 清理策略
```
简报数 > 5  → 每 5 天执行清理，删最旧 5 个
简报数 ≤ 5  → 清理 3 天前的（保留近 3 天）
简报数 ≤ 3  → 不执行清理
```

## 5. 倒计时/纪念日块

- 数据：`countdown.json` = `{"entries": [{"name", "date", "repeat": bool}]}`，agent 维护（微信"记个纪念日" → 写文件）
- **repeat**（生日/纪念日）：距下一次 ≤15 天开始报，到期报"就是今天"
- **一次性**：创建即报；过期停报；**超时 15 天自动删除**

## 6. 内容组装（早晚报）

### 早报段序
问候 → 天气+气象预警（带对应处理建议，如雨→带伞）→ 节假日/农历（带处理）→ 倒计时/纪念日（带处理，该准备了）→ **逾期（提前，催处理）** → 今日待办 → 简报要点（附原文） → 收尾

### 晚报段序
问候 → 今日完成 → 今日未完成/逾期 → 按完成度鼓励/提醒 → 晚间建议（通用化指引，不写具体私人化例子）

### 分工与话术
- worker 拼"素材文本 + 组织指令"（生产模式）；agent 按 agents.md 组织成文案
- 话术风格跟随**全局 AGENTS.md**（助理人设全局配置）；planner 的 agents.md 只写素材组织规则，不独立定制话术

## 7. 执行架构

### 7.1 worker
```
planner_worker.py --phase morning|evening [--dry-run]
morning: 问候 → 天气+预警 → 节假日/农历 → 倒计时 → 逾期 → 待办 → 简报要点
evening: 问候 → 完成 → 未完成/逾期 → 鼓励 → 晚间建议
→ post_push(reminder, 素材+组织指令)
防重：modules_data/Planner/morning_sent.json / evening_sent.json（common.io）
```

### 7.2 schedule 联动（平台新机制）
- module.json 声明 `schedule_from_settings`：`[{"phase": "morning", "time_field": "morning_time"}, {"phase": "evening", "time_field": "evening_time"}]`
- register 保存设置时：按时刻生成 cron（如 `08:30` → `30 8 * * *`，args 带 `--phase`）写入 schedule；`planner_on=false` → schedule 清空
- **平台级能力**：register 扩展 + docs/04 补充，任何带时间设置的模块可用

### 7.3 简报 job 注册
- planner 携带 `job.template.json`（agent 型长任务模板）
- register 联动 bridge scheduler：启用 → 注册（daily phase = morning_time - 5min）；设置变化 → 更新（prompt = 基底 + 方向适配 + 自定义）；停用/卸载 → 定义失效并取消 queued/running 执行
- 基底 prompt、`directions.json`、自定义 prompt 或设置快照变化都会更新定义 revision；同一进程中旧任务收到取消信号，不能发布过期产物
- 容错：job 登记失败 → 简报段自动无段，不阻塞早晚报

### 7.4 数据目录
```
modules/modules_data/Planner/
├── countdown.json
├── morning_sent.json / evening_sent.json
├── briefing/            # HTML 简报（+ 清理策略）
└── prompts/custom/      # 用户导入 prompt（明文）
```

## 8. 模块结构（modules/Planner/）

```
modules/Planner/
├── planner_worker.py        # --phase morning|evening + --dry-run
├── module.json              # settings_schema + schedule_from_settings + schedule
├── 规范.md                  # 素材组装规范（段序/数据源）
├── agents.md                # agent 素材组织规则（话术跟随全局 AGENTS.md）
├── job.template.json        # 简报生成 agent job 模板
├── prompts/                 # 预设 prompt（明文 base.prompt.md）
└── README.md                # 模块自述
```

## 9. 关联平台改造

| 项 | 内容 | 归属 |
|---|---|---|
| common.weather | 移出花粉 → 纯天气（location 驱动 + 30min 缓存 + 气象预警含洪水） | 平台 |
| common.localdata | 新建：SERVICES 注册表（pollen 内蒙古 / typhoon 沿海），available(loc)/fetch(loc, service=None)，每日缓存，符合 location 进 shared | 平台 |
| register | schedule_from_settings 联动 + job 注册/更新/注销 | 平台 |
| web | 设置渲染（desc 说明 + 简报方向词条选择器 + 自定义 prompt 导入/删除 + 花粉台风按 location 显隐） | 平台 |
| docs/04 | schedule_from_settings 机制 + job 模板注册规范 | 平台 |
| push_server | reminder → agent 队列（已有，确认可用） | 平台 |
