"""Synthetic user OAuth/message protocol; no account, credentials or network."""
import copy
import json
import os
import unittest
from unittest.mock import patch

import test_messages
from test_message_cards import CARD, PROJECTION
from work_assistant import messages
from work_assistant.process import ProcessResult
from work_assistant.util import dumps


class FakeUserCLI:
    def __init__(self, case):
        self.case = case
        self.calls = []
        self.status = {"appId": "cli_fixture", "brand": "feishu", "identity": "user", "verified": True,
                       "identities": {"user": {"status": "ready", "userName": "Synthetic owner",
                           "openId": "ou_owner", "tokenStatus": "valid",
                           "scope": "im:message im:message.send_as_user"}}, "private_extra": "never-persist-this"}
        self.sender = {"sender_type": "user", "id_type": "open_id", "id": "ou_owner"}
        self.timeout = False
        self.envelope_identity = "user"
        self.body = None

    def __call__(self, argv, stdin, **kwargs):
        args = argv[3:]  # same fixed executable/profile for config, auth and API
        self.case.assertEqual(argv[:3], ["synthetic-lark", "--profile", "fixture"])
        self.case.assertFalse(kwargs["inherit_env"])
        self.case.assertEqual(kwargs["env"]["LARKSUITE_CLI_CONFIG_DIR"], str(self.case.root.resolve()))
        for key in ("LARK_ACCESS_TOKEN", "FEISHU_APP_SECRET", "LARKSUITE_CLI_PROFILE", "LARKSUITE_CLI_CREDENTIAL_PROVIDER"):
            self.case.assertNotIn(key, kwargs["env"])
        self.calls.append((args, stdin))
        if args == ["config", "show"]:
            return ProcessResult(dumps({"appId": "cli_fixture", "brand": "feishu"}))
        if args == ["auth", "status", "--json", "--verify"]:
            self.case.assertTrue(all(r["data"]["status"] != "sending" for r in self.case.store.records(messages.KIND)))
            return ProcessResult(dumps(self.status))
        self.case.assertEqual(args[0], "api")
        self.case.assertEqual(args[args.index("--as") + 1], "user")
        if args[1] == "POST":
            self.case.assertEqual(args[2], "/open-apis/im/v1/messages")
            self.case.assertEqual(json.loads(args[args.index("--params") + 1]), {"receive_id_type": "open_id"})
            self.case.assertEqual(args[-2:], ["--data", "-"])
            self.body = json.loads(stdin)
            record = next(r for r in self.case.store.records(messages.KIND) if r["data"]["uuid"] == self.body["uuid"])
            self.case.assertEqual(record["data"]["status"], "sending")
            self.case.assertEqual(record["data"]["expected_sender_user_open_id"], "ou_owner")
            if self.timeout:
                return ProcessResult(timed_out=True)
            data = {"message_id": "om_user", "chat_id": "oc_fixture"}
        else:
            self.case.assertEqual(args[1:3], ["GET", "/open-apis/im/v1/messages/om_user"])
            content = PROJECTION if self.body["msg_type"] == "interactive" else json.loads(self.body["content"])
            data = {"items": [{"message_id": "om_user", "chat_id": "oc_fixture", "msg_type": self.body["msg_type"],
                               "sender": self.sender, "body": {"content": dumps(content)}}]}
        return ProcessResult(dumps({"ok": True, "identity": self.envelope_identity, "data": data}))

    def posts(self):
        return [call for call in self.calls if call[0][:2] == ["api", "POST"]]


