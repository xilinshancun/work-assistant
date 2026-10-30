# Work Assistant

**Make coding-agent work traceable, reviewable, and ready to hand off.**

A local-first developer work assistant for Codex / Claude Code users. A one-shot CLI connects tasks, execution, review, evidence, corrections, and reports. It reuses your installed model CLIs and accounts, with optional, explicitly authorized Feishu delivery.

[中文](README.md) · [Quickstart](docs/quickstart.en.md) · [Evidence map](docs/evidence-map.md) · [Docs](docs/README.md) · [Contributing](CONTRIBUTING.md) · [llms.txt](llms.txt)

[![CI: tests and distribution installs](https://github.com/xilinshancun/work-assistant/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/xilinshancun/work-assistant/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

| Project fact | Current scope |
| --- | --- |
| Status and version | **Alpha, source version 0.1.0**; see [CHANGELOG](CHANGELOG.md). No stable API or state-migration promise |
| Platforms | Python **3.9+**, macOS / Linux; native Windows unsupported, WSL2 unverified |
| Python dependencies | No third-party Python runtime packages; real tasks still require external model CLIs |
| Installation and names | Install from source with `pip install .`; distribution `local-work-assistant`, module `work_assistant`, command `wa` |
| Storage and network | Journals, evidence, and reports stay local; real tasks call your configured model service |

[pyproject.toml](pyproject.toml) defines package metadata. See the [capability matrix](docs/capabilities.md) for validation scope.

**This project has not been published to PyPI as of 2026-10-30.** Install from source; do not substitute `pip install local-work-assistant`. A same-name registry package is not evidence of this project's identity.

## When to use it

- **Project checks and handoffs:** retain the result, file evidence, and open questions from a read-only task, then return to the original record.
- **Sourced work reports:** summarize tasks and explicitly imported observations; preserve corrections without rewriting old reports.
- **Inspectable Feishu delivery:** preview, authorize, send, and retain remote IDs and readback results. An uncertain outcome is not treated as an unsent request.

Records come from tasks created through this tool or explicitly imported material. **It does not automatically collect all Codex / Claude chat histories.** For general chat channels, team dashboards, or a hosted agent platform, see [alternative project scopes](docs/positioning.md#怎样选择同类项目).

## See a report first

```sh
git clone https://github.com/xilinshancun/work-assistant.git
cd work-assistant
python3 -m work_assistant demo --output out/demo
```

Open **`index.html`** inside the `report_path` printed in the JSON result. Inspect tasks, sourced numbers, corrections, and next steps; `card-preview.html` previews the summary card.

Once the source is available, the demo runs offline without pip, model credentials, Paseo, or Feishu. All inputs and executors are deterministic synthetic fixtures: **this is not live model or delivery validation**. Repeating a complete demo returns the same result.

![Synthetic Lab offline report: task results, sourced numbers, and corrections](docs/assets/demo-report.png)

Execution, independent review, and business data in this screenshot are all simulated. The report also includes `daily-sync.md`, `evidence.html`, `evidence.json`, `card.json`, and `document.json`.

## Try your own project

Install and sign in to **either Codex or Claude Code**. The example explicitly selects Codex; for Claude Code, replace `--provider codex` with `--provider claude_code`. From this repository, replace the workspace path with your project:

```sh
python3 -m work_assistant init
python3 -m work_assistant doctor
python3 -m work_assistant project add my-project --workspace /absolute/path/to/project
python3 -m work_assistant ask my-project 'Read-only: compare the README with the implementation; cite evidence and open questions' --routing direct --provider codex
```

Tasks default to read-only and never automatically publish. Save the returned `task_id`, substitute it for `TASK_ID`, and replace the date with your archive date:

```sh
python3 -m work_assistant task show TASK_ID
python3 -m work_assistant report my-project --date YYYY-MM-DD
```

The report result's `data.path` points to its directory. The date is an archive period; reports include current tasks and valid claims, not only events from that day. Follow the [quickstart](docs/quickstart.en.md) for installation as `wa`, review, and report output.

`--routing direct` uses your explicitly selected executor without a classifier or planner; authorization and execution isolation still apply. Omitting it preserves the original `auto` route: `--provider` alone does not skip classification. Independent `--review` needs the other executor. See [routing modes](docs/routing.md), [dependencies](docs/capabilities.md), or [practice with a synthetic project](examples/first-task.md).

## Capabilities you can inspect

| Capability | Observable behavior | Implementation and verification |
| --- | --- | --- |
| Durable tasks and handoffs | Task/run/session records, results, blockers, and inspection steps for the original task | [Task entry](work_assistant/assistant.py), [onboarding tests](tests/test_onboarding.py) |
| Attributed findings and corrections | Observations and model claims remain distinct; numeric value/unit checks; old snapshots stay unchanged | [Evidence](work_assistant/evidence.py), [report tests](tests/test_evidence_reports.py) |
| Separate completion states | `protocol_status`, `goal_status`, and independent review are distinct | [Execution orchestration](work_assistant/runner.py), [review regressions](tests/test_review_regressions.py) |
| Explicit authority | Read by default; writes need intent plus grant; model output cannot add permissions | [Policy](work_assistant/policy.py), [policy tests](tests/test_policy.py) |
| Inspectable delivery | Durable intent before sending, saved remote IDs, no automatic replay of uncertain writes | [Messages](work_assistant/messages.py), [message tests](tests/test_messages.py) |
| Portable artifacts | Self-contained HTML / Markdown / JSON; no-account demo; wheel/sdist checks outside the checkout | [Demo](work_assistant/demo.py), [package smoke](scripts/package_smoke.py), [CI](.github/workflows/test.yml) |

```text
Your project → Task → Existing coding CLI → Execution / Evidence / Corrections
                                                       ↓
                                           Local HTML / Markdown report
                                                       ↓ explicit authorization
                                           Optional Feishu document / card
```

## Evaluate it independently

From the repository root, without model, Feishu, or Paseo accounts:

```sh
python3 scripts/evaluate.py --output out/evaluation
```

The evaluator uses temporary synthetic configuration to check the CLI, two independent demos for matching output, report artifacts, and credential-free publishing previews. It produces a JSON receipt without sending your configuration or project to a model. It verifies local behavior, **not real model quality, Feishu permissions, or client rendering**. See the [evidence map](docs/evidence-map.md) for its exact scope, focused test commands, and failure semantics.

[llms.txt](llms.txt) provides a concise fact and documentation index for retrieval tools. It points to the same implementation and evidence available to human readers.

## Boundaries and common questions

- **Does dependency-free mean no model CLI?** No. Demo, evidence, reports, and dry-runs need no model. Explicit direct tasks need authenticated Codex or Claude Code; automatic routing has additional planner requirements.
- **Does all data stay on the machine?** Storage is local by default; real model and Feishu requests leave the machine. Local-first does not mean offline inference.
- **How far does non-replay extend?** It is a recovery contract within the same configuration and shared journal, not cross-machine exactly-once delivery. Database copies do not share locks.
- **What does `verified` prove?** Receipts state the checks performed. Message readback does not prove business truth or that a recipient read it; default card readback does not fully verify URLs and styles.
- **Does it schedule work or chat with everyone?** There is no built-in scheduler. Optional inbound is disabled by default and limited to the owner and allowed conversations, with additional dependencies and tenant validation.

See [capabilities](docs/capabilities.md), the [security model](SECURITY.md), and the [roadmap](docs/positioning.md#下一阶段按什么顺序做).

## Go deeper

| Goal | Documentation |
| --- | --- |
| Install, run a task, create a report | [English quickstart](docs/quickstart.en.md) / [中文上手](docs/quickstart.md) |
| Evidence, corrections, recovery | [Detailed reference](docs/guide.md) (Chinese) |
| Import an existing report pipeline | [Import reference](docs/guide.md#导入已有的本地日报), [synthetic bundle](examples/daily-bundle) |
| Send Feishu text or display cards | [Setup](docs/guide.md#飞书预览和真实发布), [message commands](docs/messages.md) |
| Maintain a dated document | [Timeline trees](docs/timeline-trees.md) |
| Owner-only mentions and deep analysis | [Optional inbound](docs/inbound.md) (disabled by default; additional dependencies and tenant checks) |
| Troubleshoot | [Troubleshooting](docs/troubleshooting.md), [security model](SECURITY.md) |

## Contribute

Try the demo and one read-only task, then tell us where you got stuck. [Report an issue](https://github.com/xilinshancun/work-assistant/issues/new/choose) with the version, platform, sanitized command, and observed result. Do not attach private journals, credentials, or company material.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and tests, and [SECURITY.md](SECURITY.md) for security reporting. [MIT License](LICENSE).
