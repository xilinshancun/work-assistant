import html
import json
import re
import os
import tempfile
from datetime import date
from pathlib import Path
from .evidence import verify_evidence
from .util import dumps, digest, redact, redact_data, atomic_write, outbound_guard

STYLE = """body{font:16px/1.65 system-ui,sans-serif;color:#172638;background:#f4f6fa;margin:0}main{max-width:960px;margin:auto;padding:24px}section,header{background:white;border-radius:12px;padding:20px;margin-bottom:18px}h1{font-size:28px}h2{font-size:21px}a{color:#225abc}pre{white-space:pre-wrap;overflow-wrap:anywhere}li{margin-bottom:10px;overflow-wrap:anywhere}.meta{color:#526074;font-size:14px}.bar{background:#e1e9f8;height:16px}.bar span{display:block;background:#446bd0;height:16px}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid #ddd}@media(max-width:600px){main{padding:10px}section{padding:14px}}@media print{body{background:white}section{break-inside:avoid}a{color:inherit}}"""


def escape(value):
    return html.escape(redact(str(value)), quote=True)


def md_text(value):
    # Keep untrusted Markdown from creating links, images or raw HTML.
    text = redact(str(value)).replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", text)


def document_blocks(text):
    # Native docx text blocks. Bound each run and each request; no Markdown interpretation.
    chunks = []
    for line in text.splitlines():
        line = line or " "
        chunks.extend(line[i:i + 900] for i in range(0, len(line), 900))
    if len(chunks) > 1000:
        raise ValueError("document too large; narrow report inputs")
    return [{"block_type": 2, "text": {"elements": [{"text_run": {"content": chunk}}]}} for chunk in chunks]


