# Work Assistant / 本地工作助理

**让编码 Agent 的工作留下可接续、可核验、可交付的记录。**

Work Assistant 是面向 Codex / Claude Code 使用者的本地优先开发者工作助理。通过一次性 CLI 串起任务、执行、审查、证据、更正和日报；复用你已有的模型 CLI 与登录，需要时再把确定的内容投递到飞书。

[English](README.en.md) · [上手](docs/quickstart.md) · [证据地图](docs/evidence-map.md) · [文档](docs/README.md) · [贡献](CONTRIBUTING.md) · [llms.txt](llms.txt)

[![CI：测试与分发安装](https://github.com/xilinshancun/work-assistant/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/xilinshancun/work-assistant/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

| 项目事实 | 当前范围 |
| --- | --- |
| 状态与版本 | **Alpha，源码版本 0.1.0**；见 [CHANGELOG](CHANGELOG.md)，不承诺稳定 API 或状态迁移 |
| 平台 | Python **3.9+**，macOS / Linux；原生 Windows 不支持，WSL2 未验收 |
| Python 依赖 | 运行时无第三方 Python 包；真实任务仍需要外部模型 CLI |
| 安装与命名 | 从源码 `pip install .`；分发名 `local-work-assistant`、模块 `work_assistant`、命令 `wa` |
| 数据与网络 | journal、证据和报告保存在本地；真实任务会调用你配置的模型服务 |

版本与依赖以 [pyproject.toml](pyproject.toml) 为准，完整验证范围见[能力表](docs/capabilities.md)。

截至 **2026-10-30，本项目尚未发布到 PyPI**。请从源码安装，不要用 `pip install local-work-assistant` 代替；同名包不代表本项目。

## 什么时候用它

- **项目检查与交接**：让一个只读任务留下执行结果、文件依据与待确认项，下一次从原任务记录继续。
- **有来源的工作汇总**：把任务和明确导入的观测整理成日报；数字修订保留更正链，旧报告保持不变。
- **可核对的飞书交付**：先预览，再按授权发送；保存远端 ID 与回读结果，避免把未知结果当成“没发过”。

记录来自本工具创建的任务或显式导入的材料，**不会自动抓取所有 Codex / Claude 聊天历史**。如果主要需求是通用聊天渠道、团队看板或托管 Agent 平台，可参考[同类选择](docs/positioning.md#怎样选择同类项目)。

## 先看一份报告

```sh
git clone https://github.com/xilinshancun/work-assistant.git
cd work-assistant
python3 -m work_assistant demo --output out/demo
```

打开输出 JSON 的 `report_path` 目录中的 **`index.html`**。你会看到任务、数字来源、结论更正和待办；`card-preview.html` 是摘要卡片预览。

获取源码后，这个 demo 完全离线，不需要 pip、模型账号、Paseo 或飞书。所有输入和执行器都是合成 fixture，**不代表真实模型或飞书已接通**。重复执行返回同一份完整 demo。

![Synthetic Lab 离线报告：任务结果、数字来源与更正记录](docs/assets/demo-report.png)

图中执行、独立审查和业务数据均为合成示例。报告还包含 `daily-sync.md`、`evidence.html`、`evidence.json`、`card.json` 和 `document.json`。

## 用到自己的项目

先安装并登录自己的 Codex CLI；这是当前最少的真实任务依赖。以下命令在 Work Assistant 仓库内执行，把路径替换为自己的项目：

```sh
python3 -m work_assistant init
python3 -m work_assistant doctor
python3 -m work_assistant project add my-project --workspace /absolute/path/to/project
python3 -m work_assistant ask my-project '只读检查 README 与实现是否一致，列出证据和待确认项' --provider codex
```

默认只读，不自动发消息。保存 `ask` 返回的 `task_id`，将下方 `TASK_ID` 换为它，将日期换为实际归档日期：

```sh
python3 -m work_assistant task show TASK_ID
python3 -m work_assistant report my-project --date YYYY-MM-DD
```

`report` 输出的 `data.path` 指向报告目录。日期是归档周期，报告包含当前任务与有效结论，并非只筛选当天事件。完整步骤见[上手指南](docs/quickstart.md)，也支持[安装为 `wa` 命令](docs/quickstart.md#安装为-wa-命令)。

Claude Code 可作执行器与独立审查者；DSH 可作快速分类器。**指定执行器不会跳过路由**：Claude-only 暂不能独立完成默认任务流程，复杂路由依赖 Codex planner。[依赖与验证范围](docs/capabilities.md)

## 核心能力与核验入口

| 能力 | 可检查的行为 | 实现与验证 |
| --- | --- | --- |
| 持久任务与交接 | 保留 task/run/session、结果和阻塞；失败后的下一步指向原任务 | [任务入口](work_assistant/assistant.py)、[上手测试](tests/test_onboarding.py) |
| 有来源的结论与更正 | 观测与模型陈述分开；数字核对 value/unit；更正不重写旧快照 | [证据模块](work_assistant/evidence.py)、[报告测试](tests/test_evidence_reports.py) |
| 可区分的完成状态 | `protocol_status`、`goal_status`、独立 review 分开，不把模型陈述当证明 | [执行器编排](work_assistant/runner.py)、[审查回归](tests/test_review_regressions.py) |
| 明确授权 | 默认 read；write 需 intent + grant；模型输出不能新增权限 | [策略](work_assistant/policy.py)、[策略测试](tests/test_policy.py) |
| 可核对的投递 | 发送前保存 intent；保存远端 ID；结果不明不自动重放 | [消息实现](work_assistant/messages.py)、[消息测试](tests/test_messages.py) |
| 可搬走的产物 | 自包含 HTML / Markdown / JSON；无凭证 demo；wheel/sdist 仓库外安装检查 | [demo](work_assistant/demo.py)、[分发检查](scripts/package_smoke.py)、[CI](.github/workflows/test.yml) |

```text
你的项目 → 任务 → 已有编码 CLI → 执行记录 / 证据 / 更正
                                            ↓
                                 本地 HTML / Markdown 日报
                                            ↓ 明确授权
                                   可选飞书文档 / 卡片
```

## 独立评估这个项目

在仓库根目录运行，无需模型、飞书或 Paseo 账号：

```sh
python3 scripts/evaluate.py --output out/evaluation
```

评估入口使用临时合成配置检查 CLI、两个独立 demo 的一致性、报告产物与无凭证发布预览，输出 JSON 回执；不会把自己的配置或项目交给模型。它验证本地流程，**不验证真实模型质量、飞书权限或客户端显示**。精确检查范围、关键测试命令与失败语义见[证据地图](docs/evidence-map.md)。

供 AI 检索的 [llms.txt](llms.txt) 提供精简事实与公开文档索引；与人类读者使用同一套源码和验证证据。

## 边界与常见问题

- **零依赖是否等于不用模型 CLI？** 不是。只有 demo、证据、报告和 dry-run 不需要模型；真实任务至少需要能覆盖路由与执行的 CLI 组合，最少推荐 Codex。
- **数据是不是全程不出本机？** 本地存储是默认；真实模型和飞书调用仍会离开本机。不要把 local-first 当成离线推理。
- **“不重放”保证到哪里？** 同一配置和共享 journal 内的恢复契约；不是跨机器 exactly-once。复制数据库不能提供跨副本互斥。
- **`verified` 是什么？** 具体检查范围写在回执里。消息回读不证明业务事实正确或对方已读；默认卡片回读不完整核验 URL 和样式。
- **能自动定时或面向所有人聊天吗？** 不内置调度器；可选入站默认关闭，目前限定本人和允许的会话，需额外依赖与租户验收。

见[能力边界](docs/capabilities.md)、[安全模型](SECURITY.md)和[路线图](docs/positioning.md#下一阶段按什么顺序做)。

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
