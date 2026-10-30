"""Public offline evaluation entry: real CLI, isolated fixtures, no live services."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate.py"
spec = importlib.util.spec_from_file_location("offline_evaluator", SCRIPT)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_real_cli_from_unrelated_cwd_with_poisoned_home_and_environment(self):
        home = self.root / "real-home-must-not-be-used";home.mkdir()
        private = home / "config.local.json";private.write_text("ambient-private-marker")
        cwd = self.root / "unrelated";cwd.mkdir()
        destination = self.root / "new-receipt"
        env = dict(os.environ, HOME=str(home), FEISHU_APP_SECRET="private-secret-marker",
                   OPENAI_API_KEY="private-key-marker", CODEX_HOME=str(home),
                   PYTHONPATH=str(home), PASEO_AGENT_CWD="private-paseo-marker")
        result = subprocess.run([sys.executable, str(SCRIPT), "--output", str(destination)],
                                cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["schema"], "wa.evaluation.v1");self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["scope"]["data"], "synthetic");self.assertFalse(receipt["scope"]["live_tested"])
        self.assertEqual(receipt["summary"], {"passed": 4, "failed": 0, "skipped": 0})
        self.assertEqual(json.loads((destination / "receipt.json").read_text()), receipt)
        checks = {c["id"]: c for c in receipt["checks"]}
        self.assertEqual(checks["demo_a"]["details"]["report_hash"], checks["demo_b"]["details"]["report_hash"])
        self.assertEqual(checks["demo_a"]["details"]["artifact_sha256"], checks["demo_b"]["details"]["artifact_sha256"])
        self.assertEqual(checks["demo_determinism"]["details"]["equal_artifacts"], 7)
        self.assertEqual(checks["provider_free_lifecycle"]["details"]["route_run_publication_records"], 0)
        self.assertEqual(checks["provider_free_lifecycle"]["details"]["publish_status"], "dry_run")
        for marker in ("ambient-private-marker", "private-secret-marker", "private-key-marker", "private-paseo-marker"):
            self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual(private.read_text(), "ambient-private-marker")
        self.assertEqual(list(home.iterdir()), [private]);self.assertEqual(list(cwd.iterdir()), [])

    def test_output_must_be_new_and_existing_content_is_not_changed(self):
        destination = self.root / "existing";destination.mkdir()
        sentinel = destination / "receipt.json";sentinel.write_text("leave this untouched")
        result = subprocess.run([sys.executable, str(SCRIPT), "--output", str(destination)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        receipt = json.loads(result.stdout);self.assertEqual(receipt["checks"][0]["code"], "output_unavailable")
        self.assertEqual(sentinel.read_text(), "leave this untouched");self.assertEqual(list(destination.iterdir()), [sentinel])

    def test_default_success_cleans_temporary_artifacts_without_publishing_dead_paths(self):
        created = []
        real_mkdtemp = tempfile.mkdtemp
        def temporary(**kwargs):
            path = real_mkdtemp(dir=self.root, **kwargs);created.append(Path(path));return path
        with patch.object(evaluation.tempfile, "mkdtemp", side_effect=temporary):
            receipt = evaluation.evaluate()
        self.assertEqual(receipt["status"], "passed");self.assertIsNone(receipt["output_dir"])
        self.assertFalse(receipt["artifacts_retained"]);self.assertTrue(created)
        self.assertTrue(all(not p.exists() for p in created))
        self.assertTrue(all(c.get('details',{}).get('report_path') is None for c in receipt['checks']))

    def test_failure_preserves_receipt_diagnosis_and_never_echoes_exception_payload(self):
        destination = self.root / "failed"
        with patch.object(evaluation, "run_cli", side_effect=RuntimeError("private-error-marker")):
            receipt = evaluation.evaluate(destination)
        self.assertEqual(receipt["status"], "failed");self.assertTrue(receipt["artifacts_retained"])
        self.assertEqual(receipt["output_dir"], str(destination))
        self.assertEqual(json.loads((destination / "receipt.json").read_text()), receipt)
        self.assertNotIn("private-error-marker", json.dumps(receipt))
        self.assertEqual(receipt["summary"], {"passed": 0, "failed": 3, "skipped": 1})
        self.assertTrue(all(c.get("diagnostic") for c in receipt["checks"]))

    def test_default_failure_explicitly_retains_its_temporary_directory(self):
        destination = self.root / "temporary-failure";destination.mkdir()
        with patch.object(evaluation.tempfile, "mkdtemp", return_value=str(destination)), \
                patch.object(evaluation, "run_cli", side_effect=evaluation.EvaluationFailure("fixture", "Synthetic CLI failure.")):
            receipt = evaluation.evaluate()
        self.assertEqual(receipt["status"], "failed");self.assertTrue(receipt["artifacts_retained"])
        self.assertEqual(receipt["output_dir"], str(destination));self.assertTrue((destination / "receipt.json").is_file())

    def test_guard_prevents_network_and_external_child_attempts_before_they_take_effect(self):
        for index, source in enumerate(("import socket; socket.getaddrinfo('example.invalid', 443)",
                "import subprocess,sys; subprocess.run([sys.executable,'-c','raise SystemExit(99)'])")):
            with self.subTest(index=index):
                root = self.root / str(index);root.mkdir()
                package = root / "runtime" / "work_assistant";package.mkdir(parents=True)
                (package / "__init__.py").write_text("")
                (package / "__main__.py").write_text(source)
                with patch.object(evaluation, "SOURCE", package.parent):
                    env = evaluation.environment(root)
                with self.assertRaises(evaluation.EvaluationFailure) as caught:
                    evaluation.run_cli(root, env, "guard-test", ["demo"])
                self.assertEqual(caught.exception.code, "forbidden_operation")
                event = json.loads((root / "guards" / "guard-test.blocked").read_text())["event"]
                self.assertTrue(event.startswith("socket.") or event == "subprocess.Popen")

    def test_malformed_or_missing_artifacts_fail_and_external_report_paths_are_not_read(self):
        report = self.root / "report";report.mkdir()
        with self.assertRaises(evaluation.EvaluationFailure) as caught:
            evaluation.report_artifacts(report, self.root)
        self.assertEqual(caught.exception.code, "missing_artifact")
        for name in evaluation.ARTIFACTS:
            (report / name).write_text("{}" if name.endswith('.json') else
                "<html><title>Synthetic</title><p>Readable</p></html>" if name.endswith('.html') else "Synthetic Markdown")
        (report / "card.json").write_text('{"duplicate":1,"duplicate":2}')
        with self.assertRaises(evaluation.EvaluationFailure) as caught:
            evaluation.report_artifacts(report, self.root)
        self.assertIn("card.json", str(caught.exception))
        allowed = self.root / "allowed";allowed.mkdir()
        with self.assertRaises(evaluation.EvaluationFailure) as caught:
            evaluation.report_artifacts(report, allowed)
        self.assertEqual(caught.exception.code, "report_location")

    def test_timeout_diagnostic_and_replacement_environment_do_not_expose_ambient_secrets(self):
        root = self.root / "timeout";root.mkdir()
        with patch.dict(os.environ, {'FEISHU_APP_SECRET': 'ambient-secret', 'HOME': 'ambient-home'}):
            env = evaluation.environment(root)
        self.assertNotIn('FEISHU_APP_SECRET', env);self.assertEqual(env['HOME'], str(root / 'home'))
        with patch.object(evaluation.subprocess, 'run', side_effect=subprocess.TimeoutExpired([], 60, output=b'private-output')):
            with self.assertRaises(evaluation.EvaluationFailure) as caught:
                evaluation.run_cli(root, env, 'timeout', ['doctor'])
        self.assertEqual(caught.exception.code, 'cli_timeout');self.assertNotIn('private-output', str(caught.exception))
