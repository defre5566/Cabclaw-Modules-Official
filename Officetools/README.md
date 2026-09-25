# Officetools

文档解读能力模块：把微信收到的 **pdf / office / 图片** 文件解析为「概览 + 全文 markdown」，
产物供助手（agent）承接后续总结、翻译、问答等具体需求。

## 能力

| 格式 | 方式 |
|---|---|
| .pdf | pdfplumber（文本+表格）；扫描件走 RapidOCR |
| .docx / .xlsx / .pptx / .xls | python-docx / openpyxl / python-pptx / xlrd |
| .doc / .ppt | antiword / catppt（需部署机安装：`apt install antiword catdoc`） |
| .jpg / .png / .webp 图片 | RapidOCR |

## 用法（微信端）

1. 发文件给机器人（落 inbox）
2. 说「解读」→ 收到概览 + 全文产物路径；或「解读 文件名.pdf」点名
3. 之后直接问文件内容（"这个文件里说了什么"），助手按产物回答

## 设置（web 模块参数区）

- `ocr_on`：OCR 层开关（扫描件/图片识别；开启后守护周期按需准备 OCR 组件，关闭不卸载）
- `file_auto_on`：文件到达自动解读（默认关：说"解读"触发）
- `max_file_mb` / `max_pages` / `ocr_max_pages`：解析上限
- `overview_max_chars` / `retention_days`：概览限长 / 产物保留天数

## 依赖自举

模块依赖装在数据区 `pylibs/`，启用后守护周期（5 分钟）自动准备。核心解析层与 OCR 层独立，OCR 只在 `ocr_on=true` 时准备。依赖优先从模块数据区 `wheelhouse/core` 或 `wheelhouse/ocr` 的本地 wheel 安装；未预置时先用清华 PyPI 镜像，失败回退 PyPI，均仅安装预编译 wheel。源码形态与单文件版均无需用户填写源地址；网络不可达时明确报告安装失败并退避重试。

开发/离线验收可用 `OFFICETOOLS_WHEELHOUSE=<包含 core/、ocr/ 子目录的路径>` 指定本地 wheel 缓存；预置目录必须有完整的对应平台依赖闭包，否则转在线镜像。
手动预热：`officetools_worker.py --bootstrap`。

## 测试

```bash
OFFICETOOLS_PYLIBS=<pylibs 目录> \
CABCLAW_HOST=<cabclaw 项目根> \
    python -m pytest Officetools/tests/
```

未设置 `CABCLAW_HOST` 时跳过（缺主项目 common）。

## 详见

`spec.md`（行为规范与边界）/ `agents.md`（助手操作指引）
