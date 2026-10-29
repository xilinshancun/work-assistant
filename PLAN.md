# Work Assistant V1

## Goal

Deliver an actually runnable, MIT-licensed personal assistant repository. Reuse an owner's installed DeepSeek Harness (DSH), Claude Code (CC), and Codex. DSH performs fast first-stage routing; difficult, ambiguous, low-confidence or failed classifications escalate to a GPT/Codex planner, which selects the right executor. CC handles architecture/review and Codex handles most implementation. Persist work, evidence, decisions, and publishable reports so the next session can continue.

Target public repository: `xilinshancun/work-assistant`. No reference screenshots, source PDF, company identifiers, production data, or personal credentials are to be included.

The supported V1 must cover: configuration/doctor, project/task CLI, two-stage routing, real local CC/Codex/DSH subprocess adapters, durable task/run journal, evidence and claims with supersession, incremental report generation, Feishu cards and documents with dry-run and configured live adapters, publication reconciliation, and an offline end-to-end demo. Existing Paseo stays the scheduling/control interface; expose one-shot commands and integration recipes rather than adding a daemon.

## Scope and user experience

Use `python3 -m work_assistant` and an optional `wa` executable. A new checkout must run the demo and tests with Python 3.9+ and no pip installation. Supply a pyproject.toml for optional package installation, a Chinese-first README plus short English description, MIT license, security model, GitHub Actions test matrix, and synthetic example configuration.

Useful commands (exact spelling may be simplified but all behavior must exist):

1. `init`, `doctor --json`: create local ignored config/state and detect binaries without revealing credentials. Configuration may specify executable argv arrays, per-provider environment overlays, project workspaces, timeouts, model aliases, and report/publisher settings. No shell-joined commands. Resolve `~` and relative config paths consistently.
2. `project add/list`, `task add/list/show`: save goals, project, read/write intent, status, blockers, optional explicit provider, and stable task IDs. Default read-only. Project workspace must exist and match configured authorization.
3. `route TASK_ID`: real DSH classification, optional GPT escalation, persist each attempt, latency, error/fallback reason. Route without executing a task.
4. `run TASK_ID`: execute an accepted route, retain session ID and logs, persist task result and explicitly attributed model claims. Support a user-requested follow-up with a stable provider session. Never imply success because a process merely started.
5. `review TASK_ID`: opposite-provider read-only review of actual task result/diff. Write tasks should require review before being marked verified; distinguish completed vs independently reviewed/verified.
6. `evidence add/ingest`, `claim add/supersede/retract`: bring local JSON or files into a private evidence store. Ingestion can read explicitly configured existing report JSON but must never mutate another project's state or source material.
7. `report PROJECT --date ...`: generate one consistent snapshot into self-contained HTML, Markdown Daily Sync, evidence JSON/detail HTML, and Feishu card/document payloads. Newest record ordering, five-part report, evidence references, statuses, blockers, next steps. Escape all untrusted text; no CDN dependencies; include readable bars/metrics when real structured numbers exist, not invented progress percentages.
8. `publish REPORT --dry-run`, `publish REPORT --confirm`: deterministic preview/publish only after explicit command or configured grant. Feishu app mode supports document and card publication; webhook supports card only and says so. Store remote IDs, hash and status; repeat identical publishing returns previous result. Updates of the same period reuse prior document/message IDs instead of duplicates. Ambiguous network outcomes become unknown and require reconciliation.
9. `reconcile ...`: inspect existing local/remote records only; do not secretly resume an agent or resend a request. User-supplied resolution requires a reason and audit event. A separate explicit retry/followup is allowed.
10. `demo --output ...`: offline, deterministic, synthetic full journey with both simple and escalated routing, task completion, evidence, a correction/supersession, report and Feishu preview. Demo cannot impersonate live provider validation.

## Suggested modules

Flat modules are acceptable for readability; avoid overengineering. Suggested: config.py, cli.py, store.py, policy.py, routing.py, process.py, executors.py, runner.py, evidence.py, reports.py, feishu.py, demo.py. Shared data contracts live in models.py or small dataclasses. Tests use unittest and injected process/HTTP transports.

## Routing and policy contract (independent implementation target)

Public pure functions in `work_assistant/policy.py`:

```
validate_route(payload: dict, task_id: str) -> dict  # normalized or ValueError
needs_escalation(route: dict, threshold: float = 0.8) -> bool
authorize(allowed_actions: list, requested_action: str) -> bool
execution_policy(provider: str, action: str) -> dict
```

