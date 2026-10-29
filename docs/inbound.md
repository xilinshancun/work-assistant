# Opt-in owner-only Feishu intake V1

This separate foreground command handles an explicitly enabled owner's direct
messages or the same owner mentioning the bot in one approved test group. It does
not alter daily schedules, report rendering, publishing or outgoing-message
authorization. It is disabled when `inbound` is absent or `enabled` is false.
No attachments, group-wide access, business edits, publishing, credential reads
or automatic retries are authorized by an incoming prompt.

Use [the synthetic example](../examples/config.inbound.example.json) as a template
in ignored private local configuration. Configure your own official Lark CLI
securestore/profile and application, owner open_id, approved test chat and existing
project ID with an explicit `read` grant. The project workspace must be an exact
Git root with a valid HEAD. `worktree_root` must be outside that source workspace;
directories, results and unapplied changes remain private and are not removed.
Keep the same durable `state_dir` for every instance of this application; its
per-app flock and event/message deduplication live there. Never run another copy
with a different state directory to bypass an unresolved action.

Install and authenticate official `lark-cli` separately, and configure the app's
long connection, `im.message.receive_v1` subscription, bot feature, owner access
and the appropriate DM/group-mention permissions in your own tenant. This module
does not change subscriptions/scopes in the application console. Paseo must be
installed and its local daemon authenticated/configured separately. It discovers
the standard macOS executable when `paseo_argv` is omitted, otherwise uses PATH;
the example deliberately uses PATH. `gpt_provider` is explicitly `codex` or the
trusted configured alias `codex-b`; both accept the official `PROVIDER/MODEL`
combination, with optional `gpt_model`. The alias must already exist in Paseo;
this module does not create an alias, switch accounts or fall back to another
provider. Deep processing uses `high` thinking and `auto-review`. DSH uses the
existing provider config with a maximum 20-second tool-free classification call;
missing/failed DSH escalates once to Paseo GPT instead of installing everything.

After reviewing local configuration and enabling it explicitly:

```sh
python3 -m work_assistant.inbound run --config config.local.json
python3 -m work_assistant.inbound status --config config.local.json
```

`run` is foreground, holds the app flock and starts exactly one official
`lark-cli event consume im.message.receive_v1 --as bot`. Configuration App ID is
verified by the existing CLI transport and `/bot/v3/info` supplies the distinct
bot open_id. No secret/token is exported. The consumer inherits the same isolated
CLI environment as outgoing API calls, with stdin PIPE held open. It is ready only
after the exact stderr marker `[event] ready event_key=im.message.receive_v1`.
Do not set `--quiet`: stderr sequence gaps/dropped events become durable diagnostic
records. Raw stderr is not retained. stdout has a dedicated bounded reader; each
line is validated and journaled before model work. The consumer's exit or invalid
oversized stream ends the service, without a hidden restart loop. SIGTERM stops
only the owned consumer process; agents already launched remain inspectable.

The official stream is **at-most-once** at this integration boundary. The bus
acknowledges upstream before consumer persistence, has no durable replay, and
drops old events under backpressure. This module cannot promise receipt while
stopped or during overflow. Dedicated journaling narrows that loss window; it
does not turn the official bus into a durable queue.

Events are flattened JSON lines. `content` is already human-readable text, not
another JSON envelope. `message_id` (not event_id) plus configured App ID supplies
the durable deduplication key. Mentions use `mentions[].id`, never display names.
Only `sender_type=user` and the configured owner's exact open_id are accepted.
DMs must be p2p; groups must match the single test chat and mention the bot's
verified open_id. Nontext, old, future, malformed, bot/app/nonowner messages are
ignored with zero replies. Ignored records retain minimal metadata, and malformed
records only a line hash/reason; unrelated message text is not copied. Accepted
text is saved to a private 0600 artifact and referred to by hash. Unknown fields
cannot change project, recipients, providers or authority.

DS returns only `wa.inbound.v1` with exact `schema,decision,answer,confidence,reason`
fields. Tool use, malformed JSON, nonfinite/low confidence or failure escalates.
Questions about project state/files/execution always escalate conservatively.
Quick answers have a fixed “未查询项目状态/未执行操作” marker; no claims of executed
work are inferred from them. The decision is durable before a reply. A deep task
sends one investigating ack and at most one final to the event's exact chat_id,
using `messages.send` and stable `inbound:KEY:ack` / `inbound:KEY:final` request IDs.
All send verification and unknown-outcome rules are inherited. An unknown ack is
never reposted, but does not itself prevent deep processing. At most one unresolved
deep task is admitted; other requests receive a busy final and remain needs_user.

Paseo dispatch first checks `ls --all --global --label wa_event=KEY --json`, then
creates a detached local Git worktree pinned to trusted project HEAD. Request
JSON containing the authorization and untrusted user text is persisted privately.
The CLI positional prompt is only a trusted instruction to read that request file;
raw user text is never placed on the command line. Creation intent is durable
before `run --no-wait --json --label ... --new-workspace local --cwd ...`.
Paseo's `created` acknowledgement is not task completion. After launch only the
known ID is polled; no `send` or new prompt occurs during recovery. Caller PASEO_*
scope is excluded from child CLI environment to avoid inheriting another session.

