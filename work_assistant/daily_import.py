"""Import an explicitly selected, validated daily bundle. No providers or network."""
import html
import os
import re
import stat
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .reports import STYLE, save_report
from .util import digest, dumps, outbound_guard, strict_json

MAX_STATE = 1024 * 1024
MAX_FILE = 20 * 1024 * 1024
MAX_SUMMARY = 6000
BLOCK_TYPES = {"text": 2, **{"heading%s" % level: level + 2 for level in range(1, 7)}, "bullet": 12}
MAX_NATIVE_TEXT = 10000
SCOPE = ("源报告陈述 / Reported source assertions. 导入仅核对日期、水位和文件哈希；"
         "不代表独立测量或科学复现 / not independently measured or reproduced.")


def export_data(value):
    """Keep source semantics intact. Reject recognizable secrets/paths, never rewrite prose.

    This deliberately does not use the general-purpose redact() regex or read secrets
    from the environment. A rejection names the category, never echoes sensitive text.
    """
    if type(value) is str:
        outbound_guard(value)
        def url_paths(match):
            try:
                url = urlsplit(match.group(0))
            except ValueError:
                raise ValueError("daily export contains a malformed URL; cannot check paths safely") from None
            parameters = url.query + " " + url.fragment
            for _ in range(8):
                decoded = unquote(parameters)
                if decoded == parameters:
                    return " " + parameters + " "
                parameters = decoded
            raise ValueError("daily export URL parameters are too deeply encoded to check safely")
        path_text = re.sub(r"https?://[^\s<>\"']+", url_paths, value, flags=re.I)
        if re.search(r"(?i)(?:file://|(?:^|[\s\"'(<])~/|\b[A-Z]:[\\/]|\\\\[^\s\\]+\\|"
                     r"/(?:Users|home|private|tmp|var|etc|opt|srv|root|mnt|Volumes)/)", path_text):
            raise ValueError("daily export contains a local path; remove it from the source text")
        if re.search(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{15,})\b|"
                     r"(?i:bearer\s+[A-Za-z0-9_./+=-]{16,}|https?://[^\s<>]*/(?:bot/v2/hook|webhook)/[^\s<>]+)", value):
            raise ValueError("daily export contains an explicit credential; remove it from the source text")
        # Numeric token counts and prose such as password=至少12位 are not credentials.
        assignments = re.finditer(r"(?i)\b(?:api[_-]?key|app[_-]?secret|access[_-]?token|refresh[_-]?token|"
                                  r"token|password|secret)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_./+=-]{12,})", value)
        if any(re.search(r"[A-Za-z]", match.group(1)) for match in assignments):
            raise ValueError("daily export contains a credential assignment; remove it from the source text")
    elif type(value) is dict:
        for item in value.values():
            export_data(item)
    elif type(value) is list:
        for item in value:
            export_data(item)
    return value


def escape(value):
    return html.escape(str(value), quote=True)


def http_link(href):
    if not re.match(r"(?i)^https?://", href):
        return None
    _text(href, "HTTP link", 8192)
    try:
        target = urlsplit(href)
        if not target.hostname or target.username is not None or target.password is not None:
            raise ValueError()
    except ValueError:
        raise ValueError("HTTP link must be absolute and contain no credentials") from None
    if any(c.isspace() or ord(c) < 32 for c in href):
        raise ValueError("HTTP link contains unsupported whitespace/control characters")
    export_data(href)  # Check the original URL, including query/fragment, before encoding.
    # Official Docx Link.url requires the entire URL to be URL-encoded.
    return {"url": quote(href, safe="")}


def md_text(value):
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", str(value))


def card_md_text(value):
    # Feishu card Markdown requires HTML entities for literal syntax characters.
    return "".join("&#%s;" % ord(c) if c in "\\`*_{}[]()<>~#!|&" else c for c in value)


def block_text(block):
    key = next(key for key, number in BLOCK_TYPES.items() if number == block["block_type"])
    return "".join(e["text_run"]["content"] for e in block[key]["elements"])


def styled_markdown(elements, card=False, outer_bold=False):
    result = []
    for element in elements:
        run = element["text_run"]
        text = card_md_text(run["content"]) if card else md_text(run["content"])
        style = run.get("text_element_style", {})
        if style.get("inline_code"):
            text = "`" + text + "`"
        if style.get("italic"):
            text = "*" + text + "*"
        if style.get("bold") and not outer_bold:
            text = "**" + text + "**"
        if style.get("strikethrough"):
            text = "~~" + text + "~~"
        result.append(text)
    return "".join(result)


