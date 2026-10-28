import json
import tempfile
import unittest
from pathlib import Path
from work_assistant.evidence import add_evidence, add_claim, change_claim, verify_evidence, ingest
from work_assistant.reports import generate_report, load_report
from work_assistant.demo import demo
from work_assistant.util import dumps, redact
from test_runtime import Base


class EvidenceTests(Base):
    def source(self, value=10):
        path = self.root / ("sample%s.json" % value)
        path.write_text(dumps({"values": {"count": {"value": value, "unit": "items"}}}), encoding="utf-8")
        return add_evidence(self.s, self.p["id"], path, "synthetic test")

    def claim(self, value=10, **kwargs):
        source = self.source(value)
        return add_claim(self.s, self.p["id"], "synthetic count", [source["id"]],
                         numeric={"key": "count", "value": value, "unit": "items"}, **kwargs)

    def test_source_hash_and_numeric_units(self):
        source = self.source()
        for numeric in ({"key": "count", "value": 11, "unit": "items"}, {"key": "count", "value": 10, "unit": "seconds"},
                        {"key": "count", "value": True, "unit": "items"}):
            with self.assertRaises(ValueError):
                add_claim(self.s, self.p["id"], "bad value", [source["id"]], numeric=numeric)
        Path(source["data"]["artifact"]).write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            verify_evidence(source)
        with self.assertRaises(ValueError):
            add_claim(self.s, self.p["id"], "tampered", [source["id"]])

    def test_grade_derivation_and_sources(self):
        source = self.source()
        for kwargs in ({"grade": "verified"}, {"grade": "derived"}, {"grade": "derived", "derivation": "x+1", "inputs": ["missing"]}):
            with self.assertRaises(ValueError):
                add_claim(self.s, self.p["id"], "invalid", [source["id"]], **kwargs)
        with self.assertRaises(ValueError):
            add_claim(self.s, self.p["id"], "missing source", [])
        p2 = self.s.add_project("another", self.root, ["read"])
        with self.assertRaises(ValueError):
            add_claim(self.s, p2["id"], "cross project", [source["id"]])
        claim = add_claim(self.s, self.p["id"], "manual", [source["id"]], grade="measured", attribution="test operator")
        self.assertIn("scientific reproduction not implied", claim["data"]["verification_scope"])

    def test_ingestion_requires_exact_config_and_never_mutates(self):
        source = self.source()
        path = source["data"]["source"]
        before = Path(path).read_bytes()
        with self.assertRaises(ValueError):
            ingest(self.s, self.conf, self.p["id"], path, "test")
        self.conf["ingest_paths"] = [path]
        ingest(self.s, self.conf, self.p["id"], path, "test")
        self.assertEqual(before, Path(path).read_bytes())

    def test_snapshot_immutable_supersession_and_stale_derivation(self):
        first = self.claim()
        evidence = first["data"]["evidence_ids"]
        derived = add_claim(self.s, self.p["id"], "computed example", evidence, grade="derived", derivation="count*2", inputs=[first["id"]])
        report1 = generate_report(self.s, self.p["id"], "2026-01-15")
        old = Path(report1["data"]["path"], "evidence.json").read_bytes()
        second = self.claim(8)
        change_claim(self.s, first["id"], "supersede", "duplicate rows corrected after period", second["id"])
        report2 = generate_report(self.s, self.p["id"], "2026-01-15")
        latest = json.loads(Path(report2["data"]["path"], "evidence.json").read_text())
        ids = {c["id"] for c in latest["claims"]}
        self.assertEqual(ids, {second["id"]})
        self.assertEqual(len(latest["corrections"]), 1)
        self.assertEqual(old, Path(report1["data"]["path"], "evidence.json").read_bytes())
        change_claim(self.s, second["id"], "retract", "sample withdrawn")
        self.assertEqual(self.s.get(second["id"])["data"]["status"], "retracted")

    def test_render_determinism_and_cursor_rollback(self):
        self.claim()
        report = generate_report(self.s, self.p["id"], "2026-01-15")
        self.assertEqual(generate_report(self.s, self.p["id"], "2026-01-15")["id"], report["id"])
        before = self.s.db.execute("SELECT seq FROM cursors").fetchone()[0]
        self.claim(9)
        def broken(snapshot):
            raise RuntimeError("render failure")
        with self.assertRaises(RuntimeError):
            generate_report(self.s, self.p["id"], "2026-01-16", renderer=broken)
        self.assertEqual(self.s.db.execute("SELECT seq FROM cursors").fetchone()[0], before)
        self.assertEqual(len(self.s.records("report")), 1)

    def test_html_markdown_secrets_and_immutable_files(self):
        source = self.source()
        malicious = '<script>alert(1)</script> [bad](javascript:alert(1)) api_key=synthetic-private-value /Users/example/private'
        add_claim(self.s, self.p["id"], malicious, [source["id"]])
        report = generate_report(self.s, self.p["id"], "2026-01-15")
        root = Path(report["data"]["path"])
        html = (root / "index.html").read_text()
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="javascript:', html)
        for path in root.iterdir():
            text = path.read_text()
            self.assertNotIn("synthetic-private-value", text)
            self.assertNotIn("/Users/example", text)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn(r"\[bad\]", (root / "daily-sync.md").read_text())
        (root / "card.json").write_text("{}")
        with self.assertRaises(ValueError):
            load_report(self.s, report["id"])

    def test_obvious_credential_blocks_export_before_cursor(self):
        source = self.source()
        add_claim(self.s, self.p["id"], "accidental key " + "AKIA" + "A" * 16, [source["id"]])
        with self.assertRaises(ValueError):
            generate_report(self.s, self.p["id"], "2026-01-15")
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0], 0)


class DemoTests(unittest.TestCase):
    def test_full_demo_deterministic_across_directories(self):
        with tempfile.TemporaryDirectory() as root:
            first = demo(Path(root) / "a")
            second = demo(Path(root) / "b")
            self.assertEqual(first["hash"], second["hash"])
            self.assertEqual(first["routes"], 3)
            self.assertEqual(first, demo(Path(root) / "a"))
            expected = {"index.html", "daily-sync.md", "evidence.html", "evidence.json", "card.json", "document.json", "card-preview.html"}
            self.assertEqual({p.name for p in Path(first["report_path"]).iterdir()}, expected)