Paseo does not expose a structured final via `logs --json`; it is human text.
`run --output-schema` is deliberately forbidden because it cannot combine with
`--no-wait` and internally sends retry prompts. Instead the request specifies the
sole writable result artifact `.wa-inbound-result.json` at the isolated Git root:

```json
{"schema":"wa.inbound.result.v1","key":"inbound_SYNTHETIC","nonce":"RANDOM_NONCE","outcome":"answered","answer":"Synthetic result"}
```

The artifact must be the agent's final action. It is opened O_NOFOLLOW, checked
as a regular file <=64 KiB, parsed as strict JSON, and checked against exact key
and random nonce with bounded plain text and `outbound_guard`. It alone never
proves success: an initial `inspect` must be idle, `wait ID --timeout 1s --json`
must report idle, and an independent fresh `inspect --json` must again report
Status=idle and PendingPermissions=[]. Running snapshots return immediately so
new DS requests do not wait behind the deep agent. Timeouts keep
waiting without a new prompt. Transient read-only CLI lookup/inspection failures
retain the original state and record a consecutive count and timestamp. A successful
query clears the count; only ten consecutive failures consume a diagnostic final.
Malformed protocol or mismatched agent/workspace identity still stops immediately.
This read-only recovery never retries `run` or message writes. Agent wait timeouts keep
waiting without prompts; error/permission states yield needs_user diagnostics,
never the artifact answer. Missing/invalid final also yields needs_user without
repair prompts. Full `git status --porcelain=v1 -z --untracked-files=all
--ignored=matching` is checked, excluding only the exact root result path marked
`?? .wa-inbound-result.json` or `!! .wa-inbound-result.json`.
Any other change is retained, recorded needs_user and described as unapplied;
changed HEAD is rejected. No edits are applied/merged/cleaned automatically.

The immutable request permits the deep GPT to use one level of CC/Codex children
only in the same isolated workspace, inheriting the identical read/external-effect
restrictions and prohibiting grandchildren. This is a **task contract and
auto-review policy, not an OS-enforced read-only sandbox**. Agents run as the same
OS user and may technically access other local files or services; V1 consequently
remains owner-only test scope. Model output cannot increase any configured grant.

Inspect and resolve without a new prompt:

`run` holds the application flock throughout its lifetime. Stop the foreground
listener before running `inspect` or `resolve`; those commands acquire the same
flock. Stopping intake does not stop/relaunch a Paseo agent already dispatched.

```sh
python3 -m work_assistant.inbound inspect inbound_KEY --config config.local.json
python3 -m work_assistant.inbound resolve inbound_KEY --absent --reason 'Operator independently checked original creation' --confirm --config config.local.json
```

When creation intent lacks an ID, inspection uses only the original label and
cwd: exactly one match may be adopted for polling, zero remains unknown and
multiple matches need user investigation. No new `run` is allowed. `--absent`
requires a nonempty operator reason and zero label results, records an audited
operator assertion and closes that event; it does not retry/create or accept
the same message again. Any genuinely new action needs new explicit user input.
Message unknowns are inspected/resolved via the existing `work_assistant.messages`
commands using the recorded action ID; never replace a request ID to resend.
An agent stopped for permissions/errors retains the sole deep-task slot. After
the operator has dealt with that original agent through Paseo and it is normally
idle, `inbound resolve KEY --finished --reason 'Operator checked original task'
--confirm` verifies that terminal state and releases only the local slot. It does
not read/send its result, recreate it, resume it, or replace an earlier diagnostic
final. A non-idle original agent cannot be cleared this way.

All automated tests use synthetic events, fake model/CLI/transport fixtures and
temporary local Git repositories/processes. Installed CLI help/source determines
the documented protocol, but fixtures are not proof of live subscription,
permissions, final formatting, delegation or message delivery in your tenant.

New quick/deep answer finals use the shared bounded display-only card path:
a blue “快速回答” / “深度分析” header, a small gray status note, a divider and
the original answer in `plain_text` (Markdown symbols are not interpreted).
Quick notes state that no project state was queried and no operation executed.
Deep notes distinguish completed analysis from a result needing confirmation;
isolated unapplied changes are explicitly labeled as not applied or merged.
Acknowledgements and diagnostics remain text. Existing display-card size/content
limits and readable-projection GET verification still apply; visual appearance
is not independently verified by the offline tests.

Before final intent, the exact payload bytes/hash and `final_msg_type` are frozen
in `final.json` (interactive) or `final.txt` (text). Recovery reads that original
format rather than rebuilding the card. Legacy plans without `final_msg_type`
remain text, retaining their bytes/hash and stable `inbound:KEY:final` request ID.
Interrupted or unknown final intent never automatically resends either format.