def element_html(elements):
    result = []
    for element in elements:
        run = element["text_run"]
        part = escape(run["content"])
        style = run.get("text_element_style", {})
        for key, tag in (("italic", "em"), ("bold", "strong"), ("strikethrough", "del"),
                         ("underline", "u"), ("inline_code", "code")):
            if style.get(key):
                part = "<%s>%s</%s>" % (tag, part, tag)
        if style.get("link"):
            part = '<a href="' + escape(unquote(style["link"]["url"])) + '">' + part + "</a>"
        result.append(part)
    return "".join(result)


def coverage_text(start, cutoff, zone_name):
    def label(stamp):
        year = "%s年" % stamp.year if start.year != cutoff.year else ""
        return year + "%s月%s日 %s" % (stamp.month, stamp.day, stamp.strftime("%H:%M"))
    return "%s—%s · %s" % (label(start), label(cutoff), "北京时间" if zone_name == "Asia/Shanghai" else zone_name)


def native_html(blocks):
    result, in_list = [], False
    for block in blocks:
        kind = next(key for key, number in BLOCK_TYPES.items() if number == block["block_type"])
        if kind == "bullet" and not in_list:
            result.append("<ul>")
            in_list = True
        elif kind != "bullet" and in_list:
            result.append("</ul>")
            in_list = False
        tag = "h" + kind[7:] if kind.startswith("heading") else {"bullet": "li", "text": "p"}[kind]
        text = element_html(block[kind]["elements"])
        result.append("<%s class='source-line'>%s</%s>" % (tag, text, tag))
    return "".join(result) + ("</ul>" if in_list else "")


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
    excluded = {"head", "script", "style", "nav", "template", "noscript",
                "iframe", "object", "embed", "svg", "canvas"}
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}
    blocks = {"address", "article", "aside", "blockquote", "br", "dd", "details", "div",
              "dl", "dt", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6",
              "header", "footer", "hr", "li", "main", "ol", "p", "pre", "section", "summary",
              "caption", "table", "tr", "ul"}
    optional = {"p", "li", "dt", "dd", "rt", "rp", "option", "optgroup", "colgroup",
                "thead", "tbody", "tfoot", "tr", "td", "th"}
    p_closers = (blocks - {"br", "caption", "summary"}) | {"li", "dt", "dd", "fieldset", "form", "menu"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.body, self.summary, self.title = [], [], []
        self.summary_count = 0
        self.title_seen = False
        self.tree = []

    def _active(self):
        return (any(e[0] == "body" for e in self.stack)
                and not any(e[1] for e in self.stack))

    def _append(self, text, preserve=False):
        if self._active():
            self.body.append((text, preserve))
            if any(e[2] for e in self.stack):
                self.summary.append((text, preserve))
            if any(e[3] for e in self.stack):
                self.title.append((text, preserve))

    def _close(self, index):
        if any(e[0] not in self.optional for e in self.stack[index + 1:]):
            raise ValueError("ambiguous HTML nesting; close nonoptional elements explicitly")
        if self.stack[index][0] in self.blocks:
            self._append("\n")
        del self.stack[index:]

    def _implicit_close(self, targets, boundaries=()):
        for index in range(len(self.stack) - 1, -1, -1):
            tag = self.stack[index][0]
            if tag in boundaries:
                break
            if tag in targets:
                self._close(index)
                break

    def handle_starttag(self, tag, attrs):
        if tag in self.p_closers:
            self._implicit_close({"p"})
        if tag == "li":
            self._implicit_close({"li"}, {"ul", "ol"})
        elif tag in {"dt", "dd"}:
            self._implicit_close({"dt", "dd"}, {"dl"})
        elif tag in {"td", "th"}:
            self._implicit_close({"td", "th"}, {"tr", "table"})
        elif tag == "tr":
            self._implicit_close({"tr"}, {"table", "thead", "tbody", "tfoot"})
        elif tag in {"thead", "tbody", "tfoot"}:
            self._implicit_close({"thead", "tbody", "tfoot"}, {"table"})
        attrs = {name: value or "" for name, value in attrs}
        hidden = (tag in self.excluded or "hidden" in attrs
                  or attrs.get("aria-hidden", "").lower() == "true"
                  or attrs.get("role", "").lower() == "navigation"
                  or re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
                               attrs.get("style", ""), re.I))
        summary = attrs.get("id") == "sync-summary"
        if tag in self.blocks:
            self._append("\n")
        if tag in {"td", "th"}:
            self._append(" | ")
        first_title = tag == "h1" and not hidden and self._active() and not self.title_seen
        self.title_seen = self.title_seen or first_title
        node = {"tag": tag, "hidden": bool(hidden), "summary": summary, "title": first_title, "children": []}
        if tag == "a":
            node["href"] = attrs.get("href", "").strip()
        (self.stack[-1][4]["children"] if self.stack else self.tree).append(node)
        self.stack.append((tag, bool(hidden), summary, first_title, node))
        if summary and self._active():
            self.summary_count += 1
        if tag in self.void:
            self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.void:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self._close(index)
                return
        # An explicit optional end tag may follow an implicit close (e.g. </p>).
        if tag not in self.optional:
            raise ValueError("ambiguous HTML nesting; unmatched closing element")

    def handle_data(self, data):
        if self._active():
            self.stack[-1][4]["children"].append(data)
        preserve = any(e[0] == "pre" for e in self.stack)
        self._append(data.replace("\r\n", "\n").replace("\r", "\n") if preserve
                     else re.sub(r"\s+", " ", data), preserve)

    @staticmethod
    def clean(parts):
        lines, current, preserved = [], "", False
        for text, preserve in parts:
            pieces = text.split("\n")
            for index, piece in enumerate(pieces):
                current += piece
                preserved = preserved or preserve
                if index < len(pieces) - 1:
                    if preserved or current.strip():
                        lines.append(current if preserved else current.strip())
                    current, preserved = "", False
        if preserved or current.strip():
            lines.append(current if preserved else current.strip())
        return "\n".join(lines)

    def extract(self, raw):
        try:
            self.feed(raw.decode("utf-8"))
            self.close()
        except UnicodeError:
            raise ValueError("source report must be UTF-8 HTML") from None
        if any(e[0] not in self.optional | {"html", "head", "body"} for e in self.stack):
            raise ValueError("ambiguous HTML nesting; unclosed nonoptional elements")
        title, summary, body = (self.clean(p) for p in (self.title, self.summary, self.body))
        _text(title, "visible body h1 title", 200)
        _text(summary, "visible sync-summary", MAX_SUMMARY)
        _text(body, "visible report body", MAX_FILE)
        if self.summary_count != 1:
            raise ValueError("source report requires exactly one visible sync-summary")
        return title, summary, body

    def semantic_blocks(self, body_scope="strict"):
        if type(body_scope) is not str or body_scope not in {"strict", "main"}:
            raise ValueError("body_scope must be strict or main")
        mains = []
        def find(nodes):
            for node in nodes:
                if type(node) is dict and not node["hidden"]:
                    if node["tag"] == "main":
                        mains.append(node)
                    find(node["children"])
        find(self.tree)
        if len(mains) != 1:
            raise ValueError("source requires exactly one visible main for native delivery")
        def check_outside(nodes, parent=""):
            for node in nodes:
                if type(node) is str:
                    if node.strip():
                        raise ValueError("visible content outside main is unsupported; move business content into main")
                    continue
                if node["hidden"] or node is mains[0] or node["title"]:
                    continue
                check_outside(node["children"], node["tag"])
        if body_scope == "strict":
            check_outside(self.tree)
        result, summary_parts, summary_elements = [], [], []
        inline_tags = {"strong", "b", "em", "i", "span", "a", "code", "s", "small", "u", "br", "wbr",
                       "mark", "time", "abbr", "kbd", "del", "ins", "label", "var", "samp", "cite", "dfn", "bdi", "bdo"}
        containers = {"main", "section", "article", "header", "footer", "div", "ul", "ol", "li", "dl", "dt", "dd",
                      "address", "blockquote", "figure", "figcaption", "details", "table", "thead", "tbody", "tfoot"}
        leaf_blocks = {"h%s" % level for level in range(1, 7)} | {"summary", "p", "caption", "tr", "pre"}
        cell_blocks = leaf_blocks | {"div", "section", "article", "header", "li"}

        def inline(node, style=None, preserve=False):
            style = dict(style or {})
            if type(node) is str:
                return [(node.replace("\r\n", "\n").replace("\r", "\n") if preserve
                         else re.sub(r"\s+", " ", node), style)]
            if node["hidden"]:
                return []
            tag = node["tag"]
            if tag not in inline_tags | leaf_blocks | containers | {"td", "th"}:
                raise ValueError("unsupported HTML element in native content; cannot preserve its semantics")
            if tag in {"strong", "b"}:
                style["bold"] = True
            if tag in {"em", "i"}:
                style["italic"] = True
            if tag in {"s", "del"}:
                style["strikethrough"] = True
            if tag in {"u", "ins"}:
                style["underline"] = True
            if tag in {"code", "kbd", "samp"}:
                style["inline_code"] = True
            if tag == "a":
                link = http_link(node.get("href", ""))
                if link:
                    style["link"] = link
            if tag == "br":
                return [("\n", style)]
            runs, previous_block = [], False
            for child in node["children"]:
                child_runs = inline(child, style, preserve)
                child_block = type(child) is dict and child["tag"] in cell_blocks
                if child_runs and any(t.strip() for t, _ in child_runs):
                    if (child_block or previous_block) and any(t.strip() for t, _ in runs):
                        runs.append(("; ", {}))
                    previous_block = child_block
                runs.extend(child_runs)
            return ([(" | ", {})] if tag in {"td", "th"} else []) + runs

        def emit(kind, runs, in_summary=False, preserve=False):
            runs = [(text, style) for text, style in runs if text]
            if not preserve:
                while runs and not runs[0][0].strip():
                    runs.pop(0)
                while runs and not runs[-1][0].strip():
                    runs.pop()
                if runs:
                    runs[0] = (runs[0][0].lstrip(), runs[0][1])
                    runs[-1] = (runs[-1][0].rstrip(), runs[-1][1])
            if not runs:
                return
            text = "".join(t for t, _ in runs)
            if len(text) > MAX_NATIVE_TEXT or len(runs) > 1000:
                raise ValueError("native block exceeds 10000 characters or 1000 text runs; narrow source")
            elements = []
            for text_part, style in runs:
                run = {"content": text_part}
                if style:
                    run["text_element_style"] = style
                elements.append({"text_run": run})
            result.append({"block_type": BLOCK_TYPES[kind], kind: {"elements": elements}})
            if in_summary and kind in {"text", "bullet"}:
                summary_parts.append(text)
                summary_elements.append(elements)
            if len(result) > 999:  # One additional coverage block is generated below.
                raise ValueError("document too large; native delivery supports at most 1000 blocks")

        def walk(node, in_summary=False, inherited_kind="text"):
            if node["hidden"] or node["title"]:
                return
            tag = node["tag"]
            if tag not in containers | leaf_blocks | inline_tags | {"hr"}:
                raise ValueError("unsupported HTML element in native content; cannot preserve its semantics")
            if tag == "hr":
                return
            in_summary = in_summary or node["summary"]
            if tag in leaf_blocks:
                kind = ("heading" + tag[1:] if tag.startswith("h") else
                        "heading2" if tag == "summary" else inherited_kind)
                if tag == "p" and in_summary:
                    kind = "bullet"
                if tag == "pre":
                    # Use hard paragraph boundaries so blank lines are not lost as soft breaks.
                    line = []
                    for text, style in inline(node, preserve=True):
                        parts = text.split("\n")
                        for index, part in enumerate(parts):
                            if part:
                                line.append((part, style))
                            if index < len(parts) - 1:
                                emit("text", line or [(" ", {})], preserve=True)
                                line = []
                    emit("text", line or [(" ", {})], preserve=True)
                else:
                    emit(kind, inline(node), in_summary)
                return
            kind = "bullet" if tag == "li" else inherited_kind
            pending = []
            for child in node["children"]:
                if type(child) is str or child["tag"] in inline_tags:
                    pending.extend(inline(child))
                else:
                    emit(kind, pending, in_summary)
                    pending = []
                    walk(child, in_summary, kind if tag == "li" else "text")
            emit(kind, pending, in_summary)
        walk(mains[0])
        if not result or not summary_parts:
            raise ValueError("native delivery requires main content and a summary content paragraph")
        return result, summary_parts, summary_elements