def render(snapshot):
    tasks, claims, evidence, corrections = (snapshot[k] for k in ("tasks", "claims", "evidence", "corrections"))
    title = "%s · %s Daily Sync" % (snapshot["project"], snapshot["date"])
    blocks = []
    accomplished = ["%s [%s] %s · %s" % (t["id"], t["status"], t["goal"],
                    "independently reviewed" if t["verified"] else "protocol completed; goal unverified") for t in tasks if t["status"] == "completed"]
    numeric = [c for c in claims if c["data"].get("numeric")]
    numbers = ["%s: %s %s [%s; %s; %s]" % (c["data"]["numeric"]["key"], c["data"]["numeric"]["value"],
        c["data"]["numeric"]["unit"], c["id"], c["data"]["grade"], c["data"]["attribution"]) for c in numeric]
    findings = ["%s [%s; attributed to %s] %s (sources: %s)" %
                (c["id"], c["data"]["grade"], c["data"]["attribution"], c["data"]["text"], ", ".join(c["data"]["evidence_ids"])) for c in claims]
    findings += ["Correction %s: %s %s; reason: %s; replacement: %s" % (r["id"], r["data"]["action"],
        r["data"]["claim_id"], r["data"]["reason"], r["data"].get("replacement") or "none") for r in corrections]
    blocked = ["%s [%s] %s" % (t["id"], t["status"], t["blockers"] or "no blocker recorded") for t in tasks if t["blockers"] or t["status"] in {"failed", "needs_user", "outcome_unknown"}]
    next_actions = ["%s [%s] %s" % (t["id"], t["status"], t["goal"]) for t in tasks if t["status"] not in {"completed", "cancelled"}]
    for label, lines in (("1. 完成事项 / Accomplishments", accomplished), ("2. 数字 / Numbers", numbers),
                         ("3. 发现与更正 / Findings", findings), ("4. 协作与阻塞 / Blockers", blocked),
                         ("5. 下一步 / Next actions", next_actions)):
        blocks.append((label, lines))
    scope = "Frozen local snapshot. Progress unknown unless sourced. Reported findings are model/operator claims. Source/hash/value checks do not imply scientific reproduction."
    markdown = "# " + md_text(title) + "\n\n" + scope + "\n"
    body = "<header><h1>" + escape(title) + "</h1><p class=meta>" + scope + "</p><a href='evidence.html'>Evidence / 证据明细</a></header>"
    plain = title + "\n" + scope + "\n"
    for label, lines in blocks:
        effective = lines or ["无记录 / No recorded source"]
        markdown += "\n## " + label + "\n\n" + "\n".join("- " + md_text(line) for line in effective) + "\n"
        body += "<section><h2>" + label + "</h2><ul>" + "".join("<li>" + escape(line) + "</li>" for line in effective) + "</ul></section>"
        plain += "\n" + label + "\n" + "\n".join(effective) + "\n"
    if numeric:
        body += "<section><h2>源数据 / Source values</h2><p>Bar lengths compare absolute values sharing the same unit.</p>"
        for c in numeric:
            n = c["data"]["numeric"]
            maximum = max(abs(v["data"]["numeric"]["value"]) for v in numeric if v["data"]["numeric"]["unit"] == n["unit"]) or 1
            width = round(abs(n["value"]) / maximum * 100, 2)
            body += "<p>%s: %s %s <a href='evidence.html#%s'>%s</a></p><div class=bar><span style='width:%s%%'></span></div>" % (escape(n["key"]), escape(n["value"]), escape(n["unit"]), escape(c["id"]), escape(c["id"]), width)
        body += "</section>"
    details = "<header><h1>Evidence / 证据明细</h1><a href='index.html'>Daily Sync</a></header>"
    for claim in claims:
        details += "<section id='%s'><h2>%s</h2><pre>%s</pre></section>" % (escape(claim["id"]), escape(claim["id"]), escape(json.dumps(claim["data"], ensure_ascii=False, indent=2)))
    for item in evidence:
        details += "<section id='%s'><h2>%s</h2><pre>%s</pre></section>" % (escape(item["id"]), escape(item["id"]), escape(json.dumps(item["data"], ensure_ascii=False, indent=2)))
    def page(content):
        return "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>" + escape(title) + "</title><style>" + STYLE + "</style><main>" + content + "</main></html>"
    # Card is a compact native plain-text summary; the full document contains all five blocks.
    card = {"config": {"wide_screen_mode": True, "update_multi": True}, "header": {"title": {"tag": "plain_text", "content": redact(title)[:200]}},
            "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": redact(label + "\n" + "\n".join(lines or ["无记录"]))[:1800]}} for label, lines in blocks]}
    card_html = "<header><h1>" + escape(title) + "</h1><p>Feishu native text card · local preview</p><a href='index.html'>完整报告 / Full report</a></header>"
    for component in card["elements"]:
        card_html += "<section><pre>" + escape(component["text"]["content"]) + "</pre></section>"
    return {"index.html": page(body), "daily-sync.md": markdown, "evidence.html": page(details),
            "evidence.json": dumps(snapshot), "card.json": dumps(card),
            "document.json": dumps({"title": redact(title), "children": document_blocks(redact(plain))}),
            "card-preview.html": page(card_html)}


def generate_report(store, project, period, output=None, renderer=render):
    date.fromisoformat(period)
    p = store.project(project)
    # Reports have no external effects. flock releases on SIGKILL; a durable lease
    # would strand collection despite SQLite rolling back the snapshot/cursor.
    resource = "report:" + p["id"]
    with store.inspection_lock([resource]):
        with store.transaction():
            store.db.execute("DELETE FROM locks WHERE resource=?", (resource,))
            cutoff = store.db.execute("SELECT COALESCE(MAX(seq),0) FROM records").fetchone()[0]
            tasks = store.tasks(p["id"])
            claims = [c for c in store.records("claim", project_id=p["id"], cutoff=cutoff) if c["data"]["status"] == "active"]
            all_claims = {c["id"]: c for c in store.records("claim", project_id=p["id"], cutoff=cutoff)}
            # Derived assertions become stale when any input is retracted/superseded.
            def current(claim, seen=None):
                seen = set() if seen is None else seen
                if claim["id"] in seen:
                    return False
                seen = seen | {claim["id"]}
                return claim["data"]["status"] == "active" and all(i in all_claims and current(all_claims[i], seen) for i in claim["data"].get("inputs", []))
            claims = [c for c in claims if current(c)]
            evidence = store.records("evidence", project_id=p["id"], cutoff=cutoff)
            for item in evidence:
                verify_evidence(item)
            corrections = store.records("correction", project_id=p["id"], cutoff=cutoff)
            if len(tasks) + len(claims) + len(corrections) + len(evidence) > 1000:
                raise ValueError("snapshot exceeds 1000 records; split project scope")
            # No generation timestamp or report/event seq in content: identical facts yield identical hashes.
            snapshot = {"schema": "wa.report.v1", "project": p["name"], "project_id": p["id"], "date": period,
                "scope": "all known project facts at snapshot cutoff; corrections included regardless of original period",
                "tasks": tasks, "claims": claims, "evidence": evidence, "corrections": corrections}
            outbound = redact_data(snapshot)
            # Absolute copied artifact locations never leave the private journal.
            for item in outbound["evidence"]:
                item["data"]["artifact"] = "private artifact (hash verified)"
                source = next(e for e in evidence if e["id"] == item["id"])
                raw = Path(source["data"]["artifact"]).read_bytes()
                try:
                    item["data"]["observation_preview"] = redact(raw[:32768].decode("utf-8"))
                    item["data"]["preview_truncated"] = len(raw) > 32768
                except UnicodeError:
                    item["data"]["observation_preview"] = "binary artifact; exact bytes retained privately"
            content = renderer(outbound)
            record = save_report(store, p, period, content, output, {"cutoff": cutoff})
            if record["seq"] > cutoff:
                store.db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(project_id) DO UPDATE SET seq=excluded.seq", (p["id"], cutoff))
            return record


