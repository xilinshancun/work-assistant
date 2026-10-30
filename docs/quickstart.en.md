# From a demo to your first work report

[Documentation](README.md) · [中文](quickstart.md)

Start with an artifact you can inspect, then connect one read-only project task. Neither Feishu nor Paseo is required.

## 1. Run without an account

Use Python 3.9+ on macOS or Linux. Native Windows is unsupported; WSL2 is not validated. Once you have the source, no network access or Python package installation is needed for the demo.

```sh
git clone https://github.com/xilinshancun/work-assistant.git
cd work-assistant
python3 -m work_assistant --version
python3 -m work_assistant demo --output out/demo
```

The JSON output includes `report_id`, `report_path`, `hash`, and `mode: offline synthetic; no provider or Feishu validation`. Open `index.html` inside `report_path` with your file manager, `open` on macOS, or `xdg-open` on a Linux desktop.

| File | Purpose |
| --- | --- |
| `index.html` | Accomplishments, numbers, corrections, blockers, next steps |
| `daily-sync.md` | Markdown handoff |
| `evidence.html` / `evidence.json` | Sources, attribution, hashes, verification scope |
| `card-preview.html` | Local summary-card preview |
| `card.json` / `document.json` | Structured payloads for delivery adapters |

The report is self-contained, with no CDN. A synthetic count changes from 12 to 10 while retaining the correction and its reason. Protocol completion and goal verification are displayed separately. Data, execution, and review are simulated; no messages are sent.

Repeating a complete demo returns its existing result. If an interrupted run left an incomplete directory, choose another output directory rather than deleting useful state.

## 2. Check your executor

Run source commands from the repository. To use the tool elsewhere, see installation below.

```sh
python3 -m work_assistant init
python3 -m work_assistant doctor --json
```

`init` creates `config.local.json` and `.wa/`, both ignored by Git, without replacing an existing config. `doctor` checks local help and protocol capabilities; it neither invokes a model nor tests sign-in. A successful diagnostic returns exit code 0 even when optional components are absent. Inspect `readiness` and `workflows` for availability.

- Demo, records, evidence, reports, and publishing dry-runs need no model CLI.
- For the first real task, install and sign in to Codex or Claude Code. Select it with `--routing direct --provider codex` or `--routing direct --provider claude_code`; no DSH or separate planner is needed.
- Default `auto` routing still uses DSH → Codex planner; `--provider` alone does not skip it. DSH + Claude supports classifications that need no escalation; complex escalation needs Codex. Independent review needs both executors.

`local_checks_passed` means local compatibility; `authentication` remains `not tested`. Follow `next_steps` for `incompatible`, `blocked`, or `probe_failed`; do not disable isolation checks to work around a failure. See the [dependency matrix](capabilities.md).

## 3. Run a read-only task

Replace the path with a repository you are authorized to inspect. Start with a non-sensitive practice project:

```sh
python3 -m work_assistant project add my-project --workspace /absolute/path/to/project
python3 -m work_assistant ask my-project 'Read-only: compare the README with the implementation; cite files, issues, and uncertainties' --routing direct --provider codex
```

This step invokes real models using your CLI's account and service usage. Work Assistant supplies no model account. The executor may send the task and code it reads to your configured model service.

Save the returned `task_id` and replace `TASK_ID` below:

```sh
python3 -m work_assistant task show TASK_ID
```

If you only have Claude Code, replace `--provider codex` with `--provider claude_code`. You can use the [synthetic practice project](../examples/first-task.md) instead of your own repository. Direct mode does not select or switch executors automatically, or add write authority; see [routing modes](routing.md).

`protocol_status=completed` means the provider protocol ended; `goal_status=unverified` means the goal has not been independently checked. Inspect the cited files and results. With both Codex and Claude Code configured, `review TASK_ID` runs a read-only review with the opposite provider.

For `needs_user` or `outcome_unknown`, start with the read-only commands in `next_steps` and retain the original task ID. Every `ask` creates a new task; it is not a recovery command.

## 4. Create a local report

Replace `YYYY-MM-DD` with the intended archive date:

```sh
python3 -m work_assistant report my-project --date YYYY-MM-DD
```

The report result contains its directory in `data.path` and its ID in `id`. Open `index.html`. Reports need no model and do not send anything. The date is an archive period: the report includes current tasks and valid claims, not only events from that day.

Use the [evidence and correction commands](guide.md#证据更正与报告) to add observations or correct findings. Do not edit historical reports or rewrite source validation hashes.

Optionally preview publishing without contacting Feishu, replacing `REPORT_ID` with the actual ID:

```sh
python3 -m work_assistant publish REPORT_ID --dry-run
```

Your local workflow is now complete. Read [Feishu setup](guide.md#飞书预览和真实发布) before live delivery, and use your own test destination first.

## Install the wa command

The first PyPI version, `0.1.0a1`, is being prepared. The registry commands below become usable only after that upload is verified; source installation remains available now.

For normal use, [install pipx](https://pipx.pypa.io/stable/how-to/install-pipx.html) and put the CLI in its own environment. Pin the Alpha version explicitly instead of relying on prerelease selection defaults:

```sh
pipx install local-work-assistant==0.1.0a1
wa --version
wa demo --output demo-report
wa evaluate --output evaluation
wa example --output practice-project
```

Installation needs network access. The three local experience commands make no model calls. `evaluate` and `example` require new directories. `example` only copies three practice files; invoking a model still requires an explicit `ask`. Use `wa` after a pipx installation; the package need not be importable by your system Python.

pipx has its own Python version requirements. For Python 3.9/3.10, or as an alternative, use a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install local-work-assistant==0.1.0a1
.venv/bin/wa --version
```

Contributors can install from the cloned repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/wa --version
.venv/bin/wa demo --output out/installed-demo
```

Source builds require setuptools; pip may access the index for build tools. The Python runtime has no third-party packages. Installed `wa` accepts the same arguments as `python3 -m work_assistant` from the source checkout.

Keep configuration and journals outside the pipx environment. Run `wa init` in your own assistant directory; when changing directories, specify the same config explicitly:

```sh
wa --config /absolute/path/to/assistant/config.local.json task list
```

`--config` precedes the subcommand; a relative `--config` argument is resolved against the current working directory. Relative paths inside the configuration and `project --workspace` are resolved against the config directory. Demo state stays in its output directory; normal state defaults to `.wa/` beside the config.

Before upgrading, stop any listener you enabled, back up the configuration and `.wa/`, and read the target version's CHANGELOG. Alpha state compatibility is not guaranteed; downgrading the executable does not restore a database. To select a specific later version, use `pipx install --force local-work-assistant==TARGET_VERSION`. `pipx uninstall local-work-assistant` removes the app environment, not your external journal. See [troubleshooting](troubleshooting.md) for backups.