Route payload exact fields:

```
{"schema":"wa.route.v1","task_id":"t_...",
 "complexity":"trivial|simple|complex|unknown",
 "executor":"claude_code|codex|none",
 "requested_action":"read|write_workspace|publish|external",
 "reason":"nonempty text, bounded to 1000 characters",
 "confidence":0.0,"needs_escalation":false}
```

Reject unknown keys, missing keys, mismatched task IDs, wrong exact primitive types, nonfinite confidence, values outside [0,1], and arbitrary executor/model names. Do not accept bool as numeric confidence. The model cannot set shell argv, credentials, paths or allowed_actions. `authorize` recognizes only the four defined actions, rejects malformed or unknown grants, gives no implicit hierarchy (write is not publish), and returns false for publish/external at the executor boundary. Publishing uses its separate explicit authority path. A malformed grant is denied, never widened.

`needs_escalation` is true for complex/unknown, low confidence, executor none or explicit needs_escalation. DSH failure/timeout/tool use also triggers GPT outside this pure function. GPT is invoked at most once; after invalid GPT or unsatisfied intent, go needs_user with diagnostic context, no infinite loop and no silent model/account swap. A successful planner can approve a complex classification for execution; complexity alone must not prevent its final plan. User-required write intent cannot be silently rerouted as read and declared completed. Explicit provider selection does not bypass task grants.

`execution_policy` accepts claude_code/codex and read/write_workspace only, raises on others, and returns `{ "provider": ..., "action": ..., "permission_mode": ..., "tools": [...] }`. Claude read uses plan plus Read/Glob/Grep tools only (no Bash/MCP writes); write uses default or acceptEdits with normal approval handling and bounded tools. Codex read uses read-only; write uses workspace-write. Never bypass all permissions. Route actions publish/external cannot be run by a coding worker.

Routing is an optimization, not a trust boundary. Classifiers operate with minimal context and read-only host policy; reject tool-calling route outputs. Avoid an overly powerful copied persona for simple classification. Cache exact unchanged request + relevant configuration where safe; include latency and fallback counts for evaluation. Do not claim DSH is faster until measured on this installation.

## Runtime adapter requirements

Detect installed binary capabilities, do not assume current online docs match local versions. Local discovery found CC 2.1.303, Codex 0.164.0-alpha.11.1, DSH 0.2.1-rc.3. The macOS DSH launcher is conventionally `/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh` and is not necessarily in PATH. This generic install path may be discovered in code; personal paths belong only in local config.

DSH installed help: `dsh --profile headless --json [--session-id ID] -`. It lacks current upstream `--permission-mode`/`--approval-policy` flags. Set `DSH_PERMISSION_MODE=read-only` for routing. CLI can initialize its own profile and needs access to its home; main application must not dump composed configs or credentials into logs. GUI auth may require a scoped overlay enabling credentials/authorization/deepseek-account, depending on installed profile. Owner will validate live behavior; do not fabricate success when unauthenticated. Capture `session`, `text`, `status`, `tool_call`, `tool_result`, `error`, and terminal `final` events. Installed 0.2.1-rc.3 source confirms `session.sessionId`, `status.phase=turn_end` with `reason.kind=completed`, then `final.text` (no final reason field). Require exit zero, successful turn_end and nonempty final text, with no failure/truncation events. Synthetic source-derived fixtures do not constitute live validation. Tool calls in a classifier cause rejection, not acceptance. Configurable DSH timeout default 25 seconds; bounded retries=0 before escalation.

Codex: `codex exec --json --skip-git-repo-check -C WORKSPACE -s read-only|workspace-write --output-schema FILE -o FILE -`. Never combine a shell string; stdin carries user text. Keep schemas compatible with supported JSON-schema subset. Session ID comes from thread.started; require turn.completed and no turn.failed/error. Per-provider home environment may be configured; no account switching on limit errors. Pin routing effort low/medium via valid config while executor effort is configurable. Invalid/truncated/missing final output is not success.

Claude: `claude -p --output-format json --permission-mode ... --tools ...` reading prompt stdin; validate the actual CLI syntax with help. Preserve subscription login, do NOT use --bare (it ignores OAuth). Inspect result/is_error/subtype/session_id rather than exit code only. Tool/permission-denial responses must not masquerade as completion. Tool/model settings come from trusted config.

