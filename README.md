# wechat-claw 官方模块源

> wechat-claw（微信主动推送）的官方模块仓库：业务模块独立于主项目高频发布，从源安装即用（web 或拷贝到 `modules/`），模块自带设置声明与自测。

wechat-claw（微信主动推送）的**官方模块仓库**。模块更新频率高，独立于主项目（wechat-claw）发布；主项目只提供平台（调度引擎/公共库/模块管理），业务能力全部由这里的模块提供。

## 目录结构

```
wechat-claw_modules_official/
├── manifest.json   # 模块清单（名称/版本/描述/依赖）
└── <module>/       # 每模块一个目录
    ├── module.json # 模块自描述（enabled/schedule/retry/inbound/settings_schema/settings）
    ├── <name>_worker.py
    ├── 规范.md      # 业务行为自述（agent 会话依据）
    ├── agents.md    # agent 维护指引（写/改/查任务）
    ├── README.md    # 部署者使用说明
    └── tests/       # 模块自带测试（注入宿主运行）
```

## 安装模块

```bash
# 1. 拷贝模块目录到主项目的 modules/（主项目需先安装，提供 common 公共库）
cp -r <module> <wechat-claw>/modules/

# 2. 启用（token 由 register 生成，模块包不含 token）
python3 <wechat-claw>/modules/register.py --enable <module>
```

> 依赖：模块 worker 使用主项目 `modules/common/`（任务解析/推送/日志等），**请先安装主项目**。

## 模块开发

- 开发规范见主项目 `docs/开发文档-04-模块开发规范.md`（worker 骨架 / module.json 格式 / settings_schema / 铁律）
- 模块设置（settings_schema）：模块自声明，web 模块参数区自动渲染；保存走 register.update_module（后端校验器清洗）

## 测试

模块自带测试（`<module>/tests/`），运行时依赖主项目 `modules/common/`，需注入宿主路径：

```bash
WECHAT_CLAW_HOST=<wechat-claw 项目根> \
    python3 -m pytest wechat-claw_modules_official/todo/tests/
```

未设置 `WECHAT_CLAW_HOST` 时测试自动跳过（提示缺少主项目 common）。

## manifest.json 格式

```json
{
  "schema_version": 1,
  "modules": [
    {
      "name": "<模块名>",
      "version": "0.1.0",
      "purpose": "一句话用途",
      "data_sources": ["internal"],
      "files": ["module.json", "<name>_worker.py", "规范.md", "README.md"],
      "requires": {"python": ">=3.11"}
    }
  ]
}
```
