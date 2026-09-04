# emotion 模块

拟人化主动关心（人设拓展）：主动找用户说话的那部分——负责"什么时候说话、说什么素材、记住用户短期状态"，表达交给部署用户配置的 agent 人设。

## 功能

- **拟人关心**（默认线）：每天 8-21 点每小时一个随机窗口，"有话题才发"——时段问候、天气突变提醒、近期状态回应；每日条数上限与最小间隔可配，错过不补发
- **任务播报**（三阶段）：上午/下午/傍晚按设置时刻发一条轻量鼓励（不罗列清单，清单提醒归 todo 模块）；需安装 todo 模块，无任务数据自动跳过
- **倾听**（入站）：用户发"暂停关心 / 恢复关心"直接调度（模块自答）；说累/感冒/心情不好会记住（48 小时内），下次关心自然带出；深度话题转给 agent 人设
- **隐私**：用户反馈原文 AES-GCM 加密落盘，共享层只有脱敏标签

## 安装

```bash
# 方式一：从模块源安装（web 或 CLI）
# 方式二：拷贝到主项目 modules/
cp -r emotion <wechat-claw>/modules/

# 注册并启用（token 由 register 生成，模块包不含 token）
python3 <wechat-claw>/modules/register.py emotion --purpose "拟人化主动关心" --enable
```

## 设置（web 模块参数区可改，存数据区 settings.json）

| 设置 | 默认 | 说明 |
|---|---|---|
| emotion_on | 开 | 拟人关心总开关 |
| daily_limit | 4 | 拟人关心每日最多条数（任务播报不占配额） |
| min_interval_hours | 3 | 两条推送最小间隔 |
| tasks_report_on | 开 | 任务播报开关（需 todo 模块） |
| report_morning / midday / evening | 09:00 / 14:00 / 18:00 | 三阶段播报时刻 |

## 用户侧使用

- 发"**暂停关心**"：当天 21 点前不再推送（21 点后发则今日本已无推送）
- 发"**恢复关心**"：立即恢复
- 说说状态（"好累""感冒了"）：会被记住并影响之后的关心；深度倾诉由 agent 人设接手

## 自测

```bash
WECHAT_CLAW_HOST=<主项目根> python3 -m pytest emotion/tests/   # cwd = 模块源仓库根
python3 modules/emotion/emotion_worker.py --dry-run            # 只打印判定，零副作用
python3 modules/emotion/emotion_worker.py --inspect            # 查看状态与反馈摘要
```
