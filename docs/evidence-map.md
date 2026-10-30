# Evidence map / 主张与核验证据

[中文 README](../README.md) · [English README](../README.en.md) · [Documentation](README.md)

This page is for developers and retrieval agents evaluating Work Assistant.
Each claim below points to implementation, a reproducible check, and its limits.
Passing a local check is not evidence of live model quality or remote delivery.

## Start with a credential-free evaluation

On macOS/Linux with Python 3.9+, evaluate the installed package:

```sh
wa evaluate --output evaluation
```

From a source checkout or unpacked source distribution, use
`python3 -m work_assistant evaluate --output evaluation`; the compatible
`python3 scripts/evaluate.py` wrapper remains available. Evaluation uses the
runtime package associated with that entry point, not a checkout inferred from
the working directory.

The output directory must be new. The evaluator uses synthetic configuration and
data to exercise the real CLI, without model, Feishu, or Paseo calls. Keep its
JSON receipt alongside the installed version/artifact hash or source commit you evaluated. Do not replace
your own configuration or journal to make an evaluation pass.

The script prints JSON and, with `--output`, saves the same object as
`evaluation/receipt.json`. Its schema is `wa.evaluation.v1`:
`status` is `passed` or `failed`; `checks[]` holds each outcome;
`summary` counts passed/failed/skipped checks; `scope` and `limitations` describe
what was tested. Exit code **0** means all checks passed, **1** means failure.
An existing output directory is refused without modifying it. Without `--output`,
successful temporary output is deleted; failure output is retained and identified
by `output_dir` and `artifacts_retained`.

The evaluation covers:

- CLI initialization in an isolated configuration.
- Provider-free diagnostics: offline workflows remain available, while model
  execution is not presented as authenticated or usable.
- Project/task recording, report generation, and `publish --dry-run` using
  synthetic records, without claiming a queued task was executed.
- Two independent synthetic demos with matching report hashes and the seven
  expected HTML, Markdown, and JSON artifacts.

The tool does not exercise real provider authentication, model output quality,
live publication, browser rendering, or every crash/concurrency path. Use the
tests and explicit tenant validation below for the relevant additional scope.
Its Python audit guard blocks accidental socket and external child-process calls
inside the evaluated CLI; it is not an OS sandbox or a security audit.

## Claim → implementation → regression

Test names are `unittest` methods, not pytest node IDs. Run the listed test file
from the repository root using discovery, for example:

```sh
python3 -m unittest discover -s tests -p 'test_evidence_reports.py' -v
```

| Claim | Implementation | A regression to inspect | What this establishes |
| --- | --- | --- | --- |
| Python runtime has no third-party packages | [Package metadata](../pyproject.toml), [artifact smoke](../scripts/package_smoke.py) | `package_smoke.py` inspects `Requires-Dist`, then installs wheel/sdist outside the checkout | Package/runtime independence; build tools and external provider CLIs are separate |
| The no-account demo is reproducible | [demo.py](../work_assistant/demo.py) | [test_evidence_reports.py](../tests/test_evidence_reports.py): `test_full_demo_deterministic_across_directories` | Matching synthetic report hashes and complete output files, not live model success |
| Numeric claims retain source and unit checks | [evidence.py](../work_assistant/evidence.py) | [test_evidence_reports.py](../tests/test_evidence_reports.py): `test_source_hash_and_numeric_units` | A mismatched value/unit or modified source is rejected; scientific truth is not inferred |
| Corrections preserve prior reports | [reports.py](../work_assistant/reports.py), [evidence.py](../work_assistant/evidence.py) | [test_evidence_reports.py](../tests/test_evidence_reports.py): `test_snapshot_immutable_supersession_and_stale_derivation` | New snapshots carry corrections; the previous report bytes remain unchanged |
| Model output cannot grant write authority | [policy.py](../work_assistant/policy.py), [assistant.py](../work_assistant/assistant.py) | [test_policy.py](../tests/test_policy.py): `test_grants_never_widen`; [test_ask.py](../tests/test_ask.py): `test_missing_write_grant_stops_before_models_and_returns_task_id` | Application authorization checks; not a general OS sandbox guarantee |
| An uncertain execution is not automatically replayed | [runner.py](../work_assistant/runner.py), [store.py](../work_assistant/store.py) | [test_runtime.py](../tests/test_runtime.py): `test_unknown_never_replays`, `test_interruption_is_unknown` | Recovery behavior within the shared local journal |
| Message POST uncertainty is preserved | [messages.py](../work_assistant/messages.py) | [test_messages.py](../tests/test_messages.py): `test_post_api_error_and_interrupt_are_unknown_without_guessing_error_text` | Injected errors retain unknown and do not trigger automatic resending |
| Readback and sending are distinct | [messages.py](../work_assistant/messages.py) | [test_messages.py](../tests/test_messages.py): `test_readback_failures_keep_sent_id_never_resend_and_inspect_can_finish` | An acknowledged message can remain unverified; inspection does not resend it |
| Doctor does not imply model access | [config.py](../work_assistant/config.py) | [test_onboarding.py](../tests/test_onboarding.py): `test_valid_local_capabilities_never_claim_authenticated_or_live_ready` | Local interface compatibility is separate from authentication and live readiness |
| Explicit direct tasks need only their chosen executor | [routing.py](../work_assistant/routing.py), [assistant.py](../work_assistant/assistant.py) | [test_direct_routing.py](../tests/test_direct_routing.py): `test_each_direct_executor_runs_once_without_any_classifier_or_planner`, `test_direct_write_keeps_intent_and_partial_failed_write_becomes_unknown` | Zero classifier/planner calls in fixtures; the existing executor and unknown-outcome path still apply, not live account validation |
| Switching modes does not reuse a stale accepted route | [routing.py](../work_assistant/routing.py) | [test_direct_routing.py](../tests/test_direct_routing.py): `test_auto_cache_legacy_auto_and_cross_mode_switch_use_latest_accepted_route` | Auto cache hits match the runner's current accepted route, including compatibility with old auto records |
| Write authorization and report delivery are separate | [policy.py](../work_assistant/policy.py), [feishu.py](../work_assistant/feishu.py) | [test_policy.py](../tests/test_policy.py), [test_feishu.py](../tests/test_feishu.py) | A coding executor's write grant does not authorize publishing |

