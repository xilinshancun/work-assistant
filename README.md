# Work Assistant / 本地工作助理

**把编码 Agent 的工作，变成可追溯的任务、证据和日报。**

面向已经使用 Codex 或 Claude Code 的开发者个人。复用本机 CLI，保存执行记录与来源，更正结论，生成可离线查看的工作报告；需要时，再明确授权投递到飞书。

[English](README.en.md) · [上手指南](docs/quickstart.md) · [文档](docs/README.md) · [贡献](CONTRIBUTING.md) · [变更记录](CHANGELOG.md)

**Alpha · Python 3.9+ · macOS / Linux · MIT · 运行时无第三方依赖**

## 先看一份报告

```sh
git clone https://github.com/xilinshancun/work-assistant.git
cd work-assistant
python3 -m work_assistant demo --output out/demo
```

打开输出 JSON 的 `report_path` 目录中的 **`index.html`**。你会看到任务、数字来源、结论更正和待办；`card-preview.html` 是摘要卡片预览。

获取源码后，这个 demo 完全离线，不需要 pip、模型账号、Paseo 或飞书。所有输入和执行器都是合成 fixture，**不代表真实模型或飞书已接通**。重复执行返回同一份完整 demo。

![离线 demo 生成的真实报告，使用合成数据](docs/assets/demo-report.png)

## 用到自己的项目

先安装并登录自己的 Codex CLI；这是当前最少的真实任务依赖。以下命令在 Work Assistant 仓库内执行，把路径替换为自己的项目：

```sh
python3 -m work_assistant init
python3 -m work_assistant doctor
python3 -m work_assistant project add my-project --workspace /absolute/path/to/project
python3 -m work_assistant ask my-project '只读检查 README 与实现是否一致，列出证据和待确认项' --provider codex
```

默认只读，不自动发消息。`ask` 返回 `task_id`，可用 `task show TASK_ID` 查看结果和记录，用 `report my-project --date YYYY-MM-DD` 生成报告。完整步骤见[上手指南](docs/quickstart.md)，也支持[安装为 `wa` 命令](docs/quickstart.md#安装为-wa-命令)。

Claude Code 可作执行器与独立审查者；DSH 可作快速分类器。**指定执行器不会跳过路由**：Claude-only 暂不能独立完成默认任务流程，复杂路由依赖 Codex planner。[依赖与验证范围](docs/capabilities.md)

## 为什么使用它

- **让工作可以接续。** 任务、路由、提供商 session、执行结果和阻塞原因保存在本地 journal，下一次从已有记录开始。
- **让日报有依据。** 源观测与模型结论分开，保留 attribution、哈希和更正链；旧报告是不可变快照。
- **让“完成”有明确含义。** 协议结束、目标未验证、独立审查通过分别记录，不把模型说“完成了”当成证明。
- **让投递可以核对。** 飞书是可选适配器：预览、明确发送、保存远端 ID、回读检查；结果不明时不自动重发。

```text
你的项目 → 任务 → 已有编码 CLI → 执行记录 / 证据 / 更正
                                            ↓
                                 本地 HTML / Markdown 日报
                                            ↓ 明确授权
                                   可选飞书文档 / 卡片
```

它适合需要交付记录和工作汇总的个人开发者。目前没有团队看板、通用聊天渠道平台或内置定时器；本地保存也不代表模型请求离线。详细说明见[能力边界](docs/capabilities.md)和[项目定位、同类选择与路线图](docs/positioning.md)。

## 按需深入

| 你想做什么 | 从这里开始 |
| --- | --- |
| 安装、跑第一个任务、生成日报 | [中文上手](docs/quickstart.md) / [English quickstart](docs/quickstart.en.md) |
| 记录证据、更正结论、理解恢复状态 | [详细操作参考](docs/guide.md) |
| 导入自己已有的日报收集器 | [导入日报](docs/guide.md#导入已有的本地日报)、[合成 bundle](examples/daily-bundle) |
| 配置飞书、发文字或卡片 | [飞书接入](docs/guide.md#飞书预览和真实发布)、[消息命令](docs/messages.md) |
| 固定文档按日期累积、历史补录 | [Timeline 文档树](docs/timeline-trees.md) |
| 本人 @ 助理、DS 快答与深度分析 | [可选入站](docs/inbound.md)（默认关闭，需额外依赖与租户验收） |
| 出错或结果不明 | [排障](docs/troubleshooting.md)、[安全模型](SECURITY.md) |

## 一起改进

欢迎先跑 demo，再用一个只读项目任务反馈卡住的步骤。提交 [Bug](https://github.com/xilinshancun/work-assistant/issues/new/choose) 时附版本、平台、脱敏命令和实际结果；不要上传私人 journal、凭证或公司材料。功能建议请说明哪一步工作流程因此变好。

开发、测试和 PR 约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)。[MIT License](LICENSE)。
