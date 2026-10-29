"""Synthetic outgoing messages, injected HTTP/CLI; no live accounts or services."""
import copy
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from work_assistant import messages
from work_assistant.config import load
from work_assistant.feishu import APIError, AmbiguousError
from work_assistant.process import ProcessResult
from work_assistant.store import Busy, Store
from work_assistant.util import digest, dumps
from test_runtime import patch_loopback_environment


class FakeHTTP:
    def __init__(self, store):
        self.store, self.calls = store, []
        self.auth_error = self.send_error = self.get_error = None
        self.send_response = None
        self.message_changes = {}
        self.chats_pages = [{"items": [], "has_more": False}]
        self.text = None

    def __call__(self, method, url, body=None, headers=None):
        self.calls.append((method, url, copy.deepcopy(body)))
        path = urlsplit(url).path
        if path.endswith("/tenant_access_token/internal"):
            if self.auth_error:
                raise self.auth_error
            return {"code": 0, "tenant_access_token": "synthetic-token-never-persisted"}
        if method == "POST" and path.endswith("/im/v1/messages"):
            action = self.store.records(messages.KIND)[0]["data"]
            assert action["status"] == "sending"
            assert action["uuid"] == body["uuid"]
            assert len(body["uuid"]) <= 50
            artifact = Path(action["source_artifact"])
            assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
            assert digest(artifact.read_bytes()) == action["text_sha256"]
            if self.send_error:
                raise self.send_error
            self.text = json.loads(body["content"])["text"]
            return {"code": 0, "data": self.send_response if self.send_response is not None
                    else {"message_id": "om_fixture", "chat_id": "oc_fixture"}}
        if method == "GET" and "/im/v1/messages/" in path:
            if self.get_error:
                raise self.get_error
            message = {"message_id": "om_fixture", "chat_id": "oc_fixture", "msg_type": "text", "deleted": False,
                       "sender": {"id": "cli_fixture", "id_type": "app_id", "sender_type": "app"},
                       "body": {"content": dumps({"text": self.text})}}
            message.update(self.message_changes)
            return {"code": 0, "data": {"items": [message]}}
        if method == "GET" and path.endswith("/im/v1/chats"):
            params = parse_qs(urlsplit(url).query)
            index = int(params.get("page_token", ["0"])[0])
            return {"code": 0, "data": self.chats_pages[index]}
        if path.endswith("/bot/v3/info"):
            return {"code": 0, "data": {"bot": {"open_id": "ou_synthetic_bot"}}}
        raise AssertionError("unexpected fixture request")

    def posts(self):
        return [call for call in self.calls if call[0] == "POST" and urlsplit(call[1]).path.endswith("/im/v1/messages")]


