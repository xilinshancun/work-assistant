"""First review regressions; all protocol examples are synthetic, not live results."""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from work_assistant.cli import main
from work_assistant.evidence import add_evidence, add_claim
from work_assistant.executors import Providers, parse
from work_assistant.process import ProcessResult, ProcessLaunchError, execute
from work_assistant.reports import generate_report
from work_assistant.routing import route_task
from work_assistant.runner import run_task
from work_assistant.util import dumps
from test_runtime import Base, FakeProviders, response, route_value, events


def dsh_stream(text="synthetic answer", reason=None):
    return [
        {"type": "session", "sessionId": "synthetic-session"},
        {"type": "status", "phase": "turn_start", "turn": 1},
        {"type": "text", "text": text},
        {"type": "status", "phase": "turn_end", "turn": 1,
         "reason": {"kind": "completed"} if reason is None else reason},
        {"type": "final", "text": text}]


class ReviewRegressions(Base):
    def test_report_redacts_tree_without_damaging_json_escapes(self):
        source = self.root / "source.json"
        source.write_text('{}')
        evidence = add_evidence(self.s, self.p["id"], source, 'path "quote" \\backslash')
        text = 'Set api_key="abc123"; token="x"; password: a"b; cd /Users/alice"; C:\\synthetic\\x'
        add_claim(self.s, self.p["id"], text, [evidence["id"]])
        report = generate_report(self.s, self.p["id"], "2026-01-15")
        root = Path(report["data"]["path"])
        snapshot = json.loads((root / "evidence.json").read_text())
        result = snapshot["claims"][0]["data"]["text"]
        self.assertIn("[REDACTED]", result)
        self.assertIn('C:\\synthetic\\x', result)
        self.assertNotIn("abc123", result)
        self.assertNotIn("/Users/alice", result)
        for name in ("card.json", "document.json"):
            json.loads((root / name).read_text())

    def test_huge_confidence_escalates_once(self):
        ident = self.task()
        fake = FakeProviders(self.conf, [response(dumps(route_value(ident, confidence=10 ** 400))),
                                         response(dumps(route_value(ident)))])
        result = route_task(self.s, fake, ident)
        self.assertTrue(result["data"]["accepted"])
        self.assertEqual([c[0][0] for c in fake.calls], ["dsh", "codex"])
        self.assertEqual(self.s.task(ident)["status"], "routed")

    def test_codex_intermediate_command_failure_can_recover(self):
        stream = events({"type": "thread.started", "thread_id": "synthetic"},
                        {"type": "item.completed", "item": {"type": "command_execution", "status": "failed", "exit_code": 1}},
                        {"type": "item.completed", "item": {"type": "file_change", "status": "completed"}},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": "recovered"}},
                        {"type": "turn.completed"})
        for action in ("read", "write_workspace"):
            result = parse("codex", ProcessResult(stream), action=action)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["diagnostics"])
        for terminal in ('{"type":"error"}', '{"type":"turn.failed"}'):
            result = parse("codex", ProcessResult(stream + "\n" + terminal), action="write_workspace")
            self.assertEqual(result["status"], "outcome_unknown")

    def test_partial_write_failure_and_claude_denial_require_resolution(self):
        value = {"type": "result", "subtype": "success", "is_error": False, "result": "edited file",
                 "permission_denials": [{"tool_name": "Write"}]}
        self.assertEqual(parse("claude_code", ProcessResult(dumps(value)), "write_workspace")["status"], "outcome_unknown")
        for provider in ("codex", "claude_code"):
            ident = self.routed("write_workspace", provider)
            fake = FakeProviders(self.conf, [response(status="failed", error="terminal failure after an edit")])
            result = run_task(self.s, fake, ident)
            self.assertEqual(result["data"]["status"], "outcome_unknown")
            with self.assertRaises(ValueError):
                route_task(self.s, fake, ident)
            with self.assertRaises(ValueError):
                run_task(self.s, fake, ident)
        ident = self.routed("write_workspace")
        result = run_task(self.s, FakeProviders(self.conf, [response(status="failed", error="binary missing", launched=False)]), ident)
        self.assertEqual(result["data"]["status"], "failed")

    def test_only_launch_failure_can_prove_worker_did_not_start(self):
        def missing(*args, **kwargs):
            raise ProcessLaunchError("synthetic missing binary")
        result = Providers(self.conf, missing).invoke("codex", "test", self.root, action="write_workspace")
        self.assertEqual(result["status"], "failed")
        self.assertIs(result["launched"], False)
        def after_launch(*args, **kwargs):
            raise OSError("synthetic process cleanup error after launch")
        result = Providers(self.conf, after_launch).invoke("codex", "test", self.root, action="write_workspace")
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIs(result["launched"], True)
        def completed(argv, *args, **kwargs):
            Path(argv[argv.index("-o") + 1]).write_text("synthetic final")
            return ProcessResult(events({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
                                        {"type": "turn.completed"}))
        with patch.object(Path, "read_text", side_effect=OSError("synthetic final log read failure")):
            result = Providers(self.conf, completed).invoke("codex", "test", self.root, action="write_workspace")
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIs(result["launched"], True)

    def test_dsh_source_derived_protocol_requires_success_status_and_final(self):
        good = dsh_stream()
        result = parse("dsh", ProcessResult(events(*good)))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["session_id"], "synthetic-session")
        self.assertEqual(result["text"], "synthetic answer")
        invalid = [good[:-1], [good[-1]], good[:3] + [good[-1]], dsh_stream(text=""),
                   dsh_stream(reason={"kind": "error", "error": {"code": "MISSING_CREDENTIAL", "message": "synthetic"}})]
        for kind in ("blocked", "max-tokens", "interrupted", "aborted", "unknown"):
            invalid.append(dsh_stream(reason={"kind": kind}))
        invalid += [[dict(e, truncated=True) if e["type"] == "text" else e for e in good],
                    dsh_stream(reason="completed"), good + [good[-1]]]
        for stream in invalid:
            with self.subTest(stream=stream):
                self.assertEqual(parse("dsh", ProcessResult(events(*stream)))["status"], "failed")
        self.assertEqual(parse("dsh", ProcessResult(events(*good), returncode=1))["status"], "failed")

    def test_dsh_filters_only_verified_model_listing_diagnostic(self):
        diagnostic = "[llm-deepseek-account] model listing from https://api.deepseek.com/models loaded 2 model(s), skipped 0 row(s)"
        good = dsh_stream()
        lines = [dumps(e) for e in good]
        lines.insert(2, diagnostic)
        result = parse("dsh", ProcessResult("\n".join(lines)))
        self.assertEqual(result["status"], "completed")
        self.assertIn(diagnostic, result["protocol_log"])
        self.assertIn(diagnostic, result["diagnostics"])
        for invalid in ('{"type":', '[{"type":"error"}', 'unrecognized debug output', diagnostic + ' {"type":',
                        diagnostic.replace("https://api.deepseek.com/models", "https://example.invalid/models")):
            self.assertEqual(parse("dsh", ProcessResult("\n".join(lines[:2] + [invalid] + lines[2:])))["status"], "failed")

    def test_codex_item_warning_is_diagnostic_and_goals_stay_unverified(self):
        stream = events({"type": "thread.started", "thread_id": "synthetic"},
                        {"type": "item.completed", "item": {"type": "error", "message": "synthetic unstable feature warning"}},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": "Tool unavailable; could not inspect file"}},
                        {"type": "turn.completed"})
        parsed = parse("codex", ProcessResult(stream))
        self.assertEqual(parsed["status"], "completed")
        self.assertEqual(parsed["protocol_status"], "completed")
        ident = self.routed()
        run = run_task(self.s, FakeProviders(self.conf, [parsed]), ident)
        self.assertEqual(run["data"]["protocol_status"], "completed")
        self.assertEqual(run["data"]["goal_status"], "unverified")
        self.assertEqual(self.s.task(ident)["goal_status"], "unverified")
        self.assertFalse(self.s.task(ident)["verified"])
        report = generate_report(self.s, self.p["id"], "2026-01-15")
        self.assertIn("protocol completed; goal unverified", Path(report["data"]["path"], "index.html").read_text())

    def test_corrupt_paseo_metadata_is_removed_and_diagnosed(self):
        with patch.dict(os.environ, {"PASEO_AGENT_CWD": "synthetic\udcff", "WA_SYNTHETIC_VALUE": "keep-me"}):
            result = execute([sys.executable, "-c", "import os,json;print(json.dumps([os.environ.get('PASEO_AGENT_CWD'),os.environ['WA_SYNTHETIC_VALUE']]))"],
                             "", self.root, timeout=5)
        self.assertEqual(json.loads(result.stdout), [None, "keep-me"])
        self.assertIn("removed non-UTF8 Paseo metadata: PASEO_AGENT_CWD", result.diagnostics)
        self.assertIn("PASEO_AGENT_CWD", parse("codex", result)["diagnostics"][0])

    def test_other_invalid_environment_fails_before_launch_without_secret_echo(self):
        for key in ("WA_USER_SETTING", "WA_API_TOKEN"):
            with patch.dict(os.environ, {key: "private-value\udcff"}), patch("subprocess.Popen") as popen:
                with self.assertRaises(ProcessLaunchError) as caught:
                    execute(["synthetic"], "", self.root)
                self.assertIn(key, str(caught.exception))
                self.assertNotIn("private-value", str(caught.exception))
                popen.assert_not_called()

    def test_desktop_overlay_paths_follow_config_base_even_in_temporary_cwd(self):
        from work_assistant.config import load
        conf_path = self.root / "config.json"
        conf_path.write_text(dumps({"providers": {"dsh": {"patches": ["synthetic-account.yml"]}}}))
        config = load(conf_path)
        calls = []
        def process(argv, prompt, **kwargs):
            calls.append(argv)
            return ProcessResult(events(*dsh_stream()))
        Providers(config, process).invoke("dsh", "test", self.root, classifier=True)
        argv = calls[0]
        self.assertEqual(argv[argv.index("--patch") + 1], str((self.root / "synthetic-account.yml").resolve()))

    def test_classifier_and_planner_have_empty_temporary_cwd(self):
        (self.root / "AGENTS.md").write_text("Synthetic project instructions; must not be classifier context")
        observations = []
        def process(argv, prompt, cwd, **kwargs):
            observations.append((argv, str(cwd), [p.name for p in Path(cwd).iterdir()]))
            return ProcessResult(returncode=1)
        providers = Providers(self.conf, process)
        for provider in ("dsh", "codex"):
            providers.invoke(provider, "synthetic classification", self.root, classifier=True)
        for argv, cwd, files in observations:
            self.assertNotEqual(Path(cwd), self.root)
            self.assertEqual(files, [])
            if "-C" in argv:
                self.assertEqual(argv[argv.index("-C") + 1], cwd)
        providers.invoke("claude_code", "synthetic execution", self.root, action="write_workspace")
        self.assertEqual(observations[-1][1], str(self.root))
        self.assertIn("Read,Glob,Grep,Edit,Write", observations[-1][0])
        self.assertNotIn("Bash", ",".join(observations[-1][0]))

    def test_review_failure_cli_is_nonzero(self):
        conf = self.root / "cli.json"
        conf.write_text(dumps({"state_dir": str(self.s.root)}))
        for passed, expected in ((False, 2), (True, 0), (None, 2)):
            result = {"data": {"status": "completed", "review_passed": passed}}
            with patch("work_assistant.cli.dispatch", return_value=result), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--config", str(conf), "review", "t_synthetic"]), expected)

    def test_nested_and_equal_writable_projects_rejected(self):
        nested = self.root / "nested"
        nested.mkdir()
        for path in (nested, self.root, self.root.parent):
            with self.assertRaises(ValueError):
                self.s.add_project("conflicting", path, ["read", "write_workspace"])
        # Read-only aliases cannot create a second mutator.
        self.s.add_project("read-alias", nested, ["read"])

    @unittest.skipUnless(os.name == "posix", "POSIX signal semantics")
    def test_sigkill_during_report_output_does_not_strand_project(self):
        script = '''import os,signal,sys
import work_assistant.reports as r
from work_assistant.store import Store
s=Store(sys.argv[1]);original=r.atomic_write
def die(path,text):
    original(path,text)
    os.kill(os.getpid(),signal.SIGKILL)
r.atomic_write=die
r.generate_report(s,sys.argv[2],'2026-01-15')
'''
        child = subprocess.run([sys.executable, "-c", script, str(self.s.root), self.p["id"]], timeout=10)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertEqual(self.s.records("report"), [])
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0], 0)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM locks").fetchone()[0], 0)
        report = generate_report(self.s, self.p["id"], "2026-01-15")
        self.assertTrue(Path(report["data"]["path"], "index.html").is_file())
        # Recover a legacy report lease too, after obtaining the actual OS lock.
        self.s.db.execute("INSERT INTO locks VALUES(?,?,?)", ("report:" + self.p["id"], "legacy", "synthetic"))
        self.assertEqual(generate_report(self.s, self.p["id"], "2026-01-15")["id"], report["id"])

    @unittest.skipUnless(os.name == "posix", "POSIX signal semantics")
    def test_sigterm_marks_write_unknown_and_terminates_owned_process(self):
        ident = self.routed("write_workspace")
        conf, marker = self.root / "signal-config.json", self.root / "worker-started"
        conf.write_text(dumps({"state_dir": str(self.s.root)}))
        script = '''import sys
import work_assistant.cli as cli
from work_assistant.runner import run_task
from work_assistant.process import execute
from work_assistant.executors import parse
class Provider:
    def invoke(self,provider,prompt,workspace,**kw):
        result=execute([sys.executable,'-c',"import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(10)",sys.argv[3]],'',workspace,timeout=30,pass_fds=kw['pass_fds'])
        return parse(provider,result,kw['action'])
def dispatch(args,conf,store):return run_task(store,Provider(),args.id)
cli.dispatch=dispatch
sys.exit(cli.main(['--config',sys.argv[1],'run',sys.argv[2]]))
'''
        parent = subprocess.Popen([sys.executable, "-c", script, str(conf), ident, str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            parent.send_signal(signal.SIGTERM)
            parent.communicate(timeout=5)
            self.assertEqual(parent.returncode, 143)
            self.assertEqual(self.s.task(ident)["status"], "outcome_unknown")
            self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM locks").fetchone()[0], 0)
            worker = int(marker.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(worker, 0)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.communicate()