Subprocesses: bounded output, timeout, POSIX process-group termination and wait, stderr retention with redaction, explicit cwd, env overlay without printing secrets, no shell=True. Windows support may be best-effort but tested logic must be portable. On a process timeout or parent death, effects may already have happened: record outcome_unknown and never rerun automatically. Resume/follow-up is an explicitly requested new attempt, not reconciliation.

Paseo adapter is optional; do not ship a pretend adapter based on unverified command syntax. The guaranteed integration is one-shot CLI invocations from existing Paseo schedules/agents, with example prompts and recorded task IDs. Do not change daemon config or existing schedules.

## Persistence and concurrency

SQLite with schema version, foreign keys, WAL/busy timeout, atomic transitions. Tables cover projects/tasks/routes/runs/events/evidence/claims/report snapshots/cursors/publications/locks as needed.

Lease claiming is atomic; per-task AND per-workspace write exclusion prevent two mutators. Persist run identity before process launch. Use an owner token/fencing generation for finishing updates; expired leases do not make an old still-running process safe to duplicate. Heartbeat long jobs or hold a robust OS lock while alive; on crash mark stale running attempts unknown, require reconcile before rerun. All exception paths release safe locks while preserving outcome.

Stable IDs and immutable run history. Distinguish task queued/routing/routed/running/collecting/completed/failed/needs_user/outcome_unknown/cancelled; reviewed/verified state is additional and not self-attested. A state transition should be validated, and CLI should fail with useful diagnostics.

## Evidence and report requirements

Evidence stores source locator, capture time, scope/environment, content hash, local copied artifact path and origin. Claims reference existing evidence IDs and use measured/reproduced/derived/code_observed/reported. A source claim from an agent is `reported` by default; do not automatically label all results measured. Derived claims require a derivation/formula and inputs. Preserve raw observation; supersession/retraction updates claim status with audit trail, and previous report snapshots stay immutable.

Validate structured numeric assertions against their explicitly supplied source values and units, not just a regex presence test. A human-supplied/manual claim remains clearly attributed; validation of shape/hash is not scientific reproduction. Explain verification scope in output. Unknown progress stays unknown. Every report statement references a claim or task source; reported findings must be labeled. Five blocks: accomplishments, numbers, findings, collaboration/blockers, next actions. Next actions cannot create promises, owners or deadlines not present in source.

Atomic report generation: freeze cutoff, collect bounded increments, validate, write all outputs, commit snapshot/cursor only after all files are usable. Publishing has its own checkpoint, so failure to publish never loses collected data. Same input gives same report content/hash; generated timestamps must not create accidental duplicate publication. Corrections after the reporting window should appear as explicit corrections and not silently disappear behind cursor.

Self-contained HTML with readable typography, mobile layout and print styles; links between summary and evidence, traceable metric bars if applicable. Preserve static equivalents for all charts. Escape HTML, reject unsafe URL schemes and handle filenames safely. Relative links work offline. Redact tokens and local home paths in outbound material; reject obvious secrets, do not pretend a regex guarantees complete confidentiality.

## Feishu

Provide dry-run card JSON + document payloads and previews with local validation. Implement real Feishu app HTTP adapter (official API) using environment refs for credentials; tenant token acquisition; create/update document blocks; create/update card messages; appropriate image/file upload only if used. Simpler native text/card components and static SVG/HTML local charts are acceptable; no mandatory browser automation. Webhook is card-only and documents remain local. Honor application-level error codes even with HTTP 200; redact URLs that contain webhook secrets.

Use a stable destination+report-period publication identity and snapshot revision. Same content is a no-op; changed content updates known remote IDs. Record intent before every side effect and checkpoint intermediate doc/card IDs, so a card failure does not duplicate a successful document. Never treat an entire multi-step publish as one opaque request. POST timeout/connection break after possible send becomes outcome_unknown; no blind retry. Use documented endpoint idempotency fields where available; no invented API guarantees. Reconcile by known IDs or explicit operator resolution; user can authorize a new attempt after assessing ambiguity.

All live sending must be explicitly requested (`--confirm`, or an exact configured destination grant). This development session must not send to colleagues. The owner may later authorize a dedicated test destination. User authorizes GitHub publication of generic source code.

## Tests and acceptance