def _evidence_summary(evidence):
    sources, items, limitations = evidence.get("sources"), evidence.get("items", []), evidence.get("limitations")
    if any(type(v) is not list for v in (sources, items, limitations)):
        raise ValueError("evidence sources/items/limitations must be lists")
    mapping = evidence.get("source_mapping", {})
    if type(mapping) is not dict:
        raise ValueError("evidence source_mapping must be an object")
    observations = evidence.get("pr_observations", [])
    if type(observations) is not list:
        raise ValueError("evidence pr_observations must be a list")
    if len(sources) + len(items) + len(limitations) + len(mapping) + len(observations) > 1000:
        raise ValueError("evidence exceeds 1000 entries")
    selected_sources, selected_items, ids = [], [], set()
    for source in sources:
        if type(source) is not dict:
            raise ValueError("evidence source must be an object")
        ident = _text(source.get("id"), "source id", 200)
        if ident in ids:
            raise ValueError("duplicate evidence source id")
        ids.add(ident)
        summary = (_text(source["summary"], "source summary") if "summary" in source
                   else "源未提供摘要 / No source summary supplied")
        verified_at = (_text(source["verified_at"], "source verified_at", 100) if "verified_at" in source
                       else "源未提供验证时间 / Not supplied")
        selected_sources.append({"id": ident, "kind": "source", "grade": "reported", "summary": summary,
                                 "verified_at": verified_at})
    selected_observations = []
    for observation in observations:
        if type(observation) is not dict:
            raise ValueError("PR observation must be an object")
        ident = _text(observation.get("id"), "PR observation id", 200)
        if ident in ids:
            raise ValueError("duplicate evidence source id across sources/PR observations")
        ids.add(ident)
        selected_observations.append({"id": ident, "kind": "pr_observation",
            "verified_at": _text(observation.get("verified_at"), "PR observation verified_at", 100)})
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
    selected = {"sources": selected_sources, "items": selected_items,
                "limitations": [_text(v, "limitation") for v in limitations]}
    if "pr_observations" in evidence:
        selected["pr_observations"] = selected_observations
    if "source_mapping" in evidence:
        for section, refs in mapping.items():
            _text(section, "source_mapping section", 200)
            if (type(refs) is not list or not refs or len(refs) > 1000
                    or any(type(ref) is not str or ref not in ids for ref in refs)):
                raise ValueError("source_mapping requires nonempty arrays of existing source IDs")
        selected.update(source_mapping=mapping, source_mapping_grade="reported")
    if len(dumps(selected).encode("utf-8")) > 65536:
        raise ValueError("outgoing evidence summary exceeds 64 KiB; narrow source summaries")
    return selected