class MessageTests(unittest.TestCase):
    def setUp(self):
        patch_loopback_environment(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)
        self.settings = load(self.root / "config.json")["publisher"]
        self.transport = FakeHTTP(self.store)
        self.text = '合成会话文字；尚未独立复核。\n引号 " 与反斜杠 \\、emoji 😀。\n'
        self.source = self.root / "message.txt"
        self.source.write_bytes(self.text.encode("utf-8"))
        self.env = patch.dict(os.environ, {"FEISHU_APP_ID": "cli_fixture", "FEISHU_APP_SECRET": "synthetic-secret",
                                          "FEISHU_RECEIVE_ID": "shared-binding-must-not-be-used"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def send(self, request="session-fixture/request-1", kind="chat_id", receive="oc_fixture", confirm=True, **kwargs):
        return messages.send(self.store, self.settings, request, kind, receive, self.source,
                             confirm=confirm, transport=self.transport, **kwargs)

    def inspect(self, receipt):
        return messages.inspect(self.store, self.settings, receipt["action_id"], transport=self.transport)

    def test_dry_run_needs_no_credentials_client_transport_or_durable_action(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(messages, "make_client", side_effect=AssertionError("no client")):
            result = self.send(confirm=False)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["text"], self.text)
        self.assertTrue(result["confirm_required"])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.store.records(messages.KIND), [])
        self.assertFalse((self.store.root / "messages").exists())

    def test_send_readback_exact_plain_text_private_source_and_repeat_noop(self):
        settings_before, source_before = copy.deepcopy(self.settings), self.source.read_bytes()
        result = self.send()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["verification_status"], "verified")
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["message_id"], "om_fixture")
        post = self.transport.posts()[0][2]
        self.assertEqual(post["receive_id"], "oc_fixture")
        self.assertEqual(post["msg_type"], "text")
        self.assertEqual(json.loads(post["content"]), {"text": self.text})
        self.assertEqual(self.settings, settings_before)
        self.assertEqual(self.source.read_bytes(), source_before)
        action = self.store.get(result["action_id"])
        artifact = Path(action["data"]["source_artifact"])
        self.assertEqual(artifact.read_bytes(), source_before)
        self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
        self.assertNotIn("synthetic-token-never-persisted", dumps(self.store.records()))
        count = len(self.transport.calls)
        repeated = self.send()
        self.assertTrue(repeated["no_op"])
        self.assertEqual(repeated["action_id"], result["action_id"])
        self.assertEqual(repeated["uuid"], result["uuid"])
        self.assertEqual(len(self.transport.calls), count)

    def test_same_logical_request_cannot_change_target_content_or_receiver_type(self):
        self.send()
        count = len(self.transport.calls)
        for kind, receive in (("chat_id", "oc_other"), ("open_id", "ou_fixture")):
            with self.assertRaisesRegex(messages.MessageError, "different target/content"):
                self.send(kind=kind, receive=receive)
        self.source.write_text("different synthetic text")
        for confirm in (True, False):
            with self.assertRaisesRegex(messages.MessageError, "different target/content"):
                self.send(confirm=confirm)
        self.assertEqual(len(self.transport.calls), count)

    def test_open_id_uses_explicit_target_and_receipt_chat_id_for_readback(self):
        result = self.send(kind="open_id", receive="ou_fixture")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["chat_id"], "oc_fixture")
        self.assertEqual(self.transport.posts()[0][2]["receive_id"], "ou_fixture")
        self.assertIn("receive_id_type=open_id", self.transport.posts()[0][1])

    def test_open_id_missing_receipt_chat_id_is_not_adopted_from_get(self):
        self.transport.send_response = {"message_id": "om_fixture"}
        result = self.send(kind="open_id", receive="ou_fixture")
        self.assertEqual(result["status"], "verification_pending")
        self.assertIsNone(result["chat_id"])
        self.assertFalse(result["checks"]["chat"])
        self.assertEqual(result["message_id"], "om_fixture")
        self.assertEqual(self.inspect(result)["status"], "verification_pending")
        self.assertEqual(len(self.transport.posts()), 1)

    def test_readback_failures_keep_sent_id_never_resend_and_inspect_can_finish(self):
        self.transport.get_error = AmbiguousError("token=synthetic-secret-not-for-output")
        result = self.send()
        self.assertEqual(result["status"], "verification_pending")
        self.assertEqual(result["message_id"], "om_fixture")
        self.assertTrue(result["send_acknowledged"])
        self.assertNotIn("synthetic-secret-not-for-output", dumps(result))
        count = len(self.transport.calls)
        self.assertEqual(self.send()["status"], "verification_pending")
        self.assertEqual(len(self.transport.calls), count)
        self.transport.get_error = None
        before = len(self.transport.calls)
        self.assertEqual(self.inspect(result)["status"], "completed")
        self.assertTrue(all(method == "GET" or url.endswith("/tenant_access_token/internal")
                            for method, url, _ in self.transport.calls[before:]))
        self.assertEqual(len(self.transport.posts()), 1)

    def test_bad_missing_sender_content_id_chat_type_and_deleted_not_verified(self):
        cases = [{"sender": None}, {"sender": {"id": "cli_other", "id_type": "app_id", "sender_type": "app"}},
                 {"sender": {"id": "cli_fixture", "id_type": "open_id", "sender_type": "user"}},
                 {"body": None}, {"body": {"content": "invalid-json"}},
                 {"body": {"content": dumps({"text": "different synthetic text"})}},
                 {"message_id": "om_other"}, {"chat_id": "oc_other"}, {"msg_type": "post"}, {"deleted": True}]
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                self.transport.message_changes = changes
                self.source.write_text("synthetic case %s" % index)
                result = self.send(request="request-%s" % index)
                self.assertEqual(result["status"], "verification_pending")
                self.assertEqual(result["message_id"], "om_fixture")
                self.assertIsNone(result.get("verified_at"))
                before = len(self.transport.posts())
                self.send(request="request-%s" % index)
                self.assertEqual(len(self.transport.posts()), before)

    def test_ambiguous_or_missing_ack_blocks_same_request_and_new_id_bypass(self):
        self.transport.send_error = AmbiguousError("synthetic lost response")
        result = self.send()
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIsNone(result["message_id"])
        count = len(self.transport.calls)
        self.assertEqual(self.send()["status"], "outcome_unknown")
        with self.assertRaisesRegex(messages.MessageError, "unresolved action"):
            self.send(request="replacement-id-must-not-bypass")
        with patch.object(messages, "make_client", side_effect=AssertionError("no remote ID; do not guess")):
            self.assertEqual(self.inspect(result)["status"], "outcome_unknown")
        self.assertEqual(len(self.transport.calls), count)

    def test_missing_send_message_id_is_unknown(self):
        self.transport.send_response = {"chat_id": "oc_fixture"}
        result = self.send()
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(len(self.transport.posts()), 1)
        self.assertFalse(any(m == "GET" and "/im/v1/messages/" in u for m, u, _ in self.transport.calls))

    def test_auth_failure_is_failed_before_send_intent_and_never_retried(self):
        self.transport.auth_error = AmbiguousError("synthetic token unavailable")
        result = self.send()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verification_status"], "not_sent")
        self.assertFalse(result["send_acknowledged"])
        self.assertEqual(self.transport.posts(), [])
        count = len(self.transport.calls)
        self.assertEqual(self.send()["status"], "failed")
        self.assertEqual(len(self.transport.calls), count)

    def test_post_api_error_and_interrupt_are_unknown_without_guessing_error_text(self):
        self.transport.send_error = APIError("synthetic rejected code 400; definitely not sent")
        result = self.send()
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["verification_status"], "unresolved")
        self.assertNotIn("definitely", result["error"])
        count = len(self.transport.calls)
        self.assertTrue(self.send()["no_op"])
        with self.assertRaisesRegex(messages.MessageError, "unresolved action"):
            self.send(request="new-key-cannot-guess-absence")
        self.assertEqual(len(self.transport.calls), count)
        self.transport.send_error = KeyboardInterrupt()
        self.source.write_text("a new explicitly authorized synthetic request")
        with self.assertRaises(KeyboardInterrupt):
            self.send(request="interrupt-fixture")
        self.assertEqual(self.store.get(messages._action_id("interrupt-fixture"))["data"]["status"], "outcome_unknown")

    def test_resolve_absent_dry_run_does_not_change_state_or_contact_network(self):
        self.transport.send_error = APIError("synthetic uncertain response")
        action = self.send()
        before = self.store.records()
        count = len(self.transport.calls)
        with patch.dict(os.environ, {}, clear=True), patch.object(messages, "make_client", side_effect=AssertionError("local only")):
            result = messages.resolve(self.store, action["action_id"], absent=True, reason="Operator checked the synthetic destination.")
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["prior_status"], "outcome_unknown")
        self.assertEqual(result["proposed_resolution"]["basis"], "operator_assertion")
        self.assertTrue(result["confirm_required"])
        self.assertEqual(self.store.records(), before)
        self.assertEqual(len(self.transport.calls), count)

    def test_resolve_absent_keeps_history_and_never_resends_old_request(self):
        for mode in ("unknown", "known_id"):
            with self.subTest(mode=mode):
                request = "resolve-fixture/" + mode
                self.transport.send_error = APIError("uncertain send") if mode == "unknown" else None
                self.transport.get_error = APIError("read unavailable")
                result = self.send(request=request)
                before = self.store.get(result["action_id"])["data"]
                source_before = Path(before["source_artifact"]).read_bytes()
                count = len(self.transport.calls)
                reason = "操作者已核实合成目标不存在该消息／未投递。"
                with patch.object(messages, "make_client", side_effect=AssertionError("local only")):
                    receipt = messages.resolve(self.store, result["action_id"], absent=True, reason=reason, confirm=True)
                    self.assertEqual(self.inspect(receipt)["status"], "resolved_absent")
                    repeated = self.send(request=request)
                self.assertTrue(repeated["no_op"])
                self.assertEqual(repeated["status"], "resolved_absent")
                self.assertEqual(receipt["verification_status"], "operator_confirmed_absent")
                self.assertEqual(receipt["resolution"]["basis"], "operator_assertion")
                self.assertEqual(receipt["resolution"]["reason"], reason)
                self.assertEqual(receipt["resolution"]["prior_status"], before["status"])
                self.assertTrue(receipt["resolution"]["resolved_at"])
                after = self.store.get(result["action_id"])["data"]
                for key in set(before) - {"status", "verification_status"}:
                    self.assertEqual(after[key], before[key], key)
                self.assertEqual(Path(after["source_artifact"]).read_bytes(), source_before)
                audit = self.store.records("message_resolution")[0]["data"]
                self.assertEqual(audit["action_id"], result["action_id"])
                self.assertEqual(audit["prior_state"], before)
                self.assertEqual(audit["resolution"], receipt["resolution"])
                self.assertEqual(len(self.transport.calls), count)

    def test_resolved_absent_unblocks_only_new_explicit_request_without_auto_retry(self):
        self.transport.send_error = APIError("uncertain send")
        action = self.send()
        with self.assertRaisesRegex(messages.MessageError, "unresolved action"):
            self.send(request="separately-authorized-request")
        messages.resolve(self.store, action["action_id"], absent=True, reason="Operator checked absence.", confirm=True)
        count = len(self.transport.calls)
        self.assertEqual(self.send(request="separately-authorized-request", confirm=False)["status"], "dry_run")
        self.assertEqual(len(self.transport.calls), count)
        self.assertEqual(len(self.store.records(messages.KIND)), 1)
        self.transport.send_error = None
        receipt = self.send(request="separately-authorized-request")
        self.assertEqual(receipt["status"], "completed")
        self.assertNotEqual(receipt["uuid"], action["uuid"])
        self.assertEqual(len(self.transport.posts()), 2)
        self.assertEqual(self.store.get(action["action_id"])["data"]["status"], "resolved_absent")

    def test_resolve_rejects_completed_failed_wrong_kind_invalid_reason_and_flags(self):
        receipt = self.send()
        for confirm in (False, True):
            with self.assertRaisesRegex(messages.MessageError, "requires outcome_unknown"):
                messages.resolve(self.store, receipt["action_id"], absent=True, reason="Checked.", confirm=confirm)
        self.transport.auth_error = APIError("auth unavailable")
        self.source.write_text("another synthetic request")
        failed = self.send(request="failed-auth-fixture")
        with self.assertRaisesRegex(messages.MessageError, "requires outcome_unknown"):
            messages.resolve(self.store, failed["action_id"], absent=True, reason="Checked.", confirm=True)
        for reason in (None, "", " \n\t", 7, "x" * 2001, "bad\x00reason"):
            with self.subTest(reason=repr(reason)[:30]), self.assertRaisesRegex(messages.MessageError, "reason must"):
                messages.resolve(self.store, receipt["action_id"], absent=True, reason=reason, confirm=True)
        for flags in ({"absent": False}, {"absent": 1}, {"absent": True, "confirm": 1}):
            with self.assertRaises(messages.MessageError):
                messages.resolve(self.store, receipt["action_id"], reason="Checked.", **flags)
        wrong = self.store.record("event", {})
        for ident in (wrong["id"], "message_unknown"):
            with self.assertRaises(messages.MessageError):
                messages.resolve(self.store, ident, absent=True, reason="Checked.", confirm=True)
        self.assertEqual(self.store.records("message_resolution"), [])

    def test_resolve_uses_both_locks_and_rereads_status_under_lock(self):
        self.transport.send_error = APIError("uncertain send")
        receipt = self.send()
        resources = ["message:" + digest(receipt["request_id"]),
                     "message-payload:" + digest([receipt["target_sha256"], receipt["text_sha256"]])]
        for resource in resources:
            with self.store.inspection_lock([resource]), self.assertRaises(Busy):
                messages.resolve(self.store, receipt["action_id"], absent=True, reason="Checked.", confirm=True)
        original_lock = self.store.inspection_lock
        @contextmanager
        def finish_before_lock(resources):
            messages._save(self.store, self.store.get(receipt["action_id"]), status="completed")
            with original_lock(resources):
                yield
        with patch.object(self.store, "inspection_lock", finish_before_lock), self.assertRaisesRegex(messages.MessageError, "requires outcome_unknown"):
            messages.resolve(self.store, receipt["action_id"], absent=True, reason="Checked.", confirm=True)
        self.assertEqual(self.store.records("message_resolution"), [])

    def test_resolve_audit_and_status_update_are_atomic(self):
        self.transport.send_error = APIError("uncertain send")
        receipt = self.send()
        before = self.store.records()
        with patch.object(self.store, "update_record", side_effect=OSError("synthetic write failure")), self.assertRaises(OSError):
            messages.resolve(self.store, receipt["action_id"], absent=True, reason="Checked.", confirm=True)
        self.assertEqual(self.store.records(), before)

    def test_resolve_cli_local_dry_run_confirm_and_no_sent_adoption(self):
        self.transport.get_error = APIError("read unavailable")
        receipt = self.send()
        path = self.root / "resolve-config.json"
        path.write_text(dumps({"state_dir": str(self.store.root)}))
        args = ["--config", str(path), "resolve", receipt["action_id"], "--absent", "--reason", "Operator checked absence."]
        with patch.dict(os.environ, {}, clear=True), patch.object(messages, "make_client", side_effect=AssertionError("local only")):
            for confirm, status in (([], "dry_run"), (["--confirm"], "resolved_absent")):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(messages.main(args + confirm), 0)
                self.assertEqual(json.loads(output.getvalue())["status"], status)
            for invalid in (args + ["--sent"], ["resolve", receipt["action_id"], "--reason", "Checked."],
                            args[:-1] + [" \t"]):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(messages.main(invalid), 2)
                self.assertEqual(json.loads(output.getvalue())["status"], "failed")

    def test_lock_excludes_reentry_before_any_network_or_record(self):
        resources = ["message:" + digest("session-fixture/request-1"),
                     "message-payload:" + digest([digest(["chat_id", "oc_fixture"]), digest(self.source.read_bytes())])]
        for resource in resources:
            with self.store.inspection_lock([resource]), self.assertRaises(Busy):
                self.send()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.store.records(messages.KIND), [])

    def test_parent_crash_after_writeahead_recovers_unknown_without_post(self):
        script = '''import os,sys
from work_assistant.messages import send
from work_assistant.store import Store
from work_assistant.config import load
store=Store(sys.argv[1])
def fake(method,url,body=None,headers=None):
    if url.endswith('/tenant_access_token/internal'): return {'code':0,'tenant_access_token':'synthetic-token'}
    os._exit(19)
send(store,load(sys.argv[2])['publisher'],'crash-request','chat_id','oc_fixture',sys.argv[3],confirm=True,transport=fake)
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.store.root), str(self.root / "absent-config.json"), str(self.source)],
                                env={"PATH": os.environ.get("PATH", ""), "FEISHU_APP_ID": "cli_fixture", "FEISHU_APP_SECRET": "synthetic-secret"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 19, result.stderr.decode())
        ident = messages._action_id("crash-request")
        self.assertEqual(self.store.get(ident)["data"]["status"], "sending")
        self.assertEqual(self.send(request="crash-request")["status"], "outcome_unknown")
        self.assertEqual(self.transport.calls, [])

    def test_inspect_rejects_different_app_and_modified_private_source(self):
        result = self.send()
        with patch.dict(os.environ, {"FEISHU_APP_ID": "cli_other"}), self.assertRaisesRegex(messages.MessageError, "original application"):
            self.inspect(result)
        action = self.store.get(result["action_id"])
        Path(action["data"]["source_artifact"]).write_text("tampered synthetic copy")
        before = len(self.transport.calls)
        pending = self.inspect(result)
        self.assertEqual(pending["status"], "verification_pending")
        self.assertEqual(pending["message_id"], "om_fixture")
        self.assertEqual(len(self.transport.calls), before)
        self.assertEqual(pending["checks"], {})

    def test_chats_readonly_pagination_exact_names_and_invalid_tokens_refused(self):
        self.transport.chats_pages = [
            {"items": [{"chat_id": "oc_one", "name": "合成 群 A"}], "has_more": True, "page_token": "1"},
            {"items": [{"chat_id": "oc_two", "name": "合成 群 A"}], "has_more": False}]
        result = messages.chats(self.settings, transport=self.transport)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["chats"], [{"chat_id": "oc_one", "name": "合成 群 A"}, {"chat_id": "oc_two", "name": "合成 群 A"}])
        self.assertEqual(self.transport.posts(), [])
        self.transport.chats_pages[1].update(has_more=True, page_token="1")
        with self.assertRaisesRegex(messages.MessageError, "repeated"):
            messages.chats(self.settings, transport=self.transport)

    def test_invalid_source_target_request_and_webhook_do_not_contact_transport(self):
        for raw in (b"", b"  \n", b"\xff", b"bad\x00text", b"x" * (messages.MAX_TEXT_BYTES + 1)):
            self.source.write_bytes(raw)
            with self.assertRaises(messages.MessageError):
                self.send()
        self.source.write_text("valid synthetic text")
        for kwargs in ({"request": ""}, {"receive": "oc_"}, {"kind": "open_id", "receive": "oc_wrong"}):
            with self.assertRaises(messages.MessageError):
                self.send(**kwargs)
        self.settings["mode"] = "webhook"
        with self.assertRaises(messages.MessageError):
            self.send(confirm=False)
        self.assertEqual(self.transport.calls, [])

    def test_lark_cli_transport_pinned_app_bot_identity_and_explicit_target(self):
        directory = self.root / "cli"
        directory.mkdir()
        settings = dict(self.settings, transport="lark_cli", cli_argv=["synthetic-lark"], cli_config_dir=str(directory),
                        expected_app_id="cli_fixture", cli_timeout=5)
        calls = []
        def process(argv, stdin, **kwargs):
            calls.append((argv, kwargs))
            self.assertFalse(kwargs["inherit_env"])
            self.assertNotIn("FEISHU_APP_SECRET", kwargs["env"])
            if "config" in argv:
                value = {"appId": "cli_fixture", "brand": "feishu"}
            else:
                self.assertEqual(argv[argv.index("--as") + 1], "bot")
                method, path = argv[argv.index("api") + 1:argv.index("api") + 3]
                if "--params" in argv:
                    path += "?" + urlencode(json.loads(argv[argv.index("--params") + 1]))
                data = self.transport(method, "https://fixture.invalid" + path.replace("/open-apis", "", 1),
                                      json.loads(stdin) if stdin else None)["data"]
                value = {"ok": True, "identity": "bot", "data": data}
            return ProcessResult(stdout=dumps(value))
        with patch("work_assistant.lark_cli.time.sleep"):
            result = messages.send(self.store, settings, "cli-request", "chat_id", "oc_fixture", self.source,
                                   confirm=True, process=process)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any("config" in argv for argv, _ in calls))
        self.assertEqual(self.transport.posts()[0][2]["receive_id"], "oc_fixture")

    def test_cli_json_dry_run_input_error_and_pending_exit_codes(self):
        path = self.root / "module-config.json"
        path.write_text(dumps({"state_dir": str(self.store.root)}))
        args = ["--config", str(path), "send", "--request-id", "cli-direct", "--receive-id-type", "chat_id",
                "--receive-id", "oc_fixture", "--text-file", str(self.source)]
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(messages.main(args), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "dry_run")
        self.transport.get_error = APIError("synthetic readback rejection")
        from work_assistant.feishu import Feishu
        with patch.object(messages, "make_client", return_value=Feishu(self.settings, self.transport)), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(messages.main(args + ["--confirm"]), 3)
        self.assertEqual(json.loads(output.getvalue())["message_id"], "om_fixture")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(messages.main(["send"]), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")
        result = subprocess.run([sys.executable, "-m", "work_assistant.messages"] + args,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(json.loads(result.stdout)["status"], "dry_run")
