"""Entirely synthetic local bundles and injected HTTP protocol fixtures."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from work_assistant import daily_import
from work_assistant.daily_import import import_daily
from work_assistant.feishu import AmbiguousError, preview, publish, reconcile
from work_assistant.reports import load_report
from work_assistant.util import digest, dumps
from test_runtime import Base


PERIOD = "2026-01-15"
HTML = """<!doctype html><html><head><title>忽略 head</title><script>head-active</script></head>
<body><header><h1>合成项目 · 每日进展</h1><p>2026-01-15</p>
<p>区间：2026-01-14 17:30 至 2026-01-15 17:30</p></header><main>
<section id="sync-summary"><h2>今日摘要</h2><p>完成合成检查，<strong>结论仍有限</strong>。</p></section>
<section><h2>详细结果</h2><p>合成样本十个，尚未独立复现。&lt;unsafe&gt;</p>
<ul><li>保留第一条中文详情。</li><li>保留第二条限定：仅适用测试环境。</li></ul>
<details><summary>展开补充</summary><p>折叠的完整细节也须保留。</p></details>
<script>body-active</script><style>style-active</style><iframe>frame-active</iframe>
<p hidden>hidden-active</p><p style="display:none">display-active</p>
<a href="javascript:alert(1)" onclick="evil()">链接文字</a>
</section></main><footer><nav>footer-navigation</nav></footer><nav>main-navigation</nav></body></html>"""


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.fail_card = False

    def __call__(self, method, url, body=None, headers=None):
        self.calls.append((method, url, body))
        if url.endswith("/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "fixture-token"}
        if url.endswith("/docx/v1/documents"):
            data = {"document": {"document_id": "fixture-doc"}}
        elif url.endswith("/children"):
            data = {"children": [{"block_id": "fixture-block-%s" % len(self.calls)}]}
        elif "/im/v1/messages?" in url:
            if self.fail_card:
                self.fail_card = False
                raise AmbiguousError("synthetic ambiguous send")
            data = {"message_id": "fixture-message"}
        else:
            data = {"content": "fixture content"}
        return {"code": 0, "data": data}


class DailyImportTests(Base):
    def setUp(self):
        super().setUp()
        self.bundle = self.root / "synthetic-bundle"
        (self.bundle / "daily").mkdir(parents=True)
        (self.bundle / "evidence").mkdir()
        self.report_path = self.bundle / "daily" / (PERIOD + ".html")
        self.evidence_path = self.bundle / "evidence" / ("daily-" + PERIOD + ".json")
        self.state_path = self.bundle / "state.json"
        self.evidence = {"report_type": "daily", "execution_date": PERIOD, "timezone": "Asia/Shanghai",
            "covered_from": "2026-01-14T17:30:00+08:00", "covered_until": "2026-01-15T17:30:00+08:00",
            "validation": {"status": "passed", "report_sha256": ""},
            "items": [{"id": "i1", "claim": "十个合成样本；仅为源陈述", "source_ids": ["s1"],
                       "private_extra": "private-item-metadata", "grade": "measured"}],
            "sources": [{"id": "s1", "summary": "合成日志摘要 token=synthetic-private-secret",
                         "verified_at": "2026-01-15T17:00:00+08:00", "extra": "private-source-metadata"}],
            "limitations": ["尚未独立复现，仅适用合成测试。"], "extra": {"opaque": "private-top-metadata"}}
        self.state = {"timezone": "Asia/Shanghai", "daily": {"last_successful_execution_date": PERIOD,
            "covered_until": self.evidence["covered_until"], "last_report": "daily/" + PERIOD + ".html",
            "last_evidence": "evidence/daily-" + PERIOD + ".json"}}
        self.write_bundle()

    def write_bundle(self, html=HTML):
        self.report_path.write_bytes(html.encode("utf-8"))
        self.evidence["validation"]["report_sha256"] = digest(self.report_path.read_bytes())
        self.write_metadata()

    def write_metadata(self):
        self.state_path.write_text(dumps(self.state), encoding="utf-8")
        self.evidence_path.write_text(dumps(self.evidence), encoding="utf-8")

    def load(self, period=PERIOD, **kwargs):
        return import_daily(self.s, self.p["id"], self.bundle, period, **kwargs)

    def files(self, record):
        return {p.name: p.read_text(encoding="utf-8") for p in Path(record["data"]["path"]).iterdir()}

    def test_summary_full_report_qualifications_and_private_metadata(self):
        before = {p: p.read_bytes() for p in (self.state_path, self.report_path, self.evidence_path)}
        report = self.load()
        files = self.files(report)
        self.assertEqual(len(files), 7)
        self.assertEqual(load_report(self.s, report["id"]), report)
        card = json.loads(files["card.json"])
        self.assertEqual(card["header"]["title"]["content"], "合成项目 · 每日进展")
        card_text = "\n".join(e["text"]["content"] for e in card["elements"] if e["tag"] == "div")
        self.assertIn("结论仍有限", card_text)
        self.assertIn(self.evidence["covered_until"], card_text)
        self.assertNotIn("第一条中文详情", card_text)
        document = preview(self.s, report["id"])["document"]
        text = "\n".join(b["text"]["elements"][0]["text_run"]["content"] for b in document["children"])
        for expected in ("2026-01-15", "第一条中文详情", "第二条限定", "折叠的完整细节", "尚未独立复现"):
            self.assertIn(expected, text)
        for forbidden in (daily_import.SCOPE, "证据摘要", "十个合成样本；仅为源陈述", "s1", "verified_at", "[REDACTED]"):
            self.assertNotIn(forbidden, text)
            self.assertNotIn(forbidden, dumps(card))
        self.assertIn("十个合成样本；仅为源陈述", files["evidence.html"])
        self.assertIn("[REDACTED]", files["evidence.json"])
        self.assertIn(daily_import.SCOPE, files["evidence.html"])
        self.assertIn("&lt;unsafe&gt;", files["index.html"])
        for raw in files.values():
            for forbidden in ("head-active", "body-active", "style-active", "frame-active", "hidden-active",
                              "display-active", "footer-navigation", "main-navigation", "javascript:", "onclick",
                              "private-item-metadata", "private-source-metadata", "private-top-metadata", "synthetic-private-secret"):
                self.assertNotIn(forbidden, raw)
        self.assertEqual(report["data"]["source_evidence"], self.evidence)
        self.assertEqual(report["data"]["source_hashes"]["report"], digest(before[self.report_path]))
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(self.s.records("claim"), [])
        self.assertEqual(self.s.records("evidence"), [])
        self.assertEqual(json.loads(files["evidence.json"])["evidence"]["items"][0]["grade"], "reported")

    def test_determinism_changed_same_period_and_immutability(self):
        first = self.load()
        old = self.files(first)
        self.assertEqual(self.load()["id"], first["id"])
        self.write_bundle(HTML.replace("第一条中文详情", "修订后的中文详情"))
        second = self.load()
        self.assertNotEqual(second["id"], first["id"])
        self.assertNotEqual(second["data"]["hash"], first["data"]["hash"])
        self.assertEqual(first["data"]["period"], second["data"]["period"])
        self.assertEqual(self.files(first), old)
        self.assertEqual(self.load()["id"], second["id"])
        Path(second["data"]["path"], "index.html").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "immutable report"):
            self.load()

    def test_title_override_changes_display_hash_preserves_source_and_period(self):
        source_before = self.report_path.read_bytes()
        first = self.load()
        title = "合成日报 · 2026-01-15 提前预览"
        updated = self.load(title=title)
        payload = preview(self.s, updated["id"])
        self.assertEqual(payload["document"]["title"], title)
        self.assertEqual(payload["card"]["header"]["title"]["content"], title)
        self.assertEqual(payload["document"]["children"][0]["text"]["elements"][0]["text_run"]["content"], title)
        self.assertNotEqual(first["data"]["hash"], updated["data"]["hash"])
        self.assertEqual(first["data"]["period"], updated["data"]["period"])
        self.assertEqual(first["data"]["source_hashes"], updated["data"]["source_hashes"])
        self.assertEqual(source_before, self.report_path.read_bytes())
        original_snapshot = json.loads(self.files(first)["evidence.json"])
        updated_snapshot = json.loads(self.files(updated)["evidence.json"])
        self.assertEqual(original_snapshot["body"], updated_snapshot["body"])
        self.assertEqual(updated_snapshot["source_title"], "合成项目 · 每日进展")
        self.assertEqual(updated["id"], self.load(title=title)["id"])
        for invalid in ("", " \n\t", "字" * 201):
            with self.subTest(title=invalid), self.assertRaisesRegex(ValueError, "display title"):
                self.load(title=invalid)
        self.assertEqual(preview(self.s, self.load(title="字" * 200)["id"])["document"]["title"], "字" * 200)

    def test_next_day_does_not_accumulate_old_claims(self):
        self.load()
        self.state["daily"]["last_successful_execution_date"] = "2026-01-16"
        self.evidence["execution_date"] = "2026-01-16"
        self.evidence["items"][0]["claim"] = "下一日的新结论"
        self.write_bundle(HTML.replace("第一条中文详情", "次日详情"))
        record = self.load("2026-01-16")
        outgoing = json.loads(self.files(record)["evidence.json"])
        self.assertEqual([i["claim"] for i in outgoing["evidence"]["items"]], ["下一日的新结论"])

    def test_rejects_stale_timezone_type_watermark_and_validation(self):
        cases = [("state-date", "state execution date", lambda: self.state["daily"].update(last_successful_execution_date="2026-01-14")),
                 ("evidence-date", "execution_date", lambda: self.evidence.update(execution_date="2026-01-14")),
                 ("state-zone", "state timezone", lambda: self.state.update(timezone="UTC")),
                 ("evidence-zone", "evidence timezone", lambda: self.evidence.update(timezone="UTC")),
                 ("type", "must be daily", lambda: self.evidence.update(report_type="weekly")),
                 ("watermark", "watermark mismatch", lambda: self.state["daily"].update(covered_until="2026-01-14T17:30:00+08:00")),
                 ("unvalidated", "validation.status", lambda: self.evidence["validation"].update(status="failed")),
                 ("hash", "sha256 mismatch", lambda: self.evidence["validation"].update(report_sha256="0" * 64)),
                 ("order", "must precede", lambda: self.evidence.update(covered_from=self.evidence["covered_until"])),
                 ("naive", "timezone offset", lambda: self.evidence.update(covered_from="2026-01-14T17:30:00")),
                 ("future", "future cutoff", lambda: (self.evidence.update(covered_until="2999-01-15T17:30:00+08:00"), self.state["daily"].update(covered_until="2999-01-15T17:30:00+08:00")))]
        initial_state, initial_evidence = dumps(self.state), dumps(self.evidence)
        for label, message, mutate in cases:
            with self.subTest(label=label):
                self.state, self.evidence = json.loads(initial_state), json.loads(initial_evidence)
                mutate()
                self.write_metadata()
                with self.assertRaisesRegex(ValueError, message):
                    self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_traversal_and_symlink_escape_including_state(self):
        outside = self.root / "outside.html"
        outside.write_text(HTML)
        for value in ("../outside.html", str(outside)):
            self.state["daily"]["last_report"] = value
            self.write_metadata()
            with self.assertRaisesRegex(ValueError, "escapes source-root"):
                self.load()
        link = self.bundle / "escape.html"
        link.symlink_to(outside)
        self.state["daily"]["last_report"] = "escape.html"
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "escapes source-root"):
            self.load()
        state_copy = self.root / "outside-state.json"
        state_copy.write_bytes(self.state_path.read_bytes())
        self.state_path.unlink()
        self.state_path.symlink_to(state_copy)
        with self.assertRaisesRegex(ValueError, "escapes source-root"):
            self.load()

    def test_input_mutation_race_refuses_commit(self):
        original = daily_import.render_import
        for path in (self.state_path, self.report_path, self.evidence_path):
            with self.subTest(path=path.name):
                self.write_bundle()
                def mutate(snapshot):
                    result = original(snapshot)
                    path.write_bytes(path.read_bytes() + b" ")
                    return result
                with patch.object(daily_import, "render_import", mutate), self.assertRaisesRegex(ValueError, "inputs changed"):
                    self.load()
                self.assertEqual(self.s.records("report"), [])
                self.assertFalse((self.s.root / "reports").exists())

    def test_empty_duplicate_or_oversize_summary_and_missing_body(self):
        cases = [(HTML.replace('id="sync-summary"', 'id="other"'), "sync-summary"),
                 (HTML.replace("<body>", "<body><p id='sync-summary'>duplicate</p>"), "exactly one"),
                 ("<body><h1>Title</h1><section id='sync-summary'></section></body>", "sync-summary"),
                 (HTML.replace("今日摘要", "字" * 6001), "sync-summary"),
                 ("<head><h1>hidden</h1><section id='sync-summary'>hidden</section></head>", "visible body")]
        for html, message in cases:
            with self.subTest(message=message):
                self.write_bundle(html)
                with self.assertRaisesRegex(ValueError, message):
                    self.load()

    def test_file_and_document_limits_reject_without_truncation(self):
        with patch.object(daily_import, "MAX_FILE", 50), self.assertRaisesRegex(ValueError, "size limit"):
            self.load()
        detail = "<p>完整详情</p>" * 1001
        self.write_bundle(HTML.replace("<h2>详细结果</h2>", detail))
        with self.assertRaisesRegex(ValueError, "document too large"):
            self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_extra_metadata_is_not_outgoing_but_changes_identity(self):
        first = self.load()
        self.evidence["extra"]["opaque"] = "new-private-value"
        self.write_metadata()
        second = self.load()
        self.assertNotEqual(first["data"]["hash"], second["data"]["hash"])
        self.assertNotIn("new-private-value", "\n".join(self.files(second).values()))

    def test_missing_invalid_inputs_and_bounded_evidence(self):
        self.evidence_path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.load()
        self.write_bundle()
        self.state_path.write_text('{"daily":{},"daily":{}}')
        with self.assertRaisesRegex(ValueError, "valid UTF-8 JSON"):
            self.load()
        self.write_bundle()
        self.evidence["items"][0]["source_ids"] = ["missing-source"]
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "source_ids"):
            self.load()
        self.evidence["items"][0]["source_ids"] = ["s1"]
        self.evidence["sources"][0]["summary"] = "字" * 2001
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "source summary"):
            self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_utc_cutoffs_are_compared_as_instants_and_offsets_required(self):
        self.evidence.update(covered_from="2026-01-14T09:30:00Z", covered_until="2026-01-15T09:30:00Z")
        self.state["daily"]["covered_until"] = self.evidence["covered_until"]
        self.write_metadata()
        self.assertEqual(self.load()["data"]["timezone"], "Asia/Shanghai")

    def test_valueless_html_attributes_and_import_cursor_isolation(self):
        self.s.db.execute("INSERT INTO cursors VALUES(?,?)", (self.p["id"], 7))
        self.write_bundle(HTML.replace("<main>", "<main><p style aria-hidden>普通文本</p>"))
        report = self.load()
        self.assertIn("普通文本", self.files(report)["index.html"])
        self.assertEqual(self.s.db.execute("SELECT seq FROM cursors WHERE project_id=?", (self.p["id"],)).fetchone()[0], 7)

    def test_publish_fake_http_noop_update_and_reconcile_use_standard_identity(self):
        environment = {"FEISHU_APP_ID": "fixture-app", "FEISHU_APP_SECRET": "fixture-secret", "FEISHU_RECEIVE_ID": "fixture-receiver"}
        transport = FakeHTTP()
        with patch.dict(os.environ, environment):
            first_report = self.load()
            first = publish(self.s, self.conf["publisher"], first_report["id"], confirm=True, transport=transport)
            self.assertEqual(first["status"], "completed")
            count = len(transport.calls)
            repeat = publish(self.s, self.conf["publisher"], self.load()["id"], confirm=True, transport=transport)
            self.assertTrue(repeat["no_op"])
            self.assertEqual(len(transport.calls), count)
            self.write_bundle(HTML.replace("合成样本十个", "更正：合成样本八个"))
            second_report = self.load(title="同一周期的修订预览")
            second = publish(self.s, self.conf["publisher"], second_report["id"], confirm=True, transport=transport)
            self.assertEqual(second["status"], "completed")
            for key in ("identity", "document_id", "message_id"):
                self.assertEqual(first[key], second[key])
            creates = [(m, u) for m, u, _ in transport.calls if m == "POST" and u.endswith("/docx/v1/documents")]
            self.assertEqual(len(creates), 1)
            updates = [b for m, u, b in transport.calls if m == "PATCH" and u.endswith("/im/v1/messages/fixture-message")]
            self.assertEqual(len(updates), 1)
            self.assertIn("https://www.feishu.cn/docx/fixture-doc", updates[0]["content"])
            self.assertNotIn("tag\":\"action", self.files(second_report)["card.json"])
            before = len(transport.calls)
            reconcile(self.s, self.conf["publisher"], second["publication_id"], transport=transport)
            self.assertTrue(all(m == "GET" or u.endswith("/tenant_access_token/internal")
                                for m, u, _ in transport.calls[before:]))

    def test_ambiguous_publish_never_retried_by_import(self):
        transport = FakeHTTP()
        transport.fail_card = True
        with patch.dict(os.environ, {"FEISHU_APP_ID": "fixture-app", "FEISHU_APP_SECRET": "fixture-secret", "FEISHU_RECEIVE_ID": "fixture-receiver"}):
            report = self.load()
            unknown = publish(self.s, self.conf["publisher"], report["id"], confirm=True, transport=transport)
            self.assertEqual(unknown["status"], "outcome_unknown")
            before = len(transport.calls)
            self.assertEqual(self.load()["id"], report["id"])
            with self.assertRaises(ValueError):
                publish(self.s, self.conf["publisher"], report["id"], confirm=True, retry=True, transport=transport)
            self.assertEqual(len(transport.calls), before)

    def test_cli_import_and_preview_offline(self):
        config = self.root / "cli-config.json"
        config.write_text(dumps({"state_dir": str(self.s.root)}))
        def cli(*args):
            result = subprocess.run([sys.executable, "-m", "work_assistant", "--config", str(config)] + list(args),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return json.loads(result.stdout)
        report = cli("import-daily", self.p["id"], "--source-root", str(self.bundle), "--date", PERIOD,
                     "--title", "合成 CLI 提前预览")
        payload = cli("publish", report["id"], "--dry-run")
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["document"]["title"], "合成 CLI 提前预览")
        self.assertEqual(self.s.records("publication"), [])
