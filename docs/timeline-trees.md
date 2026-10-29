# Native trees in a fixed Daily Sync

The existing timeline binding, daily card identity, authorization, locks and
reconciliation commands also handle `wa.document.tree.v1`. No scheduler or new
permission source is introduced. Source blocks cannot provide IDs, ownership or
publication grants. Flat reports remain compatible. A tree report must use
application timeline mode; the per-period flat publisher refuses it before
requests or publication intent. Webhook remains explicitly card-only.

The source contract is described in [daily-callouts.md](daily-callouts.md).
Native callout `child_blocks` is an internal object array. API translation uses
remote `children` IDs and the official descendant endpoint, never object arrays
in the remote ID field.

The extension records `tree_mode=wa.document.tree.v1` on `doc_binding`, with a
recursive `tree` per date alongside its root `block_ids`. Each acknowledged node
contains `id`, a typed native `block`, `children` nodes, and `owner`. A `preserved`
child is deliberately outside the source renderer. Bindings retain the original
source signatures, even when GET observes historical user text/style/comments;
reading such an edit never silently authorizes a future overwrite.

Before each effect, the engine reads document metadata, every page of blocks,
then metadata again. Every ID/parent edge/child order must match the acknowledged
forest. Signatures and comments are checked throughout the selected date; old
dates are structurally checked but their text/styles/comments are left intact.
Server default false inline styles, alignment/folding and zero callout text color
are normalized without flattening native formatting or encoded links.

Matching type/style containers retain their IDs. Compatible leaves PATCH only
changed text elements. Added or replaced nodes are inserted under their intended
parent and verified before obsolete tool nodes are deleted. Requests contain at
most 50 nodes including descendants. A large callout starts with a nonempty
prefix, then adds remaining children in bounded batches. All callouts, children,
preserved nodes and temporary replacements count toward `timeline_max_blocks`.
New dates insert a date heading1 and body at the correct top-level position;
same-day cards reuse their existing message. The card is untouched while document
work is incomplete or ambiguous.

Historical document-only backfill uses the same binding, per-document/per-date
locks, flat/tree engine, preflight checks and checkpoints:

```sh
python3 -m work_assistant publish REPORT_ID --document-only --dry-run
python3 -m work_assistant publish REPORT_ID --document-only --confirm
```

This option requires configured `mode=app` and `document_layout=timeline`;
per-period and webhook modes are rejected, without fallback. It applies only to
report publication, not adopt/rename. Without explicit `--confirm` it is always
an offline preview, even if a destination grant exists. Report/model content
cannot select the delivery scope. There is no source rewrite or schedule change.

Successful document-only publication records `delivery_scope=document_only`,
`document_status=synchronized`, and `message_status=not_requested`. Its overall
`completed` means only the document work has finished. The section is inserted
in descending date order; other dates retain all node IDs, content, comments,
styles, and adopted/preserved children. No message API is called, including
message GETs during document-only reconciliation. An existing same-day message
ID is retained as a receipt, with its ordinary publication ID and card hashes;
the ordinary publication record and remote card remain untouched.

Document-only and ordinary document+card publication keep separate revision
records under the same destination/project/date identity and locks. The default
scope of older records is document+card. Repeating an identical completed
document-only input with its section still acknowledged returns a local no-op
with zero requests. It does not count as an ordinary card delivery: a later,
separately authorized ordinary `publish REPORT_ID --confirm` sends that day's
missing card, or updates its known card when needed, while reusing an already
synchronized document section. The tool never automatically invokes that step.

Unknown results block replay and scope switching. Complete/recover the original
scope first; adding `--retry` or changing scope cannot bypass an unresolved
record. Inspection and explicit resolution reuse the existing `reconcile`
commands, without message-ID/absence confirmations for document-only records:

```sh
python3 -m work_assistant reconcile PUBLICATION_ID
python3 -m work_assistant reconcile PUBLICATION_ID \
  --resolution retry_authorized --reason 'Operator checked exact historical tree'
python3 -m work_assistant publish REPORT_ID --document-only --confirm --retry
```

An exact, fully inserted document-only section can also be explicitly resolved
`completed` via its publication or binding ID, with a reason and full document
readback; no card receipt is required. Partial insertion cannot be marked
complete. Cancellation retains the unresolved details and does not grant
scope-switching or replay permission. Neither branch writes to a different
date or repeats an ambiguous effect without the established recovery checks.

Existing user containers need explicit adoption. For the reviewed private
surgery acknowledgement, use:

```sh
python3 -m work_assistant publish adopt-timeline PUBLICATION_ID --confirm \
  --tree-journal /private/path/completed-journal.json --title 'Project Daily Sync'
```

The journal must be completed with no pending effect; its document ID and complete
`document_tree` must match the stored publication ack. The ack snapshot hash is
recomputed, every native leaf matches the frozen acknowledged report in order,
and GET must reproduce the full tree. Destination, project, existing document,
existing message and member-grant checks still apply. Adoption clears only that
publication's layout lock inside the binding transaction, retaining the old lock
and journal hash in `tree_adoption`. It creates no document, message or share.
`--title` is a separate explicit page-title effect; without it title is retained.

The original user callout is `user-adopted`. Its baseline empty text child is
also `user-adopted/preserved`, including when the private helper initially marked
only the parent. Rendering source without that empty child cannot delete it.
The first normal publication adds the date heading without rebuilding the
adopted container. Source changes cannot delete/restyle a user container, delete
its preserved child, or ambiguously match it to multiple containers. Other
unexpected manual structures need a separately reviewed acknowledgement, not an
inferred adoption from report/model output.

A tree operation uses the same `operation` and `pending` journal as flat
timeline publication. It retains the source tree, target tree, current acknowledged
forest, parent-local actions, UUIDv4 tokens, mappings, action index and verified
history. Intent is durable before launch, response is checkpointed before GET,
and a step advances only after exact whole-tree verification. Reconciliation
compares only the explained pre-state or complete post-state of that one pending
effect. It can recover lost descendant ID relations from a proven full post-tree;
partial/unexplained content never becomes completed or replay permission.

```sh
python3 -m work_assistant reconcile DOC_BINDING_ID
python3 -m work_assistant reconcile DOC_BINDING_ID \
  --resolution retry_authorized --reason 'Operator checked the exact tree'
python3 -m work_assistant publish REPORT_ID --confirm --retry
```

Inspection/resolution performs GETs and local audit changes only. The separate
retry continues from the verified action index. Cancelling does not authorize
replay. Neither client-token retention nor `document_revision_id` supplies an
atomic CAS guarantee: a GET/write race remains with external editors. Avoid
simultaneous edits to the target section. Synthetic tests establish local
protocol/recovery behavior, not a tenant's live permissions or visible rendering.

Official API definitions:
[SDK request/response models](https://github.com/larksuite/oapi-sdk-go/blob/v3_main/service/docx/v1/model.go),
[nested block creation](https://open.feishu.cn/document/docs/docs/document-block/create-2?lang=zh-CN).
