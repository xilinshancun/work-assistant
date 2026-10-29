# Daily import callouts (internal tree v1)

`import-daily` recognizes a source `section` with exactly
`data-callout-tone="orange"` or `data-callout-tone="blue"`. It groups that
section's existing native heading/text/bullet blocks into a callout without
rewriting text, adding claims, changing order, or discarding inline links/styles.
For example, an orange section may contain an h2 and five linked li items;
a blue sync-summary section may contain an h2 and four summary paragraphs.
Counts come from the source, never generated placeholder content.

Only reports containing an exported callout use this internal `document.json`:

```json
{
  "format": "wa.document.tree.v1",
  "title": "Synthetic daily report",
  "blocks": [
    {
      "block_type": 2,
      "text": {"elements": [{"text_run": {"content": "Coverage line"}}]}
    },
    {
      "block_type": 19,
      "callout": {"background_color": 2, "border_color": 2, "emoji_id": "writing_hand"},
      "child_blocks": [
        {
          "block_type": 4,
          "heading2": {"elements": [{"text_run": {"content": "Deliverables"}}]}
        },
        {
          "block_type": 12,
          "bullet": {"elements": [{"text_run": {"content": "Synthetic PR; review pending."}}]}
        }
      ]
    }
  ]
}
```

`blocks` and `child_blocks` are internal object arrays. They are deliberately
different from the official API's `children` array of remote block IDs.
The title remains metadata, coverage remains the first text block, and leaves
retain the existing native payloads (including combined bold/italic/strikethrough
and encoded safe HTTP(S) `text_element_style.link.url`). The
[official SDK](https://github.com/larksuite/oapi-sdk-go/blob/v3_main/service/docx/v1/model.go)
defines the Callout fields and Block children type. The fixed mapping is:

| Source tone | background_color | border_color | emoji_id |
|---|---:|---:|---|
| orange | 2 | 2 | writing_hand |
| blue | 5 | 5 | bulb |

Source attributes cannot supply arbitrary API colors or emoji IDs. The full
official emoji enumeration has not been verified; these two names are explicit
supported mappings, not a claim that arbitrary emoji identifiers are valid.

The renderer supports one callout layer, with native text (2), heading1–heading6
(3–8), and bullet (12) leaves. Empty callouts, nested callouts, unknown tones,
tone attributes on other tags, and unsupported child semantics (including hr)
are rejected rather than flattened or silently discarded. All containers,
leaves, and coverage count toward the existing 1000-block maximum; per-leaf
limits remain 10000 characters and 1000 text runs. Existing source visibility,
body-scope, credentials/path checks and safe-link rules continue to apply.

`index.html` shows the two colors and child hierarchy with clickable safe links
and inline styles; `daily-sync.md` preserves the existing textual ordering and
formatting. Cards continue to use the same source summary paragraphs.
Reports without an exported callout keep the original flat
`{"title": "...", "children": [...]}` format and output behavior.

This tree is not an API request. Preview validates it offline; application
publication requires `document_layout=timeline`. The default per-period flat
publisher rejects it before requests or publication intent. Timeline translates
`child_blocks` to temporary IDs and official descendant payloads, validates every
returned ID relation against whole-tree GET readback, and keeps checkpoints per
effect. Large containers are created with nonempty children then extended in
bounded batches. Existing same-style callouts and compatible leaf IDs are reused.
There is no automatic flattening or implicit adoption of a locked document.

An existing approved surgery acknowledgement may be explicitly adopted using
`publish adopt-timeline ... --tree-journal ... --confirm`; see the README for
ownership/preserved-child rules and unknown-outcome recovery. Synthetic HTTP
tests demonstrate protocol/recovery logic only; they do not establish rendering,
account permissions or live delivery in any reader's tenant.
