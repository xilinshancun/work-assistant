# 文档 / Documentation

[首页](../README.md) · [English](../README.en.md)

先跑本地任务与报告，再按需连接飞书。Paseo 和聊天监听都不是核心安装的前提。

## 第一次使用

1. [中文上手](quickstart.md) / [English quickstart](quickstart.en.md)：离线 demo → 安装 → 一个只读任务 → 日报。
2. [能力与依赖](capabilities.md)：必需和可选 CLI、平台支持、验证范围。
3. [排障](troubleshooting.md)：doctor、路径、unknown、回读失败、数据位置。

## 操作参考

| 工作流 | 文档 |
| --- | --- |
| 任务、路由、审查、证据、更正 | [详细操作参考](guide.md) |
| 从已有 HTML/evidence 导入日报 | [导入契约](guide.md#导入已有的本地日报)、[合成示例](../examples/daily-bundle) |
| 飞书配置与发布 | [发布参考](guide.md#飞书预览和真实发布) |
| request-id、文字/卡片、本人身份、引用回复、恢复 | [消息命令](messages.md) |
| 原生高亮块与固定文档树 | [Callout](daily-callouts.md)、[Timeline](timeline-trees.md) |
| 本人 @、快速判断、Paseo 深度处理、收到表情 | [入站服务](inbound.md) |
| 使用已有 Paseo 会话或调度 | [一次性调用配方](../examples/paseo-entry.md) |

## 项目与开发

- [定位、同类选择与路线图](positioning.md)：重点解决的工作流及可验证的下一步。
- [贡献指南](../CONTRIBUTING.md)、[发布检查](releasing.md)、[变更记录](../CHANGELOG.md)。
- [安全边界](../SECURITY.md)、[开发约定](../AGENTS.md)、[最初 V1 设计](../PLAN.md)。PLAN 是设计背景，当前命令以 `--help` 和操作参考为准。

英文覆盖 README 与完整入门路径；深入协议文档当前以中文为主。欢迎改进翻译，但不要把可选功能、未验收平台或模型判断翻译为已验证保证。
