# Choose an executor: direct or auto / 选择执行方式

[English quickstart](quickstart.en.md) · [中文上手](quickstart.md) · [Capabilities](capabilities.md)

只有一种编码 CLI 时，显式选择 `--routing direct --provider claude_code` 或
`--routing direct --provider codex`。默认 `auto` 保留自动分类与升级；两种方式都
经过相同的授权与执行器检查。下文说明入口和恢复边界。

## Pick the workflow you need

| Mode | Who selects the executor? | Required tools | Model calls before execution |
| --- | --- | --- | --- |
| `direct` (opt-in) | You must supply `--provider codex` or `--provider claude_code` on `ask` | The selected, authenticated CLI | None: no classification or planning |
| `auto` (default) | Classification proposes a route; `--provider` can fix its executor | DSH optional; Codex needed if DSH is absent or escalates; plus the executor | DSH classification if available, then at most one Codex planner escalation when needed |

Codex can cover automatic planning and execution without DSH when selected with
`--provider codex`. Without this choice, the planner may select Claude Code, which
would also need to be installed. DSH + Claude Code without Codex can only cover automatic
classifications that do not require escalation. Claude Code alone can use direct.

`direct` skips model-based routing, not permission checks. It does not add project
grants, remove executor isolation, validate the goal, or switch to another provider
when execution fails. There is no claim that either mode improves model quality.

## Run a new read-only task

After `init`, `doctor`, and `project add`, choose one of these commands:

```sh
python3 -m work_assistant ask my-project 'Read-only: compare the README with the implementation; cite files and uncertainties' --routing direct --provider claude_code
```

```sh
python3 -m work_assistant ask my-project 'Read-only: compare the README with the implementation; cite files and uncertainties' --routing direct --provider codex
```

These invoke the selected real model through your account. Use the
[synthetic practice project](../examples/first-task.md) if you do not want to start
with private code. The offline demo and evaluator still require no model account.

For automatic routing, omit `--routing` or specify `--routing auto`.
`--provider` alone keeps its original meaning: fix the executor while retaining
automatic classification. Independent `--review` always uses the opposite
provider and therefore needs both Codex and Claude Code, even with direct routing.
If review cannot run or does not pass, the task is not independently verified.
Its original execution result is retained; a failed review does not repeat the task.

## Record and route a task separately

```sh
python3 -m work_assistant task add my-project 'Read-only: inspect the public example' --provider claude_code
python3 -m work_assistant route TASK_ID --routing direct
python3 -m work_assistant task show TASK_ID
python3 -m work_assistant run TASK_ID
```

Replace `TASK_ID` with the returned ID. `route --routing direct` records the
selection but does not execute the task. It uses the provider saved when the task
was created; it does not accept a replacement provider. A task without a saved
provider cannot use this mode. Inspect that task before deciding whether a new
task is appropriate; do not recreate an unresolved operation to bypass recovery.

`ask --routing direct` without `--provider` is rejected before task creation.
Write tasks still require both `--intent write_workspace` and an existing matching
project grant. A write grant does not imply read, publication, or external authority.

## Inspect decisions and recover without replay

`task show TASK_ID` includes route history. A direct record has `mode=direct`,
`basis=explicit_user_selection`, `provider=user_direct`, and no provider response.
Here `provider=user_direct` identifies the route's origin; the actual executor is
stored in `route.executor`.
Its schema-compatible `complexity=unknown` and `confidence=0.0` mean no classifier
scored the request; they are not a model assessment or a completion rating.
Report completion still separates protocol success from goal verification.

Routing mode belongs to a routing action, not to a permanent task setting.
Repeating a separate `route TASK_ID` command defaults to auto; choose direct again
explicitly when that is intended. A later explicit route becomes the runner's
current accepted route. Auto does not reuse an older cached route across a newer
direct decision. Old records without a mode remain auto records.

Neither mode reroutes a running, collecting, routing, cancelled, or unknown task.
For `outcome_unknown`, preserve the original run and inspect with
`reconcile RUN_ID` first. It may record an interrupted run's unknown status in the
local journal; it does not invoke a provider or repeat an external effect.
See [recovery](troubleshooting.md#未知结果检查与重试分开).

Doctor lists complete argument choices in `workflows.ask.paths[].task_option`.
`direct_only` means the direct path passed local CLI checks while the complete
automatic path is unavailable. Authentication remains **not tested**; doctor does
not contact a model, and an offline fixture does not prove your account is usable.

`auto_readiness` describes the automatic path separately. Its legacy value
`limited_direct_route` means DSH accepted a classification without a Codex
fallback available; despite the name, it does not mean `--routing direct`.
Use each path's `mode` and `task_option` to distinguish them.
