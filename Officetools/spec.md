# Officetools · 规范

> 调度时间以 module.json 为唯一事实源，本文件不重复声明。

## 职责

文档解读能力：微信收到的 pdf / office / 图片文件解析为「概览 + 全文 markdown 产物」，
产物供 agent 承接后续具体需求（总结/翻译/问答/提取）。模块只做"解读"，不做内容加工。

## 交互协议

### 文件到达（intent 命中 `[收到file`）

bridge 收到媒体文件 → 落 inbox/ → 消息流插入 `[收到file: <名>，已存 <路径>]` → 模块被 spawn：

- `file_auto_on=false`（默认）：记录 state.json 后 **rc=3 交还 agent**，agent 感知完整
- `file_auto_on=true`：模块接管（rc=0）——解析 → 概览回微信 → state.json 打包。
  注意：接管后 agent 不再看到该文件消息，靠硬索引链路（见下）感知概览与产物

### 解读指令（intent 命中 `解读`）

- 点名：`解读 合同.pdf`（支持前缀匹配，取最新）
- 未点名：state.json 最新未解读文件（24h 内）→ inbox 支持格式 mtime 最新未解读
- 没有可解读文件 → rc=0 回使用说明
- 找到 → 解析 → 概览回微信（纯文本，限 overview_max_chars）+ 产物路径

### rc 语义（对齐 docs-04 §6）

| 场景 | rc |
|---|---|
| 概览/使用说明/安装指引/准备中提示 | 0（自答） |
| 核心依赖未就绪 + "解读" | 0（提示等待，不转 agent——agent 拿不到文件路径） |
| 加密/损坏解析失败 | 3（转 agent） |
| 视频等范围外格式 | 0（回支持清单，转 agent 无意义） |

## 支持矩阵

| 格式 | 引擎 | 层 |
|---|---|---|
| .pdf（文本层） | pdfplumber | 核心 |
| .pdf（扫描件） | pypdfium2 渲染 → RapidOCR | OCR |
| .docx / .xlsx / .pptx / .xls | python-docx / openpyxl / python-pptx / xlrd | 核心 |
| .doc / .ppt | antiword / catppt（系统工具） | 可选 |
| .jpg / .jpeg / .png / .webp | RapidOCR | OCR |
| 视频/音频/压缩包等 | 不支持，rc=0 回支持清单 | — |

## 依赖自举（守护 tick，every 5m `--bootstrap-check`）

- 依赖装到 `modules/modules_data/Officetools/pylibs/`（不动宿主 venv、不碰模块代码区）
- 就绪判定以 import 探测为准（`.ready-*` 标记仅加速）；requirements hash 变更 → 自动增量装
- `ocr_on` 开 → 下个 tick 装 OCR 层 + 模型预热（约几分钟）；关 → 只停用不卸载
- pip 失败退避 30 分钟；单次安装超 280s 自杀重试（pip 断点续装）
- 手动预热：`Officetools_worker.py --bootstrap`

## 解析限制（settings 可调）

- `max_file_mb`（默认 200）：超限 rc=0 提示
- `max_pages`（默认 300）：PDF 超出只解析前 N 页并在概览说明
- `ocr_max_pages`（默认 20）：单次 OCR 页数上限；渲染 DPI 150、最长边 2200px
- 表格三重上限：列 30 / 行 200 / 单元格 200 字

## 产物与状态

- 产物：`modules/modules_data/Officetools/outputs/<文件名>-<时间戳>.md`（保留 retention_days 天，默认 7，超期清理）
- 状态：`state.json`（latest_file / latest_output / interpreted_files 滚动 50 条）——agent 读这里拿最新打包
- 跨模块：`shared_save("officetools_latest", …)` 元数据

## 已知边界（明示）

1. **模块 → agent 无实时通道**（主体机制）：接管后的追问依赖硬索引命中宽词表
   （文件/文档/解读/解析/表格/pdf/ppt/excel/word/总结/翻译/提取）；用户追问完全不含
   种子词时 agent 盲答，属可接受残余风险
2. 接管（rc=0）的消息不进 agent 会话历史
3. antiword/catppt 需部署机安装（`apt install antiword catdoc`），Windows 不可用
4. rapidocr 模型首次预热需联网；pip 需代理时 systemd 环境注入
5. SDK CDN 下载 60s 超时：慢网络下大文件可能到不了 inbox（SDK 层，非本模块）
6. 旧格式 .doc/.ppt 提取纯文本，无排版结构