def save_report(store, project, period, content, output=None, metadata=None):
    """Save the standard immutable bundle inside the caller's report lock/transaction."""
    for text in content.values():
        outbound_guard(text)
    expected = {"index.html", "daily-sync.md", "evidence.html", "evidence.json", "card.json", "document.json", "card-preview.html"}
    if set(content) != expected:
        raise ValueError("renderer did not produce all report outputs")
    content_hash = digest(content)
    existing = next((r for r in store.records("report", project_id=project["id"])
                     if r["data"]["hash"] == content_hash), None)
    if existing:
        restore_missing_report(existing, content)
        return load_report(store, existing["id"])
    base = Path(output).expanduser().resolve() if output else store.root / "reports"
    target = base / (project["id"] + "-" + period + "-" + content_hash[:16])
    if target.exists():
        for name, text in content.items():
            if not (target / name).is_file() or (target / name).read_text(encoding="utf-8") != text:
                raise ValueError("existing snapshot is incomplete or modified")
    else:
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".wa-report-", dir=str(base)) as staging:
            stage = Path(staging) / "snapshot"
            stage.mkdir()
            for name, text in content.items():
                atomic_write(stage / name, text)
            os.replace(str(stage), str(target))
    return store.record("report", dict(metadata or {}, hash=content_hash, path=str(target), period=period,
        files={name: digest(text.encode("utf-8")) for name, text in content.items()}), project_id=project["id"])


def restore_missing_report(report, content):
    """Restore only absent files from identical regenerated content; never repair tampering."""
    manifest = {name: digest(text.encode("utf-8")) for name, text in content.items()}
    if manifest != report["data"]["files"]:
        raise ValueError("immutable report manifest differs from regenerated content")
    target = Path(report["data"]["path"])
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError("immutable report modified: snapshot directory")
    missing = []
    # Check every extant file before writing anything, including when another file is missing.
    for name, expected in manifest.items():
        path = target / name
        if path.is_symlink() or (path.exists() and (not path.is_file() or digest(path.read_bytes()) != expected)):
            raise ValueError("immutable report modified: " + name)
        if not path.exists():
            missing.append(name)
    if not missing:
        return
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wa-restore-", dir=str(target.parent)) as staging:
        for name in missing:
            staged = Path(staging) / name
            atomic_write(staged, content[name])
            # A hard link installs a complete file without replacing any concurrent writer.
            try:
                os.link(str(staged), str(target / name))
            except FileExistsError:
                path = target / name
                if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != manifest[name]:
                    raise ValueError("immutable report modified during restoration: " + name)


def load_report(store, ident):
    report = store.get(ident)
    if report["kind"] != "report":
        raise ValueError("expected report ID")
    for name, expected in report["data"]["files"].items():
        path = Path(report["data"]["path"]) / name
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError("immutable report modified or missing: " + name)
    return report
