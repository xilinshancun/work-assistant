# 排障 / Troubleshooting

[文档首页](README.md) · [English quickstart](quickstart.en.md)

先保留输出中的 task/run/report/action ID，再检查原记录。不要把反复执行 `ask`、换 request-id 或删数据库当作恢复方法。

## Doctor 的结果怎样看

```sh
python3 -m work_assistant --config config.local.json doctor --json
```

| readiness | Meaning / 处理 |
| --- | --- |
| `not_installed` | 配置的二进制未找到；检查对应 provider 的 `argv` 和 `env.PATH` |
| `probe_failed` | 本地 help 执行失败、超时或输出无效；检查安装与该 CLI 自身的 help |
| `incompatible` | CLI 缺少必需参数；核对版本及诊断列出的能力 |
| `blocked` | Codex MCP 隔离预检失败；先修复兼容性，不绕过预检 |
| `local_checks_passed` | 本地接口检查通过；**不代表认证或模型访问通过** |

Doctor 不调用模型。正常输出 JSON 的退出码是 0；它是诊断，不是自动部署的健康门槛。查看 `workflows.ask.paths` 与 `gpt_fallback`，不要要求所有可选 provider/飞书都就绪。没有模型 CLI 时，`workflows.offline` 仍然可用。

## 常见症状

| 症状 | 下一步 |
| --- | --- |
| `No module named work_assistant` | 源码模式进入仓库根目录；或在 venv 安装后使用该环境的 `wa`。别依赖另一个目录的 PYTHONPATH |
| `wa: command not found` | 用 `.venv/bin/wa`，或激活安装它的 venv |
| 换目录后看不到原任务 | 明确使用同一个 `--config /absolute/path/config.local.json`；状态默认跟随配置位置 |
| 缺 DSH | Codex 可承担一次 planner 升级；只有 Claude 时还没有完整默认路由 |
| 已登录但任务仍失败 | Doctor 不验登录；核对提供商自身账号、配置和返回错误。不要导出凭证到 issue |
| demo 输出目录不完整 | 换新的输出目录保留原现场；完整目录重复执行会返回旧结果 |
| 报告出现 `goal unverified` | 正常边界：执行协议完成不证明目标实现。查看实际产物，必要时做独立 review |
| 源 HTML 哈希不符 | 让源采集器重新生成并验证 bundle；不要手改 validation 或 hash 来通过导入 |
| 卡片 GET 核对未通过 | 已知消息可能已发送；用原 action 的 `inspect` 只读核对，不重新发一条 |
| Windows import/锁失败 | 原生 Windows 不支持；WSL2 也尚未验收 |

## 未知结果：检查与重试分开

```sh
python3 -m work_assistant task show TASK_ID
python3 -m work_assistant reconcile RUN_ID
```

把占位 ID 换成原回执中的 ID。不带 resolution 的 reconcile 不会启动模型或重发。`ask` 返回的 `next_steps` 也只提供检查命令。

普通消息使用独立入口：

```sh
python3 -m work_assistant.messages --config config.local.json inspect ACTION_ID
```

`outcome_unknown` 代表请求可能生效。缺本地远端 ID 不能证明远端不存在；有明确审计理由的人工 resolve 与后续新操作须遵循[恢复参考](guide.md#未知结果与恢复)或[消息恢复](messages.md)。

退出码：正常命令为 0；输入错误、failed/needs_user/unknown 或审查未通过为 2；键盘中断为 130；SIGTERM 为 143。Doctor 对缺失可选依赖仍返回成功的诊断对象。

## 数据与备份

真实配置默认 `config.local.json`，状态默认配置旁 `.wa/`，其中包括 SQLite、日志、证据和报告。demo 独立写到 `--output` 目录。它们都可能含私人信息，不属于可上传的错误附件。

升级前先正常停止使用该状态目录的进程，确认没有活跃任务，再备份配置与**整个状态目录**（包括 SQLite 的配套文件）。不要只复制正在写入的 `.sqlite3` 主文件，不要在任务运行时替换状态库。把备份当作敏感材料保存；不要并行运行两个副本来恢复同一远端操作。

## 提交有用的反馈

提供 `--version`、OS/Python/CLI 版本、脱敏后的命令、预期与实际结果、是否能用合成数据复现。Doctor 输出也需先人工检查路径和身份字段；不要附完整配置、数据库、provider 原始日志或业务源文件。

[Bug 入口](https://github.com/xilinshancun/work-assistant/issues/new/choose) · [漏洞私密报告](https://github.com/xilinshancun/work-assistant/security/advisories/new) · [安全策略](../SECURITY.md)
