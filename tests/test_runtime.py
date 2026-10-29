import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from pathlib import Path
from work_assistant.config import load
from work_assistant.process import execute, ProcessResult, ProcessLaunchError
from work_assistant.executors import parse, Providers
from work_assistant.store import Store, Busy
from work_assistant.routing import route_task
from work_assistant.runner import run_task
from work_assistant.util import dumps


def events(*items):
    return "\n".join(dumps(item) for item in items)


def route_value(ident, **changes):
    value = {"schema": "wa.route.v1", "task_id": ident, "complexity": "simple", "executor": "codex",
             "requested_action": "read", "reason": "test", "confidence": 0.95, "needs_escalation": False}
    value.update(changes)
    return value


def response(text="completed", **changes):
    value = {"status": "completed", "text": text, "session_id": "test-session", "tool_calls": False,
             "error": "", "latency_ms": 4, "stderr": ""}
    value.update(changes)
    return value


class FakeProviders:
    def __init__(self, conf, answers):
        self.config, self.answers, self.calls = conf, list(answers), []

    def invoke(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        value = self.answers.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conf = load(self.root / "config.json")
        self.s = Store(self.root / "state")
        self.p = self.s.add_project("test", self.root, ["read", "write_workspace"])

    def tearDown(self):
        self.s.close()
        self.temp.cleanup()

    def task(self, intent="read", provider=None):
        return self.s.add_task(self.p["id"], "synthetic task", intent, provider)["id"]

    def routed(self, intent="read", provider=None):
        ident = self.task(intent, provider)
        fake = FakeProviders(self.conf, [response(dumps(route_value(ident, requested_action=intent)))])
        route_task(self.s, fake, ident)
        return ident


class ParserTests(unittest.TestCase):
    def test_dsh_terminal_and_tools(self):
        # Synthetic projection derived from installed 0.2.1-rc.3 source; not live success.
        good = events({"type": "session", "sessionId": "dsh1"}, {"type": "text", "text": "hello"},
                      {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
                      {"type": "final", "text": "hello"})
        self.assertEqual(parse("dsh", ProcessResult(good))["status"], "completed")
        self.assertEqual(parse("dsh", ProcessResult(good))["session_id"], "dsh1")
        for bad in (good.rsplit("\n", 1)[0], good + '\n{"type":"error"}', good[:-2]):
            self.assertEqual(parse("dsh", ProcessResult(bad))["status"], "failed")
        self.assertTrue(parse("dsh", ProcessResult(good + '\n{"type":"tool_call"}'))["tool_calls"])
        self.assertIn('"type":"final"', parse("dsh", ProcessResult(good))["protocol_log"])
        self.assertIn('"type":"error"', parse("dsh", ProcessResult(good + '\n{"type":"error"}'))["protocol_log"])

    def test_codex_terminal(self):
        good = events({"type": "thread.started", "thread_id": "thread1"},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}, {"type": "turn.completed"})
        self.assertEqual(parse("codex", ProcessResult(good))["session_id"], "thread1")
        for bad in (good.rsplit("\n", 1)[0], good + '\n{"type":"turn.failed"}', good + '\n{"type":"error"}'):
            self.assertEqual(parse("codex", ProcessResult(bad))["status"], "failed")
        self.assertEqual(parse("codex", ProcessResult(good, returncode=1))["status"], "failed")

    def test_claude_failure_with_exit_zero_and_denial(self):
        value = {"type": "result", "subtype": "success", "is_error": False, "session_id": "cc1", "result": "done"}
        self.assertEqual(parse("claude_code", ProcessResult(dumps(value)))["status"], "completed")
        for changes in ({"is_error": True}, {"subtype": "error_max_turns"}, {"permission_denials": [{"tool": "Write"}]}):
            self.assertEqual(parse("claude_code", ProcessResult(dumps(dict(value, **changes))))["status"], "failed")

    def test_unknown(self):
        for result in (ProcessResult(timed_out=True), ProcessResult(truncated=True), ProcessResult(returncode=-9)):
            self.assertEqual(parse("codex", result)["status"], "outcome_unknown")


class RoutingTests(Base):
    def test_missing_dsh_binary_escalates_without_installing_all_providers(self):
        ident = self.task()
        self.conf["providers"]["dsh"]["argv"] = ["missing-dsh-synthetic"]
        calls = []
        def process(argv, prompt, **kwargs):
            calls.append(argv)
            if argv[-3:] == ["mcp", "list", "--json"]:
                return ProcessResult("[]")
            if argv[0] == "missing-dsh-synthetic":
                raise ProcessLaunchError("synthetic optional binary missing")
            return ProcessResult(events({"type": "thread.started", "thread_id": "synthetic"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": dumps(route_value(ident))}},
                {"type": "turn.completed"}))
        accepted = route_task(self.s, Providers(self.conf, process), ident)
        self.assertTrue(accepted["data"]["accepted"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1][-3:], ["mcp", "list", "--json"])
        self.assertIn("missing", self.s.records("route", task_id=ident)[1]["data"]["fallback_reason"])
    def test_simple_and_exact_cache(self):
        ident = self.task()
        fake = FakeProviders(self.conf, [response(dumps(route_value(ident)))])
        accepted = route_task(self.s, fake, ident)
        self.assertTrue(accepted["data"]["accepted"])
        self.assertEqual(route_task(self.s, fake, ident)["id"], accepted["id"])
        self.assertEqual(len(fake.calls), 1)

    def test_escalation_once_and_complex_planner_allowed(self):
        for first in (response(status="outcome_unknown", error="timeout"), response(tool_calls=True), response(text="bad json"),
                      response(status="failed", error="auth"), response(dumps({"bad": "route"}))):
            ident = self.task()
            fake = FakeProviders(self.conf, [first, response(dumps(route_value(ident, complexity="complex")))])
            accepted = route_task(self.s, fake, ident)
            self.assertTrue(accepted["data"]["accepted"])
            self.assertEqual([call[0][0] for call in fake.calls], ["dsh", "codex"])

    def test_planner_failure_needs_user(self):
        ident = self.task()
        fake = FakeProviders(self.conf, [response(text="bad"), response(text="still bad")])
        self.assertFalse(route_task(self.s, fake, ident)["data"]["accepted"])
        self.assertEqual(self.s.task(ident)["status"], "needs_user")
        self.assertEqual(len(fake.calls), 2)

    def test_explicit_write_cannot_shrink_and_provider_cannot_grant(self):
        ident = self.task("write_workspace", "claude_code")
        fake = FakeProviders(self.conf, [response(dumps(route_value(ident))), response(dumps(route_value(ident)))])
        route_task(self.s, fake, ident)
        self.assertEqual(self.s.task(ident)["status"], "needs_user")
        self.s.db.execute("UPDATE projects SET grants='[\"read\"]' WHERE id=?", (self.p["id"],))
        fake = FakeProviders(self.conf, [response(dumps(route_value(ident, requested_action="write_workspace")))] * 2)
        route_task(self.s, fake, ident)
        self.assertEqual(self.s.task(ident)["status"], "needs_user")


class RunTests(Base):
    def test_completed_reported_claim_and_independent_review(self):
        ident = self.routed("write_workspace")
        fake = FakeProviders(self.conf, [response("work done"), response(dumps({"verdict": "pass", "reason": "inspected actual files"}))])
        run_task(self.s, fake, ident)
        self.assertEqual(self.s.task(ident)["status"], "completed")
        self.assertFalse(self.s.task(ident)["verified"])
        self.assertEqual(self.s.records("claim")[0]["data"]["grade"], "reported")
        run_task(self.s, fake, ident, review=True)
        self.assertTrue(self.s.task(ident)["verified"])
        self.assertEqual(fake.calls[1][0][0], "claude_code")
        self.assertEqual(fake.calls[1][1]["action"], "read")

    def test_unknown_never_replays(self):
        ident = self.routed()
        fake = FakeProviders(self.conf, [response(status="outcome_unknown", error="timeout")])
        result = run_task(self.s, fake, ident)
        for operation in (lambda: run_task(self.s, fake, ident), lambda: route_task(self.s, fake, ident)):
            with self.assertRaises(ValueError):
                operation()
        self.s.reconcile_run(result["id"])
        self.assertEqual(len(fake.calls), 1)
        with self.assertRaises(ValueError):
            self.s.reconcile_run(result["id"], "completed", "")
        self.s.reconcile_run(result["id"], "failed", "operator inspected workspace; explicit retry required")
        self.assertEqual(self.s.task(ident)["status"], "failed")

    def test_interruption_is_unknown(self):
        ident = self.routed()
        with self.assertRaises(KeyboardInterrupt):
            run_task(self.s, FakeProviders(self.conf, [KeyboardInterrupt()]), ident)
        self.assertEqual(self.s.task(ident)["status"], "outcome_unknown")

    def test_followup_explicit_saved_session(self):
        ident = self.routed()
        fake = FakeProviders(self.conf, [response(), response()])
        run_task(self.s, fake, ident)
        with self.assertRaises(ValueError):
            run_task(self.s, fake, ident)
        run_task(self.s, fake, ident, followup="Please check one more synthetic file")
        self.assertEqual(fake.calls[1][1]["session_id"], "test-session")

    def test_concurrent_task_and_workspace_and_fencing(self):
        other = Store(self.s.root)
        self.addCleanup(other.close)
        with self.s.claim(["task:test", "workspace:test"]) as (owner, _):
            for resource in ("task:test", "workspace:test"):
                with self.assertRaises(Busy):
                    with other.claim([resource]):
                        self.fail("duplicate claim")
            record = self.s.record("run", {"status": "running"})
            self.s.update_record(record["id"], {"status": "completed"}, owner)
        with self.assertRaises(Busy):
            self.s.update_record(record["id"], {}, owner)

    def test_run_rechecks_state_after_claim(self):
        ident = self.routed()
        real_claim = self.s.claim
        @contextmanager
        def delayed_claim(resources):
            self.s.transition(ident, "running")
            self.s.transition(ident, "collecting")
            self.s.transition(ident, "completed")
            with real_claim(resources) as value:
                yield value
        fake = FakeProviders(self.conf, [])
        with patch.object(self.s, "claim", delayed_claim), self.assertRaises(ValueError):
            run_task(self.s, fake, ident)
        self.assertEqual(fake.calls, [])

    def test_interrupted_review_reconciles_without_changing_completed_task(self):
        ident = self.routed()
        run_task(self.s, FakeProviders(self.conf, [response()]), ident)
        with self.assertRaises(KeyboardInterrupt):
            run_task(self.s, FakeProviders(self.conf, [KeyboardInterrupt()]), ident, review=True)
        review_run = self.s.records("run", task_id=ident)[0]
        self.s.reconcile_run(review_run["id"], "failed", "review interrupted; no verification")
        self.assertEqual(self.s.task(ident)["status"], "completed")
        self.assertFalse(self.s.task(ident)["verified"])


class ProcessTests(Base):
    def test_argv_and_stdin_not_shell(self):
        marker = self.root / "must-not-exist"
        prompt = "$(touch %s); `touch %s`" % (marker, marker)
        result = execute([sys.executable, "-c", "import sys;print(sys.stdin.read())"], prompt, self.root, timeout=5)
        self.assertEqual(result.stdout.strip(), prompt)
        self.assertFalse(marker.exists())

    def test_timeout_and_bounded_output(self):
        result = execute([sys.executable, "-c", "import time;time.sleep(30)"], "", self.root, timeout=0.1)
        self.assertTrue(result.timed_out)
        result = execute([sys.executable, "-c", "print('x'*1000000)"], "", self.root, timeout=5, max_bytes=100)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout), 100)

    def test_provider_argv_modes_and_env(self):
        calls = []
        def process(argv, prompt, **kwargs):
            calls.append((argv, prompt, kwargs))
            if argv[-3:] == ["mcp", "list", "--json"]:
                return ProcessResult("[]")
            return ProcessResult(returncode=1)
        providers = Providers(self.conf, process)
        for provider in ("dsh", "codex", "claude_code"):
            providers.invoke(provider, "test", self.root, classifier=provider == "dsh")
        self.assertEqual(calls[0][2]["env"]["DSH_PERMISSION_MODE"], "read-only")
        self.assertEqual(calls[1][0][-3:], ["mcp", "list", "--json"])
        self.assertIn("read-only", calls[2][0])
        self.assertIn("Read,Glob,Grep", calls[3][0])
        self.assertNotIn("--bare", calls[3][0])
        self.assertFalse(any("bypass" in a for call in calls for a in call[0]))