Optional official CLI transport: `publisher.transport=lark_cli` supports app mode
alongside the default HTTP adapter. Trusted local config pins `cli_argv`,
`cli_config_dir`, optional `cli_profile`, and `expected_app_id`. Config-relative
paths resolve before use. The adapter calls masked `config show`, verifies App ID
and Feishu brand, then probes bot info before recording any mutation intent.
Credentials remain in the official CLI securestore. Each API process receives a
complete allowlisted OS environment, explicit isolated config directory and bot
identity; ambient credentials/profiles/workspace/auth proxies are excluded.
Optional explicit document-member sharing uses an approved user open_id env ref,
a checkpointed permission-create effect and member/permission destination identity;
no group or public sharing. The official CLI standard
`https://www.feishu.cn/docx/{id}` URL adds a native button to the outbound card
copy without modifying report snapshots.
Strict success envelopes and definite typed rejections are distinguished from
ambiguous outcomes. Existing per-stage checkpoints, no-op, pacing, authorization
and read-only reconciliation apply unchanged. Doctor checks selected local config
without API calls. Offline FakeCLI tests are protocol fixtures, not live proof.

- Entire unittest suite offline, Python 3.9 and 3.12.
- Strict route validation: missing/unknown keys, JSON fence, NaN, boolean confidence, wrong task ID, invalid executor, injected grants; route tool invocation/timeout/error escalates once; GPT failure blocks; explicit write cannot shrink to read and report success.
- Provider fixture parsers validate actual DSH, CC and Codex event shapes, failure with exit=0, permission denial, truncated streams, no final, killed processes and subprocess argv safety.
- Two concurrent invocations cannot run the same task or mutate same workspace twice. Parent interruption leaves recoverable unknown; fencing prevents stale completion.
- Evidence source existence/hash, claim grades, derived input requirements, retraction/supersession, stale claims excluded and corrections visible.
- Report outputs deterministic and escape malicious HTML/URLs, no secret leak, render failure freezes cursor, publishing failure does not lose report.
- Fake local HTTP server tests Feishu application errors, create/update/no-op, stage checkpointing, timeout unknown and explicit reconcile. No tests contact live services.
- Offline demo creates complete inspectable report + evidence + card/document outputs from synthetic work. README commands must actually run.
- Root performs opt-in live DSH route and independent CC/Codex read-only smoke tests against a synthetic local workspace, recording latency and provider IDs privately. A controlled temp-file write/review smoke can follow. Live Feishu requires actual account/destination setup and must be honestly reported if not configured.
- Before public push scan tracked source for personal/company paths, credentials, runtime artifacts and reference attachments; root reviews diff and verifies test output. Publish only this new repo.

## Implementation workflow

### External daily import contract

`import-daily PROJECT --source-root ROOT --date YYYY-MM-DD [--timezone Asia/Shanghai] [--title TITLE]`
imports an explicitly selected, validated local bundle without collecting, scheduling,
calling models, or publishing. It verifies date/timezone/watermark agreement, aware
nonfuture coverage with cutoff date matching the requested date in the selected zone, original HTML SHA-256 and validation status; bounded reads stay
inside ROOT after symlink resolution, and all inputs are re-read before committing.
HTMLParser extracts the body title, unique sync-summary and complete readable body
without active markup/navigation. Table cell/row boundaries and preformatted lines are
preserved; optional paragraph/list/table end tags are handled, and ambiguous nesting
is rejected. Only the first visible h1 supplies the title. Seven escaped standard outputs and an ordinary
immutable report record reuse the existing publisher identity/checkpoints. External documents contain only display title, coverage and source body with its
original qualifications; cards contain title, coverage and summary. Internal evidence
and technical verification notes remain in local evidence.html/json only. Evidence
summaries are bounded and attributed as reported; import-specific export checks
reject recognizable credentials/local paths without rewriting ordinary parameter
prose or reading environment secrets. The shared redact behavior is unchanged; extra metadata stays in
the private journal. The optional nonempty title override is bounded to 200 characters,
changes the report hash and display only, and preserves source bytes and period identity. Imports contain only that source bundle, leave normal collection
cursors untouched, reject documents above 1000 blocks without truncation, and retain
deterministic no-op / same-period update behavior. Identical trusted regeneration
can restore missing snapshot files under the same report ID, after verifying all
extant files; modified files are always rejected and never replaced. Synthetic examples live under
examples/daily-bundle; no personal source paths or business data are tracked.

Claude architect supplies the plan; Codex implementation worker owns main branch in its assigned worktree. A second Codex worker independently implements only policy.py and policy tests in a separate worktree using the exact pure interface above, to compare the critical permission boundary. Claude reviewer evaluates both and reviews the integrated implementation. Root integrates, validates live adapters, and handles authorized public GitHub creation/push after checks. No worker creates remote repositories or sends external messages.
