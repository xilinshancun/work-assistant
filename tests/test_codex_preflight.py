"""MCP inspection is synthetic and never starts a server or contacts a service."""
import json
import copy
import shutil
import sys
import unittest
from unittest.mock import patch
from work_assistant.config import doctor
from work_assistant.executors import Providers, codex_mcp_preflight
from work_assistant.process import ProcessResult, ProcessLaunchError, execute
from work_assistant.util import dumps
from test_runtime import Base, events


class CodexPreflightTests(Base):
    def apply_cli_overrides(self, argv, original):
        """Mirror installed CLI's literal dot splitting and separate layer validation."""
        rows = {r["name"]: copy.deepcopy(r) for r in original}
        for index, value in enumerate(argv):
            if index == 0 or argv[index - 1] != "-c" or not value.startswith("mcp_servers."):
                continue
            key, table = value.split("=", 1)
            parts = key.split(".")
            # enabled=false alone is an invalid transport in the override layer.
            kinds = {'{command="wa-mcp-disabled",enabled=false}': "stdio",
                     '{url="http://127.0.0.1:9/wa-disabled",enabled=false}': "streamable_http"}
            if len(parts) != 2 or table not in kinds:
                return ProcessResult(returncode=1, stderr="synthetic invalid transport")
            name = parts[1]  # Quotes remain literal characters, as in Codex 0.164.
            if name in rows and rows[name].get("transport", {}).get("type") != kinds[table]:
                return ProcessResult(returncode=1, stderr="synthetic incompatible transport")
            rows[name] = {"name": name, "enabled": False,
                          "transport": {"type": kinds[table]}}
        return ProcessResult(dumps(list(rows.values())), stderr="private-preflight-material", latency_ms=3)

    def completed(self):
        return ProcessResult(events({"type": "thread.started", "thread_id": "synthetic"},
                                    {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
                                    {"type": "turn.completed"}), latency_ms=7)

    def test_all_modes_disable_each_enabled_server_and_external_tools(self):
        names = ['simple', 'node_repl', 'desktop-42', 'http_client']
        rows = [{"name": name, "enabled": True, "transport": {
            "type": "streamable_http" if name == "http_client" else "stdio",
            "env": {"AUTH": "private-preflight-material"}, "url": "https://private.invalid/private-preflight-material"}} for name in names]
        rows.append({"name": "already_disabled", "enabled": False, "transport": {"type": "stdio"}})
        self.conf["providers"]["codex"]["argv"] = ["synthetic-codex", "--profile", "synthetic-profile", "-c", "features.apps=true"]
        self.conf["providers"]["codex"]["env"] = {"CODEX_HOME": str(self.root / "synthetic-home")}
        calls = []
        def process(argv, prompt, **kwargs):
            calls.append((argv, prompt, kwargs))
            if argv[-3:] == ["mcp", "list", "--json"]:
                return self.apply_cli_overrides(argv, rows)
            return self.completed()
        providers = Providers(self.conf, process)
        for action, classifier, session in (("read", False, None), ("write_workspace", False, None),
                                             ("read", True, None), ("read", False, "synthetic-session"),
                                             ("write_workspace", False, "synthetic-session")):
            result = providers.invoke("codex", "synthetic prompt", self.root, action=action, classifier=classifier, session_id=session)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["mcp_preflight"]["disabled_count"], len(names))
            self.assertEqual(result["latency_ms"], 13)
            self.assertIs(result["mcp_preflight"]["verified"], True)
            before, verification, after = calls[-3:]
            self.assertEqual(before[0][-3:], ["mcp", "list", "--json"])
            self.assertEqual(verification[0][-3:], ["mcp", "list", "--json"])
            self.assertEqual(before[1], "")
            self.assertEqual(before[2]["env"], after[2]["env"])
            self.assertEqual(before[2]["cwd"], after[2]["cwd"])
            self.assertEqual(before[2]["env"], verification[2]["env"])
            self.assertEqual(before[2]["cwd"], verification[2]["cwd"])
            self.assertLessEqual(before[2]["timeout"], 10)
            self.assertEqual(before[2]["max_bytes"], 128 * 1024)
            for command in (before[0], verification[0], after[0]):
                self.assertIn("features.apps=false", command)
                self.assertIn('web_search="disabled"', command)
                self.assertIn("sandbox_workspace_write.network_access=false", command)
                self.assertEqual(command[:3], ["synthetic-codex", "--profile", "synthetic-profile"])
            overrides = [a for a in after[0] if a.startswith("mcp_servers.")]
            decoded = []
            for arg in overrides:
                key, table = arg.split("=", 1)
                self.assertRegex(key, r'^mcp_servers\.[A-Za-z0-9_-]+$')
                name = key.split(".")[1]
                self.assertEqual(table, '{url="http://127.0.0.1:9/wa-disabled",enabled=false}' if name == "http_client"
                                 else '{command="wa-mcp-disabled",enabled=false}')
                decoded.append(name)
            self.assertEqual(set(decoded), set(names))
            self.assertNotIn("private-preflight-material", dumps(result))
            self.assertNotIn("already_disabled", " ".join(overrides))
        self.assertEqual(len(calls), 15)  # Two fresh lists per invocation, including resume.

    def test_invalid_or_unavailable_inspection_never_starts_exec(self):
        malformed = ["not JSON private-preflight-material", "{}", "null", "[" * 1200 + "]" * 1200, '[{"name":"x","enabled":true}',
                     dumps([{"name": "x"}]), dumps([{"enabled": True}]), dumps([{"name": "", "enabled": True}]),
                     dumps([{"name": "x", "enabled": 1}]), dumps([{"name": "x", "enabled": None}]),
                     dumps([{"name": "x", "enabled": True}, {"name": "x", "enabled": False}]),
                     dumps([{"name": "line\nbreak", "enabled": True}]),
                     dumps([{"name": "with.dot", "enabled": True}]),
                     dumps([{"name": '"quoted"', "enabled": True}]),
                     dumps([{"name": "space name", "enabled": False}]),
                     dumps([{"name": "合成", "enabled": True}]),
                     dumps([{"name": "shell;$(synthetic)", "enabled": True}]),
                     dumps([{"name": "x", "enabled": True, "transport": {"type": "unknown"}}]),
                     dumps([{"name": "x", "enabled": False, "transport": {"type": "sse"}}]),
                     dumps([{"name": "x", "enabled": True, "transport": {"type": True}}]),
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
        self.assertEqual(len(calls), 3)

    def test_verification_mismatch_or_failure_never_starts_exec(self):
        original = [{"name": "first", "enabled": True, "transport": {"type": "stdio"}},
                    {"name": "second", "enabled": False, "transport": {"type": "streamable_http"}}]
        variants = [ProcessResult(dumps(original)),
                    ProcessResult(dumps([{"name": "first", "enabled": False, "transport": {"type": "stdio"}}])),
                    ProcessResult(dumps([{"name": n, "enabled": False, "transport": {"type": "stdio"}} for n in ("first", "second", "extra")])),
                    ProcessResult(dumps([{"name": n, "enabled": False, "transport": {"type": "stdio"}} for n in ("first", "second")])),
                    ProcessResult("not JSON private-preflight-material"), ProcessResult("[]", returncode=1),
                    ProcessResult("[]", timed_out=True), ProcessResult("[]", truncated=True),
                    ProcessLaunchError("private-preflight-material")]
        for fixture in variants:
            calls = []
            def process(argv, *args, **kwargs):
                calls.append(argv)
                if len(calls) == 1:
                    return ProcessResult(dumps(original))
                if isinstance(fixture, BaseException):
                    raise fixture
                return fixture
            result = Providers(self.conf, process).invoke("codex", "prompt", self.root, action="write_workspace")
            self.assertEqual(result["status"], "failed")
            self.assertIs(result["launched"], False)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(a[-3:] == ["mcp", "list", "--json"] for a in calls))
            self.assertNotIn("private-preflight-material", dumps(result))

    def test_mock_rejects_partial_transport_and_preserves_literal_quote_keys(self):
        rows = [{"name": "node_repl", "enabled": True, "transport": {"type": "stdio"}}]
        partial = self.apply_cli_overrides(["codex", "-c", "mcp_servers.node_repl.enabled=false"], rows)
        self.assertNotEqual(partial.returncode, 0)
        quoted = self.apply_cli_overrides(["codex", "-c", 'mcp_servers."node_repl"={command="wa-mcp-disabled",enabled=false}'], rows)
        listing = {r["name"]: r["enabled"] for r in json.loads(quoted.stdout)}
        self.assertTrue(listing["node_repl"])
        self.assertIn('"node_repl"', listing)

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

    def test_doctor_verifies_overrides_and_rejects_ineffective_disable(self):
        rows = [{"name": "node_repl", "enabled": True, "transport": {"type": "stdio"}},
                {"name": "http_client", "enabled": True, "transport": {"type": "streamable_http"}},
                {"name": "desktop", "enabled": False, "transport": {"type": "stdio"}}]
        for effective in (True, False):
            calls = []
            def process(argv, *args, **kwargs):
                if argv[-3:] == ["mcp", "list", "--json"]:
                    calls.append(argv)
                    return self.apply_cli_overrides(argv, rows) if effective else ProcessResult(dumps(rows))
                return ProcessResult("--json --sandbox --output-schema --output-last-message")
            with patch("shutil.which", return_value="synthetic-codex"):
                result = doctor(self.conf, process)
            self.assertIs(result["codex"]["capabilities"]["mcp_isolation_ready"], effective)
            self.assertEqual(len(calls), 2)
            self.assertTrue(any('mcp_servers.node_repl={command="wa-mcp-disabled",enabled=false}' == a for a in calls[-1]))

    @unittest.skipUnless(shutil.which("codex"), "optional installed Codex CLI protocol regression")
    def test_installed_cli_full_table_overrides_with_isolated_synthetic_config(self):
        home = self.root / "isolated-home"
        codex_home = home / "codex"
        codex_home.mkdir(parents=True)
        marker = home / "mcp-must-never-start"
        config = codex_home / "config.toml"
        config.write_text('\n'.join([
            '[mcp_servers.node_repl]',
            'command = ' + json.dumps(sys.executable),
            'args = ' + json.dumps(['-c', 'import sys;from pathlib import Path;Path(sys.argv[1]).write_text("unexpected server start")', str(marker)]),
            'enabled = true',
            'env = {SYNTHETIC_AUTH="private-preflight-material"}',
            '[mcp_servers.cua_repl]',
            'command = "wa-synthetic-mcp-never-run"',
            'enabled = true',
            '[mcp_servers.already_disabled]',
            'command = "wa-synthetic-mcp-never-run"',
            'enabled = false',
        ]) + '\n')
        before = config.read_bytes()
        observations = []
        def real_list(argv, prompt, **kwargs):
            self.assertEqual(argv[-3:], ["mcp", "list", "--json"])
            result = execute(argv, prompt, **kwargs)
            if result.returncode == 0:
                observations.append({r["name"]: r["enabled"] for r in json.loads(result.stdout)})
            return result
        overrides, metadata = codex_mcp_preflight([shutil.which("codex")],
            {"HOME": str(home), "CODEX_HOME": str(codex_home), "XDG_CONFIG_HOME": str(home / "xdg")},
            self.root, process=real_list)
        expected = {"node_repl", "cua_repl", "already_disabled"}
        self.assertEqual(set(observations[0]), expected)
        self.assertIs(observations[0]["node_repl"], True)
        self.assertIs(observations[0]["cua_repl"], True)
        self.assertEqual(set(observations[1]), expected)
        self.assertFalse(any(observations[1].values()))
        self.assertEqual(metadata["disabled_count"], 2)
        self.assertIs(metadata["verified"], True)
        self.assertTrue(overrides)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(marker.exists())
        self.assertNotIn("private-preflight-material", dumps(metadata))

    @unittest.skipUnless(shutil.which("codex"), "optional installed Codex CLI protocol regression")
    def test_installed_cli_disables_http_with_matching_transport_without_exec(self):
        home = self.root / "isolated-http-home"
        codex_home = home / "codex"
        codex_home.mkdir(parents=True)
        config = codex_home / "config.toml"
        config.write_text('[mcp_servers.synthetic_http]\nurl="http://127.0.0.1:9/synthetic-never-contact"\nenabled=true\n')
        before = config.read_bytes()
        calls, observations = [], []
        def only_list(argv, prompt, **kwargs):
            calls.append(argv)
            self.assertEqual(argv[-3:], ["mcp", "list", "--json"])
            result = execute(argv, prompt, **kwargs)
            if result.returncode == 0:
                observations.append({r["name"]: (r["enabled"], r["transport"]["type"]) for r in json.loads(result.stdout)})
            return result
        overrides, metadata = codex_mcp_preflight([shutil.which("codex")],
            {"HOME": str(home), "CODEX_HOME": str(codex_home), "XDG_CONFIG_HOME": str(home / "xdg")},
            self.root, process=only_list)
        self.assertIs(metadata["verified"], True)
        self.assertEqual(observations, [{"synthetic_http": (True, "streamable_http")},
                                       {"synthetic_http": (False, "streamable_http")}])
        self.assertEqual(overrides, ["-c", 'mcp_servers.synthetic_http={url="http://127.0.0.1:9/wa-disabled",enabled=false}'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(config.read_bytes(), before)
