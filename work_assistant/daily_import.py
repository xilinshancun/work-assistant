"""Import an explicitly selected, validated daily bundle. No providers or network."""
import os
import re
import stat
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .reports import STYLE, document_blocks, escape, md_text, save_report
from .util import digest, dumps, redact_data, strict_json

MAX_STATE = 1024 * 1024
MAX_FILE = 20 * 1024 * 1024
MAX_SUMMARY = 6000
SCOPE = ("源报告陈述 / Reported source assertions. 导入仅核对日期、水位和文件哈希；"
         "不代表独立测量或科学复现 / not independently measured or reproduced.")


def _read(root, relative, limit):
    if type(relative) is not str or not relative.strip():
        raise ValueError("source file path must be nonempty text")
    # Absolute paths are permitted only inside the explicitly supplied bundle.
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError("source path escapes source-root after symlink resolution")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(str(path), flags), "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("source must be a regular file")
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("source file exceeds size limit (%s bytes)" % limit)
    # Resolve again to detect a replaced path/symlink while reading.
    if (root / relative).resolve() != path:
        raise ValueError("source inputs changed during import")
    return path, raw


def _object(raw, label):
    try:
        value = strict_json(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ValueError(label + " must be valid UTF-8 JSON") from None
    if type(value) is not dict:
        raise ValueError(label + " must be a JSON object")
    return value


def _text(value, label, limit=2000):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ValueError(label + " must be nonempty bounded text (max %s characters)" % limit)
    return value


def _timestamp(value, label):
    _text(value, label, 100)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(label + " must be an ISO timestamp with timezone") from None
    if stamp.tzinfo is None:
        raise ValueError(label + " must include a timezone offset")
    return stamp


class VisibleReport(HTMLParser):
    """Extract text only; arbitrary markup, attributes and links never reach output."""
    excluded = {"head", "script", "style", "footer", "nav", "template", "noscript",
                "iframe", "object", "embed", "svg", "canvas"}
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}
    blocks = {"address", "article", "aside", "blockquote", "br", "dd", "details", "div",
              "dl", "dt", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6",
              "header", "hr", "li", "main", "ol", "p", "pre", "section", "summary",
              "table", "tr", "ul"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.body, self.summary, self.title = [], [], []
        self.summary_count = 0

    def _active(self):
        return (any(e[0] == "body" for e in self.stack)
                and not any(e[1] for e in self.stack))

    def _append(self, text):
        if self._active():
            self.body.append(text)
            if any(e[2] for e in self.stack):
                self.summary.append(text)
            if any(e[0] == "h1" for e in self.stack):
                self.title.append(text)

    def handle_starttag(self, tag, attrs):
        attrs = {name: value or "" for name, value in attrs}
        hidden = (tag in self.excluded or "hidden" in attrs
                  or attrs.get("aria-hidden", "").lower() == "true"
                  or attrs.get("role", "").lower() == "navigation"
                  or re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
                               attrs.get("style", ""), re.I))
        summary = attrs.get("id") == "sync-summary"
        if tag in self.blocks:
            self._append("\n")
        self.stack.append((tag, bool(hidden), summary))
        if summary and self._active():
            self.summary_count += 1
        if tag in self.void:
            self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.blocks:
            self._append("\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self._append(re.sub(r"\s+", " ", data))

    @staticmethod
    def clean(parts):
        return "\n".join(line.strip() for line in "".join(parts).splitlines() if line.strip())

    def extract(self, raw):
        try:
            self.feed(raw.decode("utf-8"))
            self.close()
        except (UnicodeError, ValueError):
            raise ValueError("source report must be UTF-8 HTML") from None
        title, summary, body = (self.clean(p) for p in (self.title, self.summary, self.body))
        _text(title, "visible body h1 title", 200)
        _text(summary, "visible sync-summary", MAX_SUMMARY)
        _text(body, "visible report body", MAX_FILE)
        if self.summary_count != 1:
            raise ValueError("source report requires exactly one visible sync-summary")
        return title, summary, body


def _evidence_summary(evidence):
    sources, items, limitations = (evidence.get(k) for k in ("sources", "items", "limitations"))
    if any(type(v) is not list for v in (sources, items, limitations)):
        raise ValueError("evidence sources/items/limitations must be lists")
    if len(sources) + len(items) + len(limitations) > 1000:
        raise ValueError("evidence exceeds 1000 entries")
    selected_sources, selected_items, ids = [], [], set()
    for source in sources:
        if type(source) is not dict:
            raise ValueError("evidence source must be an object")
        ident = _text(source.get("id"), "source id", 200)
        if ident in ids:
            raise ValueError("duplicate evidence source id")
        ids.add(ident)
        selected_sources.append({"id": ident, "summary": _text(source.get("summary"), "source summary"),
                                 "verified_at": _text(source.get("verified_at"), "source verified_at", 100)})
    item_ids = set()
    for item in items:
        if type(item) is not dict:
            raise ValueError("evidence item must be an object")
        ident = _text(item.get("id"), "item id", 200)
        refs = item.get("source_ids")
        if (ident in item_ids or type(refs) is not list or not refs
                or any(type(ref) is not str or ref not in ids for ref in refs) or len(refs) > 1000):
            raise ValueError("evidence item requires unique id and existing source_ids")
        item_ids.add(ident)
        selected_items.append({"id": ident, "claim": _text(item.get("claim"), "item claim"),
                               "source_ids": refs, "grade": "reported"})
    selected = redact_data({"sources": selected_sources, "items": selected_items,
                            "limitations": [_text(v, "limitation") for v in limitations]})
    if len(dumps(selected).encode("utf-8")) > 65536:
        raise ValueError("outgoing evidence summary exceeds 64 KiB; narrow source summaries")
    return selected


def render_import(snapshot):
    title, summary, body, coverage, selected = (snapshot[k] for k in
                                              ("title", "summary", "body", "coverage", "evidence"))
    interval = "覆盖区间 / Coverage: %s → %s (%s)" % (
        coverage["covered_from"], coverage["covered_until"], snapshot["timezone"])
    evidence_lines = ["证据摘要 / Evidence (reported)"]
    evidence_lines += ["%s: %s (sources: %s)" % (i["id"], i["claim"], ", ".join(i["source_ids"]))
                       for i in selected["items"]]
    evidence_lines += ["%s: %s (source verified_at: %s; supplied by source)" %
                       (s["id"], s["summary"], s["verified_at"]) for s in selected["sources"]]
    evidence_lines += ["局限 / Limitations"] + (selected["limitations"] or ["源未列出 / None supplied by source"])
    evidence_text = "\n".join(evidence_lines)
    plain = title + "\n" + interval + "\n" + SCOPE + "\n\n" + body + "\n\n" + evidence_text
    document = {"title": title, "children": document_blocks(plain)}
    # Card limits never silently truncate the source summary. Split into native text elements.
    card_text = interval + "\n" + summary
    card = {"config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": card_text[i:i + 1500]}}
                         for i in range(0, len(card_text), 1500)]
                         + [{"tag": "note", "elements": [{"tag": "plain_text", "content": SCOPE}]}]}

    def paragraphs(text):
        return "".join("<p>" + escape(line) + "</p>" for line in text.splitlines())

    def page(text):
        return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'><title>"
                + escape(title) + "</title><style>" + STYLE + "</style></head><body><main>"
                + text + "</main></body></html>")

    header = "<header><h1>" + escape(title) + "</h1><p>" + escape(interval) + "</p><p>" + escape(SCOPE) + "</p></header>"
    return {"index.html": page(header + "<section id='sync-summary'>" + paragraphs(summary)
                                + "</section><section>" + paragraphs(body)
                                + "</section><a href='evidence.html'>证据 / Evidence</a>"),
            "daily-sync.md": "# " + md_text(title) + "\n\n" + md_text(interval) + "\n\n" + SCOPE
                             + "\n\n" + "\n\n".join(md_text(line) for line in body.splitlines())
                             + "\n\n" + "\n\n".join(md_text(line) for line in evidence_lines) + "\n",
            "evidence.html": page(header + "<section>" + paragraphs(evidence_text)
                                  + "</section><a href='index.html'>完整报告 / Full report</a>"),
            "evidence.json": dumps(snapshot), "card.json": dumps(card), "document.json": dumps(document),
            "card-preview.html": page(header + "<section>" + paragraphs(summary)
                                      + "</section><a href='index.html'>完整报告 / Full report</a>")}


