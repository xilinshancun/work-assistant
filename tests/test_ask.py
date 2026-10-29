import contextlib
import io
import json
from unittest.mock import patch
from work_assistant.assistant import ask_task
from work_assistant.cli import main
from work_assistant.util import dumps
from test_runtime import Base, response, route_value


class AskProvider:
    def __init__(self, config, route_status="completed", run_status="completed", verdict="pass"):
        self.config, self.calls = config, []
        self.route_status, self.run_status, self.verdict = route_status, run_status, verdict

    def invoke(self, provider, prompt, workspace, **kwargs):
        self.calls.append((provider, kwargs))
        if kwargs.get("classifier"):
            request = json.JSONDecoder().raw_decode(prompt.split("Input: ", 1)[1])[0]
            return response(dumps(route_value(request["task_id"], requested_action=request["intent"])), status=self.route_status, error="synthetic route failure")
        if prompt.startswith("Read-only independent review"):
            return response(dumps({"verdict": self.verdict, "reason": "synthetic independent review"}))
        return response("synthetic executor result", status=self.run_status, error="synthetic execution failure")


class AskTests(Base):
    def test_default_read_and_optional_review_report_reuse_existing_records(self):
        provider = AskProvider(self.conf)
        result = ask_task(self.s, provider, self.p["id"], "inspect synthetic fixture", review=True, report_date="2026-01-15")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["goal_status"], "independently_reviewed")
        self.assertEqual(self.s.task(result["task_id"])["intent"], "read")
        self.assertEqual([step["kind"] for step in result["steps"]], ["route", "run", "review", "report"])
        self.assertEqual(len(self.s.records("publication")), 0)
        self.assertEqual(len(self.s.tasks()), 1)
        self.assertEqual(provider.calls[1][0], "codex")
        self.assertEqual(provider.calls[2][0], "claude_code")

    def test_no_optional_stages_by_default_and_no_implicit_goal_verification(self):
        result = ask_task(self.s, AskProvider(self.conf), self.p["id"], "inspect fixture")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["goal_status"], "unverified")
        self.assertEqual([s["kind"] for s in result["steps"]], ["route", "run"])
        self.assertEqual(self.s.records("report"), [])

    def test_missing_write_grant_stops_before_models_and_returns_task_id(self):
        p = self.s.add_project("read-only", self.root, ["read"])
        provider = AskProvider(self.conf)
        result = ask_task(self.s, provider, p["id"], "write fixture", intent="write_workspace")
        self.assertEqual(result["status"], "needs_user")
        self.assertEqual(self.s.task(result["task_id"])["status"], "needs_user")
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.s.project(p["id"])["grants"], ["read"])

    def test_route_or_run_failure_and_unknown_stop_immediately(self):
        for route_status, run_status, expected, count in (
                ("failed", "completed", "needs_user", 2),
                ("completed", "failed", "failed", 2),
                ("completed", "outcome_unknown", "outcome_unknown", 2)):
            provider = AskProvider(self.conf, route_status, run_status)
            result = ask_task(self.s, provider, self.p["id"], "inspect fixture", review=True, report_date="2026-01-15")
            self.assertEqual(result["status"], expected)
            self.assertEqual(len(provider.calls), count)
            self.assertTrue(result["task_id"].startswith("t_"))
            self.assertEqual(self.s.records("report"), [])

    def test_review_fail_stops_before_report_with_saved_task_and_run(self):
        provider = AskProvider(self.conf, verdict="fail")
        result = ask_task(self.s, provider, self.p["id"], "inspect fixture", review=True, report_date="2026-01-15")
        self.assertEqual(result["status"], "needs_user")
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(result["stage"], "review")
        self.assertEqual(self.s.records("report"), [])
        self.assertFalse(self.s.task(result["task_id"])["verified"])

    def test_explicit_write_still_requires_matching_intent_and_grant(self):
        provider = AskProvider(self.conf)
        result = ask_task(self.s, provider, self.p["id"], "write fixture", intent="write_workspace")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(provider.calls[1][1]["action"], "write_workspace")
        self.assertEqual(self.s.task(result["task_id"])["intent"], "write_workspace")

    def test_cli_entry_returns_json_task_id_and_nonzero_on_unresolved_outcome(self):
        conf = self.root / "config.json"
        conf.write_text(dumps({"state_dir": str(self.s.root)}))
        provider = AskProvider(self.conf, run_status="outcome_unknown")
        output = io.StringIO()
        with patch("work_assistant.cli.Providers", return_value=provider), contextlib.redirect_stdout(output):
            rc = main(["--config", str(conf), "ask", self.p["id"], "inspect fixture", "--review"])
        result = json.loads(output.getvalue())
        self.assertEqual(rc, 2)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(self.s.task(result["task_id"])["status"], "outcome_unknown")
