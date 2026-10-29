"""MCP inspection is synthetic and never starts a server or contacts a service."""
import json
from unittest.mock import patch
from work_assistant.config import doctor
from work_assistant.executors import Providers
from work_assistant.process import ProcessResult, ProcessLaunchError
from work_assistant.util import dumps
from test_runtime import Base, events


class CodexPreflightTests(Base):
    def completed(self):
        return ProcessResult(events({"type": "thread.started", "thread_id": "synthetic"},
                                    {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
                                    {"type": "turn.completed"}), latency_ms=7)

    def test_all_modes_disable_each_enabled_server_and_external_tools(self):
        names = ['simple', 'with.dot', 'quote"and\\slash', "unicode-合成", 'shell;$(synthetic)']
        rows = [{"name": name, "enabled": True, "transport": {"env": {"AUTH": "private-preflight-material"}}} for name in names]
        rows.append({"name": "already_disabled", "enabled": False})
        self.conf["providers"]["codex"]["argv"] = ["synthetic-codex", "--profile", "synthetic-profile", "-c", "features.apps=true"]
        self.conf["providers"]["codex"]["env"] = {"CODEX_HOME": str(self.root / "synthetic-home")}
        calls = []
        def process(argv, prompt, **kwargs):
            calls.append((argv, prompt, kwargs))
            if argv[-3:] == ["mcp", "list", "--json"]:
                return ProcessResult(dumps(rows), stderr="private-preflight-material", latency_ms=3)
            return self.completed()
        providers = Providers(self.conf, process)
        for action, classifier, session in (("read", False, None), ("write_workspace", False, None),
                                             ("read", True, None), ("read", False, "synthetic-session"),
                                             ("write_workspace", False, "synthetic-session")):
            result = providers.invoke("codex", "synthetic prompt", self.root, action=action, classifier=classifier, session_id=session)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["mcp_preflight"]["disabled_count"], len(names))
            self.assertEqual(result["latency_ms"], 10)
            before, after = calls[-2:]
            self.assertEqual(before[0][-3:], ["mcp", "list", "--json"])
            self.assertEqual(before[1], "")
            self.assertEqual(before[2]["env"], after[2]["env"])
            self.assertEqual(before[2]["cwd"], after[2]["cwd"])
            self.assertLessEqual(before[2]["timeout"], 10)
            self.assertEqual(before[2]["max_bytes"], 128 * 1024)
            for command in (before[0], after[0]):
                self.assertIn("features.apps=false", command)
                self.assertIn('web_search="disabled"', command)
                self.assertIn("sandbox_workspace_write.network_access=false", command)
                self.assertEqual(command[:3], ["synthetic-codex", "--profile", "synthetic-profile"])
            overrides = [a for a in after[0] if a.startswith("mcp_servers.")]
            decoded = []
            for arg in overrides:
                self.assertTrue(arg.endswith(".enabled=false"))
                decoded.append(json.loads(arg[len("mcp_servers."):-len(".enabled=false")]))
            self.assertEqual(set(decoded), set(names))
            self.assertNotIn("private-preflight-material", dumps(result))
            self.assertNotIn("already_disabled", " ".join(overrides))
        self.assertEqual(len(calls), 10)  # No cached preflight, including resumed sessions.

    def test_invalid_or_unavailable_inspection_never_starts_exec(self):
        malformed = ["not JSON private-preflight-material", "{}", "null", "[" * 1200 + "]" * 1200, '[{"name":"x","enabled":true}',
                     dumps([{"name": "x"}]), dumps([{"enabled": True}]), dumps([{"name": "", "enabled": True}]),
                     dumps([{"name": "x", "enabled": 1}]), dumps([{"name": "x", "enabled": None}]),
                     dumps([{"name": "x", "enabled": True}, {"name": "x", "enabled": False}]),
                     dumps([{"name": "line\nbreak", "enabled": True}]),
                     '[{"name":"\\udcff","enabled":true}]',
                     '[{"name":"x","enabled":false,"enabled":true}]']
        results = [ProcessResult(raw) for raw in malformed]
        results += [ProcessResult("[]", returncode=2), ProcessResult("[]", timed_out=True),
                    ProcessResult("[]", truncated=True), ProcessResult("[]", returncode=-9),
                    ProcessLaunchError("private-preflight-material"), OSError("private-preflight-material")]
        for fixture in results:
            calls = []
            def process(argv, *args, **kwargs):
                calls.append(argv)
                if isinstance(fixture, BaseException):
                    raise fixture
                return fixture
            result = Providers(self.conf, process).invoke("codex", "prompt", self.root, action="write_workspace")
            self.assertEqual(result["status"], "failed")
            self.assertIs(result["launched"], False)
            self.assertIs(result["preflight_failed"], True)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-3:], ["mcp", "list", "--json"])
            self.assertNotIn("private-preflight-material", dumps(result))

    def test_empty_mcp_list_still_applies_apps_web_network_policy(self):
        calls = []
        def process(argv, *args, **kwargs):
            calls.append(argv)
            return ProcessResult("[]") if argv[-3:] == ["mcp", "list", "--json"] else self.completed()
        result = Providers(self.conf, process).invoke("codex", "prompt", self.root)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["mcp_preflight"]["disabled_count"], 0)
        self.assertIn("features.apps=false", calls[-1])
        self.assertIn('web_search="disabled"', calls[-1])
        self.assertIn("sandbox_workspace_write.network_access=false", calls[-1])

    def test_doctor_reports_unsupported_mcp_list_as_unsafe_without_auth_dump(self):
        calls = []
        def process(argv, *args, **kwargs):
            calls.append(argv)
            if argv[-3:] == ["mcp", "list", "--json"]:
                return ProcessResult("private-preflight-material", stderr="private-preflight-material", returncode=2)
            return ProcessResult("--json --sandbox --output-schema --output-last-message")
        with patch("shutil.which", return_value="synthetic-codex"):
            result = doctor(self.conf, process)
        self.assertFalse(result["codex"]["capabilities"]["mcp_isolation_ready"])
        self.assertIn("cannot safely start Codex", result["codex"]["diagnostic"])
        self.assertNotIn("private-preflight-material", dumps(result))
        self.assertFalse(any("exec" in argv and "--help" not in argv for argv in calls))
