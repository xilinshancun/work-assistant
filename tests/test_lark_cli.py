"""Offline official-CLI contract fixtures; never invokes a real Lark account."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from work_assistant.config import load, doctor
from work_assistant.feishu import publish, reconcile, make_client, Feishu, APIError, AmbiguousError
from work_assistant.lark_cli import LarkCLI
from work_assistant.process import execute, ProcessResult, ProcessLaunchError
from work_assistant.reports import generate_report
from test_runtime import Base

FAKE = r'''
import json, os, sys, time
from pathlib import Path
root = Path(os.environ["LARKSUITE_CLI_CONFIG_DIR"])
mode = json.loads((root / "mode.json").read_text())
a = sys.argv[1:]
body = json.loads(sys.stdin.read()) if "--data" in a else None
polluted = any(k.startswith(("FEISHU_", "LARK_", "OPENCLAW_", "HERMES_", "PASEO_")) or
    (k.startswith("LARKSUITE_") and k not in {"LARKSUITE_CLI_CONFIG_DIR", "LARKSUITE_CLI_NO_UPDATE_NOTIFIER", "LARKSUITE_CLI_NO_SKILLS_NOTIFIER"}) for k in os.environ)
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps({"args": a, "body": body, "polluted": polluted}) + "\n")
if "config" in a:
    print(json.dumps({"appId": mode.get("app", "cli_fixture"), "brand": "feishu", "appSecret": "****"}))
    sys.exit(0)
path = a[a.index("api") + 2]
method = a[a.index("api") + 1]
if path.endswith("/bot/v3/info") and mode.get("auth_error"):
    print(json.dumps({"ok": False, "identity": "bot", "error": {"type": "authentication", "subtype": "token_invalid", "code": 99991663, "message": "synthetic-private"}}), file=sys.stderr)
    sys.exit(3)
if path.endswith("/im/v1/messages") and method == "POST" and mode.get("timeout"):
    time.sleep(10)
if path.endswith("/im/v1/messages") and mode.get("reject"):
    print(json.dumps({"ok": False, "identity": "bot", "error": {"type": "authorization", "subtype": "missing_scope", "message": "synthetic-private"}}), file=sys.stderr)
    sys.exit(4)
data = {}
if path.endswith("/docx/v1/documents"):
    data = {"document": {"document_id": "doc-fixture"}}
elif path.endswith("/children"):
    data = {"children": [{"block_id": "block-%s" % body["index"]}]}
elif path.endswith("/im/v1/messages"):
    data = {"message_id": "message-fixture"}
elif method == "GET":
    data = {"content": "synthetic observation"}
print(json.dumps({"ok": True, "identity": "bot", "data": data}))
'''


class LarkCLITests(Base):
    def setUp(self):
        super().setUp()
        self.directory = self.root / "cli-config"
        self.directory.mkdir()
        self.script = self.root / "fake.py"
        self.script.write_text(FAKE, encoding="utf-8")
        self.mode()
        self.settings = dict(self.conf["publisher"], transport="lark_cli", cli_argv=[sys.executable, str(self.script)],
            cli_config_dir=str(self.directory), expected_app_id="cli_fixture", cli_profile="fixture", cli_timeout=5)
        self.env = patch.dict(os.environ, {"FEISHU_RECEIVE_ID": "synthetic-receiver",
            "FEISHU_APP_SECRET": "old-synthetic", "LARKSUITE_CLI_TENANT_ACCESS_TOKEN": "old-synthetic",
            "LARKSUITE_CLI_APP_ID": "cli_old", "LARKSUITE_CLI_APP_SECRET": "old-synthetic",
            "LARKSUITE_CLI_CREDENTIAL_PROVIDER": "old-provider", "LARKSUITE_CLI_AUTH_PROXY": "old-proxy",
            "LARKSUITE_CLI_PROFILE": "old-profile", "LARK_CHANNEL": "1", "OPENCLAW_HOME": "old", "HERMES_HOME": "old"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pacing = patch("work_assistant.lark_cli.time.sleep")
        self.pacing.start()
        self.addCleanup(self.pacing.stop)
        self.task()
        self.report = generate_report(self.s, self.p["id"], "2026-01-15")

    def mode(self, **values):
        (self.directory / "mode.json").write_text(json.dumps(values))

    def calls(self):
        path = self.directory / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def send(self, **kwargs):
        return publish(self.s, self.settings, self.report["id"], confirm=True, **kwargs)

    def test_create_update_noop_query_body_and_reconcile(self):
        first = self.send()
        self.assertEqual(first["status"], "completed")
        count = len(self.calls())
        self.assertTrue(self.send()["no_op"])
        self.assertEqual(len(self.calls()), count)
        self.task()
        self.report = generate_report(self.s, self.p["id"], "2026-01-15")
        second = self.send()
        self.assertEqual(second["document_id"], first["document_id"])
        self.assertEqual(second["message_id"], first["message_id"])
        calls = self.calls()
        api = [c for c in calls if "api" in c["args"]]
        sends = [c for c in api if c["args"][3:5] == ["POST", "/open-apis/im/v1/messages"]]
        self.assertEqual(len(sends), 1)
        args = sends[0]["args"]
        self.assertEqual(json.loads(args[args.index("--params") + 1]), {"receive_id_type": "chat_id"})
        self.assertIn("uuid", sends[0]["body"])
        self.assertEqual(args[-2:], ["--data", "-"])
        self.assertTrue(any(c["args"][3:5] == ["PATCH", "/open-apis/im/v1/messages/message-fixture"] for c in api))
        self.assertFalse(any(c["polluted"] for c in calls))
        before = len(calls)
        result = reconcile(self.s, self.settings, second["publication_id"])
        self.assertEqual(set(result["inspection"]), {"document", "content", "message"})
        self.assertTrue(all(c["args"][3] == "GET" for c in self.calls()[before:] if "api" in c["args"]))

    def test_app_mismatch_refuses_before_api(self):
        self.mode(app="cli_wrong")
        result = self.send()
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["pending"])
        self.assertEqual(len(self.calls()), 1)

    def test_auth_rejection_has_no_writeahead_and_explicit_retry(self):
        self.mode(auth_error=True)
        def observed(*args, **kwargs):
            self.assertIsNone(self.s.records("publication")[0]["data"]["pending"])
            return execute(*args, **kwargs)
        result = self.send(process=observed)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("synthetic-private", json.dumps(result))
        self.assertIsNone(result["pending"])
        self.mode()
        self.assertEqual(self.send(retry=True)["status"], "completed")

    def test_mutation_timeout_unknown_never_automatically_retried(self):
        self.settings["cli_timeout"] = .2
        self.mode(timeout=True)
        result = self.send()
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["pending"]["stage"], "create_card")
        self.assertEqual(result["document_id"], "doc-fixture")
        before = len(self.calls())
        with self.assertRaises(ValueError):
            self.send(retry=True)
        self.assertEqual(len(self.calls()), before)
        inspected = reconcile(self.s, self.settings, result["publication_id"])
        self.assertIn("document", inspected["inspection"])
        self.assertTrue(all(c["args"][3] == "GET" for c in self.calls()[before:] if "api" in c["args"]))

    def test_definite_permission_rejection_preserves_checkpoint(self):
        self.mode(reject=True)
        result = self.send()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["document_id"], "doc-fixture")
        self.assertIn("authorization/missing_scope", result["error"])
        self.assertNotIn("synthetic-private", result["error"])

    def test_mutation_broken_output_keeps_unknown_checkpoint(self):
        def broken_card(argv, *args, **kwargs):
            result = execute(argv, *args, **kwargs)
            if "/open-apis/im/v1/messages" in argv:
                result.stdout = "{"
            return result
        result = self.send(process=broken_card)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["pending"]["stage"], "create_card")
        self.assertEqual(result["document_id"], "doc-fixture")
        with self.assertRaises(ValueError):
            self.send(retry=True)

    def test_explicit_member_sharing_and_outbound_document_button(self):
        self.settings.update(document_member_env="WA_APPROVED_MEMBER",
                             document_member_perm="edit")
        with patch.dict(os.environ, {"WA_APPROVED_MEMBER": "ou_fixture"}):
            first = self.send()
            self.assertEqual(first["document_member_status"], "granted")
            self.task()
            self.report = generate_report(self.s, self.p["id"], "2026-01-15")
            self.assertEqual(self.send()["status"], "completed")
            grants = [c for c in self.calls() if "/open-apis/drive/v1/permissions/doc-fixture/members" in c["args"]]
            self.assertEqual(len(grants), 1)
            grant = grants[0]
            self.assertEqual(grant["body"], {"member_type": "openid", "member_id": "ou_fixture", "perm": "edit", "type": "user"})
            self.assertEqual(json.loads(grant["args"][grant["args"].index("--params") + 1]),
                             {"type": "docx", "need_notification": "false"})
            self.assertTrue(self.send()["no_op"])
        cards = [json.loads(c["body"]["content"]) for c in self.calls() if c["body"] and "content" in c["body"]]
        for card in cards:
            button = card["elements"][-1]["actions"][0]
            self.assertEqual(button["url"], "https://www.feishu.cn/docx/doc-fixture")
            self.assertEqual(button["text"]["content"], "打开 Daily Sync")
        path = Path(self.report["data"]["path"]) / "card.json"
        self.assertNotIn("https://www.feishu.cn", path.read_text())
        with patch.dict(os.environ, {"WA_APPROVED_MEMBER": "ou_other"}):
            self.assertNotEqual(LarkCLI(self.settings).destination(), first["destination"])

    def test_sharing_timeout_is_unknown_and_blocks_card(self):
        self.settings["document_member_env"] = "WA_APPROVED_MEMBER"
        def timeout(argv, *args, **kwargs):
            if "/open-apis/drive/v1/permissions/doc-fixture/members" in argv:
                return ProcessResult(timed_out=True)
            return execute(argv, *args, **kwargs)
        with patch.dict(os.environ, {"WA_APPROVED_MEMBER": "ou_fixture"}):
            result = self.send(process=timeout)
            self.assertEqual(result["status"], "outcome_unknown")
            self.assertEqual(result["pending"]["stage"], "share_document_member")
            self.assertIsNone(result["message_id"])
            self.assertEqual(result["document_id"], "doc-fixture")
            with self.assertRaises(ValueError):
                self.send(retry=True)

    def test_document_config_rejects_group_member_and_invalid_permission(self):
        with self.assertRaises(ValueError):
            LarkCLI(dict(self.settings, document_member_perm="view"))
        client = LarkCLI(dict(self.settings, document_member_env="WA_APPROVED_MEMBER"))
        with patch.dict(os.environ, {"WA_APPROVED_MEMBER": "oc_group"}), self.assertRaises(APIError):
            client.destination()

    def test_preflight_timeout_is_failed_before_any_mutation(self):
        def timeout(argv, *args, **kwargs):
            if "/open-apis/bot/v3/info" in argv:
                return ProcessResult(timed_out=True)
            return execute(argv, *args, **kwargs)
        result = self.send(process=timeout)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["pending"])
        self.assertEqual(result["steps"], [])
        self.assertEqual(len(self.calls()), 1)

    def test_strict_success_and_ambiguous_errors(self):
        malformed = ["", "{", '{}', '{"ok":true,"identity":"user","data":{}}',
                     '{"ok":false,"ok":true,"identity":"bot","data":{}}',
                     '{"ok":true,"identity":"bot","data":{"n":NaN}}',
                     '{"ok":true,"identity":"bot","dry_run":true,"data":{}}',
                     '{"ok":true,"identity":"bot","data":[]}']
        results = [ProcessResult(stdout=s) for s in malformed]
        results += [ProcessResult(timed_out=True), ProcessResult(truncated=True), ProcessResult(returncode=-9),
                    ProcessResult(returncode=1, stderr=json.dumps({"ok": False, "identity": "bot", "error": {
                        "type": "network", "subtype": "server_error", "message": "private"}}))]
        for response in results:
            with self.subTest(response=response):
                client = LarkCLI(self.settings, process=lambda *a, **kw: response)
                client.authenticated = True
                with self.assertRaises(AmbiguousError):
                    client.request("POST", "/docx/v1/documents", {"title": "test"})

    def test_launched_oserror_is_ambiguous_but_launch_failure_definite(self):
        for exc, cls in [(OSError("private"), AmbiguousError), (ProcessLaunchError("private"), APIError)]:
            def failure(*a, **kw):
                raise exc
            client = LarkCLI(self.settings, process=failure)
            client.authenticated = True
            with self.assertRaises(cls) as caught:
                client.request("POST", "/docx/v1/documents", {})
            self.assertNotIn("private", str(caught.exception))

    def test_config_paths_and_validation(self):
        path = self.root / "relative.json"
        values = dict(self.settings, cli_argv=["./tool"], cli_config_dir="relative-dir")
        path.write_text(json.dumps({"publisher": values}))
        conf = load(path)
        self.assertEqual(conf["publisher"]["cli_config_dir"], str((self.root / "relative-dir").resolve()))
        self.assertEqual(conf["publisher"]["cli_argv"][0], str((self.root / "tool").resolve()))
        for changes in [{"mode": "webhook"}, {"expected_app_id": ""}, {"cli_argv": "shell"}, {"cli_timeout": float("inf")}]:
            path.write_text(json.dumps({"publisher": dict(values, **changes)}))
            with self.assertRaises(ValueError):
                load(path)

    def test_doctor_only_checks_selected_config(self):
        conf = dict(self.conf, providers={}, publisher=self.settings)
        result = doctor(conf)["feishu"]
        self.assertTrue(result["installed"])
        self.assertTrue(result["app_id_matches"])
        self.assertFalse(result["live_tested"])
        self.assertEqual(len(self.calls()), 1)
        self.mode(app="cli_wrong")
        self.assertFalse(doctor(conf)["feishu"]["app_id_matches"])

    def test_default_http_and_same_destination_identity(self):
        with patch.dict(os.environ, {"FEISHU_APP_ID": "cli_fixture"}):
            default = dict(self.settings, transport="http")
            self.assertIsInstance(make_client(default), Feishu)
            self.assertEqual(make_client(default).destination(), LarkCLI(self.settings).destination())

    def test_full_environment_runner_does_not_reintroduce_parent(self):
        with patch.dict(os.environ, {"WA_SYNTHETIC_AMBIENT": "old"}):
            result = execute([sys.executable, "-c", "import os; print(os.environ.get('WA_SYNTHETIC_AMBIENT','absent'))"],
                "", cwd=self.root, env={"PATH": os.environ.get("PATH", "")}, inherit_env=False)
        self.assertEqual(result.stdout.strip(), "absent")