def import_daily(store, project, source_root, period, timezone_name="Asia/Shanghai"):
    try:
        requested = date.fromisoformat(period)
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("date must be YYYY-MM-DD and timezone must be an installed IANA zone") from None
    if requested.isoformat() != period:
        raise ValueError("date must be YYYY-MM-DD")
    p = store.project(project)
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("source-root must be an existing bundle directory")
    with store.inspection_lock(["report:" + p["id"]]):
        state_path, state_raw = _read(root, "state.json", MAX_STATE)
        state = _object(state_raw, "state")
        daily = state.get("daily")
        if type(daily) is not dict:
            raise ValueError("state.daily must be an object")
        if state.get("timezone") != timezone_name:
            raise ValueError("state timezone does not match requested timezone")
        if daily.get("last_successful_execution_date") != period:
            raise ValueError("stale source: state execution date does not match requested date")
        report_path, report_raw = _read(root, daily.get("last_report"), MAX_FILE)
        evidence_path, evidence_raw = _read(root, daily.get("last_evidence"), MAX_FILE)
        evidence = _object(evidence_raw, "evidence")
        if evidence.get("report_type") != "daily" or evidence.get("execution_date") != period:
            raise ValueError("evidence must be daily with execution_date matching requested date")
        if evidence.get("timezone") != timezone_name:
            raise ValueError("evidence timezone does not match requested timezone")
        start = _timestamp(evidence.get("covered_from"), "covered_from")
        end = _timestamp(evidence.get("covered_until"), "covered_until")
        if start >= end:
            raise ValueError("covered_from must precede covered_until")
        if daily.get("covered_until") != evidence["covered_until"]:
            raise ValueError("state/evidence covered_until watermark mismatch")
        if end > datetime.now(timezone.utc) or requested > datetime.now(zone).date():
            raise ValueError("future cutoff or execution date refused")
        validation = evidence.get("validation")
        if type(validation) is not dict or validation.get("status") != "passed":
            raise ValueError("source validation.status must be passed")
        if validation.get("report_sha256") != digest(report_raw):
            raise ValueError("source validation report_sha256 mismatch")
        title, summary, body = VisibleReport().extract(report_raw)
        coverage = {"covered_from": evidence["covered_from"], "covered_until": evidence["covered_until"]}
        selected = _evidence_summary(evidence)
        snapshot = redact_data({"schema": "wa.report.v1", "origin": "external_daily_import",
            "project": p["name"], "project_id": p["id"], "date": period, "timezone": timezone_name,
            "source_hashes": {"report": digest(report_raw), "evidence": digest(evidence_raw), "state": digest(state_raw)},
            "coverage": coverage, "title": title, "summary": summary, "body": body,
            "scope": SCOPE, "tasks": [], "claims": [], "corrections": [], "evidence": selected})
        content = render_import(snapshot)
        # Re-read every input, including state, after extraction/rendering. Changed bytes,
        # disappeared files, or replaced symlinks invalidate the entire attempt.
        for relative, path, raw, limit in ((daily["last_report"], report_path, report_raw, MAX_FILE),
                                           (daily["last_evidence"], evidence_path, evidence_raw, MAX_FILE),
                                           ("state.json", state_path, state_raw, MAX_STATE)):
            after_path, after = _read(root, relative, limit)
            if after_path != path or after != raw:
                raise ValueError("source inputs changed during import; retry after collector finishes")
        with store.transaction():
            # Extra source metadata remains in the private journal, outside all outbound files.
            return save_report(store, p, period, content, metadata={"origin": "external_daily_import",
                "source_hashes": snapshot["source_hashes"], "coverage": coverage, "timezone": timezone_name,
                "source_root": str(root), "source_state": state, "source_evidence": evidence})
