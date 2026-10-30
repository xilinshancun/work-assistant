# 从 demo 到自己的第一份日报

[文档首页](README.md) · [English](quickstart.en.md)

目标是先看到产物，再接入一个只读项目任务。全程无需配置飞书或 Paseo。

## 1. 不用账号，先运行 demo

需要 macOS 或 Linux、Python 3.9+。原生 Windows 不支持；WSL2 尚未验收。源码获取后不需要联网或安装 Python 包。

```sh
git clone https://github.com/xilinshancun/work-assistant.git
cd work-assistant
python3 -m work_assistant --version
python3 -m work_assistant demo --output out/demo
```

输出包含 `report_id`、`report_path`、`hash`，以及 `mode: offline synthetic; no provider or Feishu validation`。在文件管理器中打开 `report_path` 里的 `index.html`；macOS 也可用 `open`，Linux 桌面可用 `xdg-open` 打开该文件。

| 文件 | 用途 |
| --- | --- |
| `index.html` | 完成事项、数字、发现与更正、阻塞、下一步 |
| `daily-sync.md` | 便于阅读和交接的 Markdown |
| `evidence.html` / `evidence.json` | 来源、归属、哈希与验证范围 |
| `card-preview.html` | 摘要卡片本地预览 |
| `card.json` / `document.json` | 投递适配器使用的结构化内容 |

报告全部自包含，无 CDN。示例里一个数字由 12 更正为 10，保留原因和被替代结论。任务完成与目标未验证也分别显示。数据、模型执行和审查均为合成演示，不会发送任何消息。

再次执行相同命令返回已有完整 demo；如果上次被中断而目录不完整，使用新的 `--output` 目录，不删除可能有用的状态。

## 2. 检查本机执行器

后续源码命令仍在本仓库目录执行。若想从任意目录使用，先看下方安装方法。

```sh
python3 -m work_assistant init
python3 -m work_assistant doctor --json
```

`init` 创建本地 `config.local.json` 和 `.wa/`，不会覆盖已有配置。两者均已被 Git 忽略。`doctor` 检查本地 CLI 的 help/协议能力，不调用模型、不测试账号登录。JSON 输出成功时退出码为 0，即使某些可选功能未配置；判断能力应读取 `readiness` 和 `workflows`。

- **只想看 demo、管理证据或生成报告**：不需要任何模型 CLI。
- **第一次真实任务**：建议已安装并登录 Codex。没有 DSH 时，会升级一次到 Codex planner，再由 Codex 执行。
- **使用 Claude Code 执行或独立审查**：再配置 Claude Code。只有 Claude Code 而没有 DSH/Codex 不能完成当前路由；DSH + Claude 仅有直接分类路径，复杂升级仍需要 Codex。

`local_checks_passed` 只表示本地接口兼容，认证仍是 `not tested`。`incompatible`、`blocked` 或 `probe_failed` 请按 `next_steps` 处理；不要通过关闭隔离检查来绕过失败。完整组合见[能力表](capabilities.md)。

## 3. 运行一个只读任务

把路径换成自己有权访问的代码仓库，首次建议用不含敏感数据的练习项目：

```sh
python3 -m work_assistant project add my-project --workspace /absolute/path/to/project
python3 -m work_assistant ask my-project '只读检查 README 与实现是否一致；列出文件依据、问题和无法确认的部分' --provider codex
```

这是会调用真实模型的步骤，使用你自己的 CLI 登录及其服务用量。Work Assistant 不提供模型账号。执行器可能把任务和读取到的代码交给所配置的模型服务。

保存返回的 `task_id`。以下命令的 `TASK_ID` 替换为该值：

```sh
python3 -m work_assistant task show TASK_ID
```

`protocol_status=completed` 表示提供商协议结束，`goal_status=unverified` 表示目标尚未独立核验。查看实际文件依据和结果后，再决定是否需要独立审查。已配置 Codex 和 Claude Code 时，可运行 `review TASK_ID`，由相反提供商只读检查。

遇到 `needs_user` 或 `outcome_unknown`，先运行输出 `next_steps` 中的只读检查命令，保留原任务 ID。不要反复调用 `ask` 来恢复原任务：每次 `ask` 都会创建一个新任务。

## 4. 生成本地日报

将 `YYYY-MM-DD` 换为实际归档日期：

```sh
python3 -m work_assistant report my-project --date YYYY-MM-DD
```

报告输出中 `data.path` 是产物目录，`id` 是报告 ID。打开 `index.html` 查看结果；报告不需要模型，也不会发送。日期是归档周期，报告包含当前任务和有效结论，不仅是当天发生的事件。

需要加入自己的观测或更正数字时，使用[证据与更正命令](guide.md#证据更正与报告)。不要直接修改旧报告或源校验 hash。

可选的发布预览不访问飞书：

```sh
python3 -m work_assistant publish REPORT_ID --dry-run
```

这里将 `REPORT_ID` 换为真实报告 ID。到此已完成本地工作流。真正投递前再阅读[飞书配置](guide.md#飞书预览和真实发布)，先使用自己的测试目的地。

## 安装为 wa 命令

从仓库根目录安装，虚拟环境避免修改系统 Python：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/wa --version
.venv/bin/wa demo --output out/installed-demo
```

构建时需要 setuptools；pip 可能为构建工具访问包索引。安装后运行时无第三方依赖。这是源码安装路径，不依赖同名 PyPI 包。

激活环境后，`wa` 等价于 `python3 -m work_assistant`。从任意目录运行时，显式指定同一配置，以免创建互不关联的状态库：

```sh
source /absolute/path/to/work-assistant/.venv/bin/activate
wa --config /absolute/path/to/work-assistant/config.local.json task list
```

`--config` 放在子命令之前。配置中的相对路径和 `project --workspace` 相对配置文件目录解析。演示输出只在指定输出目录中；真实状态默认在配置旁的 `.wa/`。备份方法和问题诊断见[排障指南](troubleshooting.md)。