def render_import(snapshot):
    title, summary, body, coverage, selected = (snapshot[k] for k in
                                              ("title", "summary", "body", "coverage", "evidence"))
    zone = ZoneInfo(snapshot["timezone"])
    start, cutoff = (_timestamp(coverage[k], k).astimezone(zone) for k in ("covered_from", "covered_until"))
    interval = coverage_text(start, cutoff, snapshot["timezone"])
    evidence_lines = ["证据摘要 / Evidence (reported)"]
    evidence_lines += ["%s: %s (sources: %s)" % (i["id"], i["claim"], ", ".join(i["source_ids"]))
                       for i in selected["items"]]
    evidence_lines += ["%s [reported]: %s (source verified_at: %s)" %
                       (s["id"], s["summary"], s["verified_at"]) for s in selected["sources"]]
    evidence_lines += ["%s [pr_observation] (source verified_at: %s; supplied by source)" %
                       (s["id"], s["verified_at"]) for s in selected.get("pr_observations", [])]
    if "source_mapping" in selected:
        evidence_lines += ["章节来源映射 / Section source mapping (reported)"]
        evidence_lines += ["%s → %s" % (section, ", ".join(refs))
                           for section, refs in selected["source_mapping"].items()]
    evidence_lines += ["局限 / Limitations"] + (selected["limitations"] or ["源未列出 / None supplied by source"])
    evidence_text = "\n".join(evidence_lines)
    coverage_block = {"block_type": 2, "text": {"elements": [{"text_run": {"content": interval}}]}}
    document = {"title": title, "children": [coverage_block] + snapshot["native_blocks"]}
    time_hint = cutoff.strftime("截至 %m-%d %H:%M")
    summary_paragraphs = snapshot["summary_paragraphs"]
    lead, bullets = summary_paragraphs[0], summary_paragraphs[1:]
    summary_elements = snapshot["summary_elements"]
    bullet_divs = []
    for text, elements in zip(bullets, summary_elements[1:]):
        struck = any(e["text_run"].get("text_element_style", {}).get("strikethrough") for e in elements)
        bullet_divs.append({"tag": "div", "text": {"tag": "lark_md" if struck else "plain_text",
                           "content": "• " + (styled_markdown(elements, card=True) if struck else text)}})
    signature = "工作助理 · %s · %s" % (snapshot["project"], time_hint)
    card = {"config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "**" + styled_markdown(summary_elements[0], card=True, outer_bold=True) + "**"}},
                         {"tag": "hr"}]
                         + bullet_divs
                         + [{"tag": "note", "elements": [{"tag": "plain_text", "content": signature}]}]}
    # The publisher inserts the remote document's default-outline button before the final note.

    def paragraphs(text):
        return "".join("<p class='source-line'>" + escape(line) + "</p>" for line in text.split("\n"))

    def page(text):
        return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'><title>"
                + escape(title) + "</title><style>" + STYLE
                + ".source-line{white-space:pre-wrap}</style></head><body><main>"
                + text + "</main></body></html>")

    header = "<header><h1>" + escape(title) + "</h1><p>" + escape(interval) + "</p></header>"
    return {"index.html": page(header + "<section>" + native_html(snapshot["native_blocks"])
                                + "</section><a href='evidence.html'>证据 / Evidence</a>"),
            "daily-sync.md": "# " + md_text(title) + "\n\n" + md_text(interval)
                             + "\n\n" + "\n\n".join(
                                 (("#" * (b["block_type"] - 2) + " " if 3 <= b["block_type"] <= 8 else
                                   "- " if b["block_type"] == 12 else "") +
                                  styled_markdown(b[next(k for k, n in BLOCK_TYPES.items() if n == b["block_type"])]["elements"]))
                                 for b in snapshot["native_blocks"]) + "\n",
            "evidence.html": page(header + "<p>" + escape(SCOPE) + "</p><section>" + paragraphs(evidence_text)
                                  + "</section><a href='index.html'>完整报告 / Full report</a>"),
            "evidence.json": dumps(snapshot), "card.json": dumps(card), "document.json": dumps(document),
            "card-preview.html": page("<header style='background:#225abc;color:white'><h1>" + escape(title)
                                      + "</h1></header><section><p><strong>" + element_html(summary_elements[0]) + "</strong></p><hr>"
                                      + "<ul>" + "".join("<li>" + element_html(elements) + "</li>" for elements in summary_elements[1:]) + "</ul>"
                                      + "<a style='display:inline-block;border:1px solid #bbb;border-radius:6px;padding:8px 16px'"
                                      + " href='index.html'>打开 Daily Sync</a><p class='meta'>" + escape(signature) + "</p></section>")}


