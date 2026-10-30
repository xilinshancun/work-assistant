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
- **第一次真实任务**：安装并登录 Codex 或 Claude Code，使用 `--routing direct --provider codex` 或 `--routing direct --provider claude_code` 明确选择执行器，不需要 DSH 或额外 planner。
- **自动分类与独立审查**：默认 `auto` 仍走 DSH → Codex planner，单独指定 `--provider` 不跳过它。DSH + Claude 的 auto 仅支持不需升级的分类；复杂升级需要 Codex。独立审查需要两种执行器。

`local_checks_passed` 只表示本地接口兼容，认证仍是 `not tested`。`incompatible`、`blocked` 或 `probe_failed` 请按 `next_steps` 处理；不要通过关闭隔离检查来绕过失败。完整组合见[能力表](capabilities.md)。

## 3. 运行一个只读任务

把路径换成自己有权访问的代码仓库，首次建议用不含敏感数据的练习项目：

```sh
python3 -m work_assistant project add my-project --workspace /absolute/path/to/project
python3 -m work_assistant ask my-project '只读检查 README 与实现是否一致；列出文件依据、问题和无法确认的部分' --routing direct --provider codex
```

这是会调用真实模型的步骤，使用你自己的 CLI 登录及其服务用量。Work Assistant 不提供模型账号。执行器可能把任务和读取到的代码交给所配置的模型服务。

保存返回的 `task_id`。以下命令的 `TASK_ID` 替换为该值：

```sh
python3 -m work_assistant task show TASK_ID
```

只有 Claude Code 时，把命令中的 `--provider codex` 换成 `--provider claude_code`。不想使用自己的项目，可以先做[合成小项目练习](../examples/first-task.md)。direct 不会自动挑选或切换执行器，也不添加写权限；详见[两种执行方式](routing.md)。

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

首个 PyPI 版本 `0.1.0a1` 正在准备发布。以下发行版命令仅在该版本上传并核实后可用；目前可用后面的源码安装方式。

日常使用推荐 [pipx](https://pipx.pypa.io/stable/how-to/install-pipx.html)，将 CLI 放在独立环境中。显式固定 Alpha 版本，不依赖包管理器是否默认选择预发布版：

```sh
pipx install local-work-assistant==0.1.0a1
wa --version
wa demo --output demo-report
wa evaluate --output evaluation
wa example --output practice-project
```

`pipx` 需先单独安装；安装时需要网络，以上三个本地体验命令不调用模型。`evaluate` 和 `example` 要求新目录。`example` 只复制三个练习文件，实际调用模型要由你再执行 `ask`。pipx 安装后用 `wa`，不需要让系统 Python 能 import 此包。

pipx 本身有独立的 Python 版本要求。只有 Python 3.9/3.10，或不想使用 pipx 时，可选择自己的虚拟环境：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install local-work-assistant==0.1.0a1
.venv/bin/wa --version
```

需要修改源码或参与贡献时，在克隆的仓库根目录安装：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/wa --version
.venv/bin/wa demo --output out/installed-demo
```

源码构建需要 setuptools；pip 可能为构建工具访问包索引。安装后 Python 运行时无第三方包。安装后的 `wa` 与源码目录中的 `python3 -m work_assistant` 使用相同命令参数。

配置和 journal 保存在运行时选择的位置，不放进 pipx 环境。先在自己的工作助理目录运行 `wa init`；从其他目录操作时显式使用同一配置：

```sh
wa --config /absolute/path/to/assistant/config.local.json task list
```

`--config` 放在子命令之前；相对的 `--config` 按当前工作目录解析。配置内部相对路径和 `project --workspace` 相对配置文件目录解析。演示只写指定输出目录，真实状态默认在配置旁的 `.wa/`。

升级前先停下自己启用的监听器，备份配置和 `.wa/`，阅读目标版本 CHANGELOG。Alpha 不保证状态向后兼容；降级可执行文件不等于恢复数据库。升级到明确版本时使用 `pipx install --force local-work-assistant==目标版本`；卸载命令是 `pipx uninstall local-work-assistant`，不会替你删除外部 journal。备份细节见[排障指南](troubleshooting.md)。