## Run the relevant layer

| Question | Reproduction |
| --- | --- |
| Can I get a useful artifact without an account? | `wa evaluate --output evaluation` (installed), or `python3 scripts/evaluate.py --output evaluation` (source) |
| Do evidence and corrections retain their meaning? | `python3 -m unittest discover -s tests -p 'test_evidence_reports.py' -v` |
| Does an unknown send avoid automatic replay? | `python3 -m unittest discover -s tests -p 'test_messages.py' -v` |
| Are task authorization checks covered? | `python3 -m unittest discover -s tests -p 'test_policy.py' -v` |
| Does explicit single-executor routing preserve authorization and recovery? | `python3 -m unittest discover -s tests -p 'test_direct_routing.py' -v` |
| Does the installed package work outside the checkout? | Build and run the [distribution procedure](releasing.md); it installs wheel and sdist in separate venvs |
| Does the entire offline regression suite pass? | `python3 -m unittest discover -s tests -v` |

Tests inject providers and transports; some bind loopback HTTP servers or launch
synthetic child processes. They do not authorize real model or Feishu calls.
The distribution procedure may download build tools, unlike the source evaluator.

## Reading the result without overstating it

- **`reported`**: an attributed model/operator statement. Source/hash/value
  checks do not turn it into an independently measured fact.
- **`protocol_status=completed`**: the provider protocol ended successfully;
  inspect `goal_status` and actual artifacts before claiming the task is done.
- **`local_checks_passed`**: local CLI compatibility. Authentication remains
  untested by doctor.
- **Delivery `verified`**: the checks listed in that receipt passed. It does not
  mean the recipient read the message, nor that its business content is true.
  Default card readback does not fully verify URL/style fidelity.
- **`outcome_unknown`**: an effect may have happened. Absence of a local remote ID
  does not prove absence at the destination. Inspection and new actions are
  separate operations.

The non-replay contract depends on using the same configuration and journal;
copies on different machines do not share locks or provide exactly-once delivery.
See [capabilities](capabilities.md) and [SECURITY](../SECURITY.md).

## Commit-specific evidence and project maturity

An existing baseline is [commit 171ef3f](https://github.com/xilinshancun/work-assistant/commit/171ef3f005b94671e27e428f0f95769d5d7fb545),
with [nine successful hosted checks](https://github.com/xilinshancun/work-assistant/actions/runs/41002550791)
on 2026-10-30: build, four macOS/Linux × Python 3.9/3.12 test combinations,
and four artifact-install combinations. This predates the standalone evaluator.
For another commit, consult [its own CI run](https://github.com/xilinshancun/work-assistant/actions/workflows/test.yml)
and reproduce the checks; a badge for `main` is not evidence for an arbitrary checkout.

Work Assistant remains Alpha. No independent cross-project speed, cost, security,
or model-quality benchmark is claimed. The [positioning page](positioning.md)
describes intended fit and alternatives, and the [roadmap](positioning.md#下一阶段按什么顺序做)
separates plans from available capabilities.