def import_daily(store, project, source_root, period, timezone_name="Asia/Shanghai", title=None, body_scope="strict"):
    if type(body_scope) is not str or body_scope not in {"strict", "main"}:
        raise ValueError("body_scope must be strict or main")
    if title is not None:
        title = _text(title, "display title", 200).strip()
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
        dates = [evidence[key] for key in ("execution_date", "report_date") if key in evidence]
        if evidence.get("report_type") != "daily" or not dates or any(value != period for value in dates):
            raise ValueError("evidence must be daily with execution_date/report_date matching requested date; aliases must agree")
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
        if end.astimezone(zone).date() != requested:
            raise ValueError("covered_until date in requested timezone must match requested date")
        validation = evidence.get("validation")
        if type(validation) is not dict or validation.get("status") != "passed":
            raise ValueError("source validation.status must be passed")
        if validation.get("report_sha256") != digest(report_raw):
            raise ValueError("source validation report_sha256 mismatch")
        parsed = VisibleReport()
        source_title, summary, body = parsed.extract(report_raw)
        coverage = {"covered_from": evidence["covered_from"], "covered_until": evidence["covered_until"]}
        native_blocks, summary_paragraphs, summary_elements = parsed.semantic_blocks(body_scope)
        selected = _evidence_summary(evidence)
        snapshot = export_data({"schema": "wa.report.v1", "origin": "external_daily_import",
            "project": p["name"], "project_id": p["id"], "date": period, "timezone": timezone_name,
            "source_hashes": {"report": digest(report_raw), "evidence": digest(evidence_raw), "state": digest(state_raw)},
            "coverage": coverage, "title": source_title if title is None else title,
            "source_title": source_title, "summary": summary, "body": body,
            "native_blocks": native_blocks, "summary_paragraphs": summary_paragraphs,
            "summary_elements": summary_elements,
            "import_options": {"body_scope": body_scope},
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
                "import_options": snapshot["import_options"],
                "source_root": str(root), "source_state": state, "source_evidence": evidence})
