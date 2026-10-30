# Work Assistant

**Turn coding-agent work into traceable tasks, evidence, and reports.**

Work Assistant is a local-first CLI for developers who already use Codex or
Claude Code. Run a task through your own model CLI, keep its execution record,
separate observations from model claims, and produce a self-contained work
report. Feishu delivery is optional and requires explicit authorization.

[Source](https://github.com/xilinshancun/work-assistant/tree/v0.1.0a1) ·
[中文](https://github.com/xilinshancun/work-assistant/blob/v0.1.0a1/README.md) ·
[Quickstart](https://github.com/xilinshancun/work-assistant/blob/v0.1.0a1/docs/quickstart.en.md) ·
[Evidence map](https://github.com/xilinshancun/work-assistant/blob/v0.1.0a1/docs/evidence-map.md)

## Install and see a report

Requires Python 3.9+ on macOS or Linux. This is **Alpha 0.1.0a1**, with no
stable API or automatic state-migration promise. Native Windows is unsupported;
WSL2 has not been validated. The Python runtime uses only the standard library.
Model CLIs are separate installations.

Install the pinned prerelease with [pipx](https://pipx.pypa.io/stable/how-to/install-pipx.html):

```sh
pipx install local-work-assistant==0.1.0a1
wa --version
wa demo --output demo-report
```

Open `index.html` in the `report_path` printed by the demo. No model account,
Feishu application, or Paseo installation is needed. After installation, the demo
runs offline. Its data, executors, and review results are synthetic fixtures.

![Synthetic report with tasks, attributed numbers, and corrections](https://raw.githubusercontent.com/xilinshancun/work-assistant/v0.1.0a1/docs/assets/demo-report.png)

Current pipx has its own Python requirements. For a Python 3.9/3.10 installation,
or if you prefer a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install local-work-assistant==0.1.0a1
.venv/bin/wa demo --output demo-report
```

## Check the installed package

```sh
wa evaluate --output evaluation
wa example --output practice-project
python3 practice-project/count_records.py practice-project/records.txt
```

Both output directories must be new. The evaluator returns a JSON receipt for
four offline checks: two demos, matching report artifacts, and a provider-free
task/report/preview workflow. It does not use your configuration or call a model.
The practice counter prints `5`; its README deliberately says `3`, giving you a
small, inspectable task for your first real executor run.

## Use your own coding CLI

Install and authenticate Codex or Claude Code. Stay in the same directory used
for `wa example` above; it will also hold Work Assistant's configuration and local
state. If you change directories, use an absolute practice-project path:

```sh
wa init
wa doctor
wa project add practice --workspace ./practice-project
wa ask practice 'Read-only: compare the README with the counter and input; cite the mismatch and uncertainties' --routing direct --provider codex
```

For Claude Code, use `--provider claude_code`. Explicit `direct` routing invokes
only the selected executor. Default `auto` routing has additional classification
and planning requirements; `--provider` alone does not bypass them. Independent
review requires the other executor.

Keep the returned task ID. A new `ask` creates a new task; it is not a retry or
recovery command. Replace the placeholders below with that ID and an archive date:

```sh
wa task show TASK_ID
wa report practice --date YYYY-MM-DD
```

The report result's `data.path` locates the HTML, Markdown, and JSON artifacts.
The date is an archive period, not a filter for events from that day.

## What the records mean

- Observations and model claims have distinct attribution. A model's statement
  is not automatically an independently verified fact.
- Corrections create new report snapshots; old snapshots retain their bytes.
- Task writes require intent and project grants. Models cannot grant permission.
- Uncertain execution or delivery is preserved for inspection, without automatic
  replay. This depends on the same local configuration and journal; it is not a
  cross-machine exactly-once guarantee.
- Local storage does not mean offline inference. Real tasks use your model
  service; Feishu calls use your own application and destination.
- Optional chat intake is disabled by default, limited to its configured owner
  and conversations, and needs additional software and tenant validation.

See the [capability matrix](https://github.com/xilinshancun/work-assistant/blob/v0.1.0a1/docs/capabilities.md),
[routing guide](https://github.com/xilinshancun/work-assistant/blob/v0.1.0a1/docs/routing.md),
and [security model](https://github.com/xilinshancun/work-assistant/blob/v0.1.0a1/SECURITY.md).
The offline checks are reproducible evidence of local behavior, not a model-quality,
performance, security, or live-delivery benchmark.

## Contribute

Clone the [source repository](https://github.com/xilinshancun/work-assistant/tree/v0.1.0a1)
to develop adapters, run the regression suite, or inspect implementation evidence.
See [contributing](https://github.com/xilinshancun/work-assistant/blob/v0.1.0a1/CONTRIBUTING.md)
and the [changelog](https://github.com/xilinshancun/work-assistant/blob/v0.1.0a1/CHANGELOG.md).
Report reproducible issues with sanitized commands and output; never attach
credentials, private journals, or company material. Licensed under
[MIT](https://github.com/xilinshancun/work-assistant/blob/v0.1.0a1/LICENSE).
