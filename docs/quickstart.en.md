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
- For the first real task, install and sign in to Codex separately. Without DSH, routing escalates once to the Codex planner, then Codex executes.
- Claude Code can execute tasks and independently review Codex work. Claude alone cannot complete current routing. DSH + Claude supports direct classification but needs Codex for complex escalation.

`local_checks_passed` means local compatibility; `authentication` remains `not tested`. Follow `next_steps` for `incompatible`, `blocked`, or `probe_failed`; do not disable isolation checks to work around a failure. See the [dependency matrix](capabilities.md).

## 3. Run a read-only task

Replace the path with a repository you are authorized to inspect. Start with a non-sensitive practice project:

```sh
python3 -m work_assistant project add my-project --workspace /absolute/path/to/project
python3 -m work_assistant ask my-project 'Read-only: compare the README with the implementation; cite files, issues, and uncertainties' --provider codex
```

This step invokes real models using your CLI's account and service usage. Work Assistant supplies no model account. The executor may send the task and code it reads to your configured model service.

Save the returned `task_id` and replace `TASK_ID` below:

```sh
python3 -m work_assistant task show TASK_ID
```

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

From the repository root, install into a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/wa --version
.venv/bin/wa demo --output out/installed-demo
```

Building requires setuptools; pip may access the package index for build tools. The installed runtime has no third-party dependencies. This installs from source and does not rely on a similarly named PyPI package.

After activating the environment, `wa` is equivalent to `python3 -m work_assistant`. When changing directories, specify the same config explicitly to keep using the same state:

```sh
source /absolute/path/to/work-assistant/.venv/bin/activate
wa --config /absolute/path/to/work-assistant/config.local.json task list
```

`--config` precedes the subcommand. Relative config paths and `project --workspace` are resolved against the config directory. Demo state stays in its output directory; normal state defaults to `.wa/` beside the config. See [troubleshooting](troubleshooting.md) for backups and diagnostics.
