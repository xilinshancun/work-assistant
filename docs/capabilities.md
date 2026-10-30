# 能力、依赖与验证范围 / Capability boundaries

[文档首页](README.md)

当前为 Alpha。这里区分“代码提供了入口”“本地兼容检查通过”和“在你的账号、平台与租户实际通过”，三者不等价。

## 平台与安装

| 环境 | 范围 |
| --- | --- |
| macOS、Linux，Python 3.9+ | 核心目标平台，使用 POSIX `fcntl` / `flock` 和 SQLite |
| Python 3.9 / 3.12 | 自动化测试矩阵；其他 3.9+ 版本未逐一验证 |
| 原生 Windows | 不支持；不是仅缺少 UI，核心任务锁依赖 POSIX |
| WSL2 | 尚未验收，不承诺等同 Linux CI 结果 |
| 无网络环境 | 已获取源码后的 demo、证据、报告、dry-run 可用；安装构建工具可能需要网络 |

运行时只有 Python 标准库。安装构建需要 setuptools。模型 CLI、飞书 CLI 和 Paseo 均由使用者单独安装，不捆绑账号或服务额度。

## 按工作流选择依赖

| 你拥有的工具 | 能做什么 | 限定 |
| --- | --- | --- |
| 只有 Python | 离线 demo、记录与证据、不可变报告、发布预览 | 不能执行真实模型任务 |
| 只有 Codex | 显式 direct 执行；也可提供 auto 的 planner + 执行 | auto 中 DSH 可缺省，失败后升级一次；登录需自行验证 |
| 只有 Claude Code | `--routing direct --provider claude_code` 完成显式任务执行 | 默认 auto 仍不能独立跑通；独立 review 需要 Codex |
| DSH + Claude Code | 显式 direct；或 DSH 接受且不需升级的 auto 路由 | auto 无 Codex 时无法完成复杂/失败分类的 GPT 升级 |
| Codex + Claude Code | 两种执行器、相反提供商独立审查 | DSH 可选；审查判断不等于科学复现 |
| 飞书自建应用 + HTTP 或官方 lark-cli | 原生文档、摘要卡片、journal、回读与恢复 | 需要自己的应用权限和目的地；不提供共享机器人 |
| 官方 lark-cli + 用户授权 | 显式本人身份发送、本人发送方核验 | 需额外 OAuth scope；user 卡片不支持原位 update-card |
| DSH / Paseo / lark-cli + 入站配置 | 本人私聊、允许的测试群 @、深度处理、引用答复与收到表情 | 默认关闭、仅本人发送者；详见 [inbound](inbound.md) 的精确依赖与权限 |

`--routing auto` 是默认值，单独使用 `--provider` 仍不跳过 DSH → Codex planner。显式 `--routing direct` 要求指定执行器，省去分类调用，但不省去授权或执行隔离检查；不会给任务添加 grant。`doctor` 的 `workflows.ask.paths` 分别列出两种方式，Claude-only 显示 `direct_only`，不是 auto 可用。`local_checks_passed` 和 `direct_only` 均不证明已登录、模型可用或服务端权限通过。详见[执行方式](routing.md)。

## 已实现与仍需验收

| 能力 | 仓库内验证 | 使用者仍需验证 |
| --- | --- | --- |
| 任务、授权、执行协议、恢复 | 注入进程 fixture、超时/崩溃/并发测试 | 所用 CLI 版本、登录、实际目标结果 |
| 证据、派生输入、更正与报告 | 确定性 demo、哈希/数值/来源链测试 | 观测是否真实、结论是否科学有效 |
| 安装包 | wheel / sdist 构建与仓库外 smoke 检查 | 自己的 OS/Python/构建工具环境 |
| 飞书文档与消息 | 合成 HTTP/CLI、create/update/no-op/unknown 测试 | 自己租户的权限、远端内容与客户端显示 |
| 入站与 reaction | 合成事件/模型/消息/Paseo 测试 | 官方事件流 ready、投递语义与真实回复链 |

维护者曾在隔离的合成项目及自有飞书环境验证部分真实链路，见[详细范围](guide.md#已验证的实际链路)。这不能替代另一个安装环境的验收，也不构成性能或稳定性基准。

## 明确边界

- 本地 journal 不是云端同步。多会话去重需要同一配置、同一状态库；复制数据库不能提供跨机器互斥。
- `reported` 是归属明确的陈述，`protocol completed` 只是协议结束；两者都不能自动提升为目标已验证。
- 本地优先不等于全部离线。真实模型与飞书调用会离开本机；配置、日志和证据可能包含敏感材料。
- 权限由用户配置和明确操作决定，模型不能授予自己权限。本工具不是恶意代码的通用隔离器。
- 入站深度 Agent 的只读约定和层级约束不是 OS 沙箱保证；官方事件流没有本工具级历史重放，停机时可能漏消息。
- 飞书 GET 能核对的范围以各适配器回执为准；默认卡片回读不完整证明按钮 URL/样式。`verified` 不表示收件人已读或内容事实正确。
- 不内置 scheduler，不接管已有聊天历史或任意频道，也没有多租户团队权限管理。可选入站是显式前台进程。
- `.local/` 私有部署脚本和个人 Skill 不属于公共安装接口。公共路径是 `wa`、`python -m work_assistant.*`、`examples/` 与本目录文档。

不确定结果应先核对，不能通过换请求 ID、删除 journal、改用另一种发送方式来假装恢复。参见 [SECURITY.md](../SECURITY.md) 与[排障](troubleshooting.md)。