class UserMessageTests(unittest.TestCase):
    def setUp(self):
        test_messages.MessageTests.setUp(self)
        self.settings.update(transport="lark_cli", cli_argv=["synthetic-lark"], cli_config_dir=str(self.root),
                             cli_profile="fixture", expected_app_id="cli_fixture", message_user_open_id="ou_owner")
        self.cli = FakeUserCLI(self)
        timer = patch("work_assistant.user_lark_cli.time.sleep")
        timer.start()
        self.addCleanup(timer.stop)

    def send(self, request="user-request", **kwargs):
        return messages.send(self.store, self.settings, request, "open_id", "ou_recipient", self.source,
                             confirm=True, identity="user", process=self.cli, **kwargs)

    def inspect(self, result):
        return messages.inspect(self.store, self.settings, result["action_id"], process=self.cli)

    def test_user_send_inspect_noop_identity_query_body_environment_and_no_secret_record(self):
        with patch.dict(os.environ, {"LARK_ACCESS_TOKEN": "old", "LARKSUITE_CLI_PROFILE": "old",
                                    "LARKSUITE_CLI_CREDENTIAL_PROVIDER": "old"}):
            result = self.send()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["identity"], "user")
            self.assertEqual(result["expected_sender_user_open_id"], "ou_owner")
            self.assertTrue(self.send()["no_op"])
            self.assertEqual(self.inspect(result)["status"], "completed")
        self.assertEqual(len(self.cli.posts()), 1)
        self.assertNotIn("never-persist-this", dumps(self.store.records()))
        self.assertNotIn("Synthetic owner", dumps(self.store.records()))
        self.assertFalse(any("/bot/v3/info" in str(call) for call in self.cli.calls))

    def test_each_missing_scope_rejected_before_post(self):
        for index, scope in enumerate(("im:message", "im:message.send_as_user", "")):
            self.cli.status["identities"]["user"]["scope"] = scope
            self.assertEqual(self.send("scope-" + str(index))["status"], "failed")
        self.assertEqual(self.cli.posts(), [])

    def test_owner_app_status_token_and_exact_verified_checks_before_post(self):
        original = copy.deepcopy(self.cli.status)
        variants = [{"appId": "cli_wrong"}, {"verified": 1}, {"identity": "bot"}, {"brand": "lark"}]
        for index, changes in enumerate(variants):
            self.cli.status = dict(original, **changes)
            self.assertEqual(self.send("status-" + str(index))["status"], "failed")
        for index, changes in enumerate(({"openId": "ou_wrong"}, {"tokenStatus": "expired"},
                                         {"status": "missing"}, {"scope": ["im:message", "im:message.send_as_user"]})):
            self.cli.status = copy.deepcopy(original)
            self.cli.status["identities"]["user"].update(changes)
            self.assertEqual(self.send("owner-" + str(index))["status"], "failed")
        self.assertEqual(self.cli.posts(), [])

    def test_app_sender_or_wrong_user_cannot_verify_user_message(self):
        for index, sender in enumerate(({"sender_type": "app", "id_type": "app_id", "id": "cli_fixture"},
                                       {"sender_type": "user", "id_type": "open_id", "id": "ou_wrong"})):
            self.cli.sender = sender
            self.source.write_text("synthetic sender case " + str(index))
            result = self.send("sender-" + str(index))
            self.assertEqual(result["status"], "verification_pending")
            self.assertFalse(result["checks"]["sender"])

    def test_unknown_never_reposts_or_changes_identity_under_same_request(self):
        self.cli.timeout = True
        result = self.send()
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertTrue(self.send()["no_op"])
        self.assertEqual(self.inspect(result)["status"], "outcome_unknown")
        with self.assertRaises(messages.MessageError):
            messages.send(self.store, self.settings, "user-request", "open_id", "ou_recipient", self.source,
                          confirm=True, identity="bot", process=self.cli)
        with self.assertRaises(messages.MessageError):
            self.send("bypass-unknown")
        self.assertEqual(len(self.cli.posts()), 1)

    def test_inspect_changed_owner_refused_without_cli(self):
        result = self.send()
        count = len(self.cli.calls)
        self.settings["message_user_open_id"] = "ou_different"
        with self.assertRaisesRegex(messages.MessageError, "original trusted user"):
            self.inspect(result)
        self.assertEqual(len(self.cli.calls), count)

    def test_card_user_readback_and_update_explicitly_unsupported(self):
        card = self.root / "card.json"
        card.write_text(dumps(CARD))
        result = messages.send(self.store, self.settings, "user-card", "open_id", "ou_recipient", None,
                               confirm=True, identity="user", card_file=card, process=self.cli)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(json.loads(self.cli.body["content"]), CARD)
        self.assertIn("not independently verified", result["verification_scope"])
        count = len(self.cli.calls)
        with self.assertRaisesRegex(messages.MessageError, "does not support user"):
            messages.update_card(self.store, self.settings, result["action_id"], "user-update", card,
                                 confirm=True, process=self.cli)
        self.assertEqual(len(self.cli.calls), count)

    def test_bot_legacy_record_compatible_and_new_user_not_confused_with_bot(self):
        bot_settings = dict(self.settings, transport="http")
        self.transport.send_error = test_messages.AmbiguousError("synthetic timeout")
        result = messages.send(self.store, bot_settings, "bot-request", "open_id", "ou_recipient", self.source,
                               confirm=True, transport=self.transport)
        record = self.store.get(result["action_id"])
        data = dict(record["data"])
        data.pop("identity")
        self.store.update_record(record["id"], data)
        with self.assertRaises(messages.MessageError):
            self.send("bot-request")
        self.assertEqual(self.send()["status"], "completed")
        self.assertEqual(messages.inspect(self.store, bot_settings, record["id"], transport=self.transport)["identity"], "bot")

    def test_wrong_success_envelope_cannot_become_verified_or_resend(self):
        self.cli.envelope_identity = "bot"
        self.assertEqual(self.send()["status"], "outcome_unknown")
        self.send()
        self.assertEqual(len(self.cli.posts()), 1)

    def test_cli_identity_and_no_owner_or_http_rejected_offline(self):
        args = messages.parser().parse_args(["send", "--request-id", "fixture", "--receive-id-type", "open_id",
            "--receive-id", "ou_fixture", "--text-file", "fixture.txt", "--identity", "user"])
        self.assertEqual(args.identity, "user")
        for settings in (dict(self.settings, transport="http"), dict(self.settings, message_user_open_id=None)):
            with self.assertRaises(messages.MessageError):
                messages.send(self.store, settings, "dry", "open_id", "ou_recipient", self.source, identity="user")
        self.assertEqual(self.cli.calls, [])
