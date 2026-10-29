"""Source-bound bot replies, using synthetic official reply/GET fixtures only."""
import copy
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import test_messages
from work_assistant import messages
from work_assistant.feishu import APIError, AmbiguousError
from work_assistant.message_cards import card_projection
from work_assistant.process import ProcessResult
from work_assistant.util import digest, dumps


class FakeReplyHTTP(test_messages.FakeHTTP):
    def __init__(self, store, source="om_source"):
        super().__init__(store)
        self.sources = {source: {"message_id": source, "chat_id": "oc_fixture", "deleted": False}}
        self.source_error = None
        self.relation = {"parent_id": source, "root_id": source}
        self.ack_changes = {}
        self.reply_messages = {}

    def __call__(self, method, url, body=None, headers=None):
        path = urlsplit(url).path
        if method == "GET" and path.rsplit("/", 1)[-1] in self.sources:
            self.calls.append((method, url, copy.deepcopy(body)))
            if self.source_error:
                raise self.source_error
            return {"code": 0, "data": {"items": [copy.deepcopy(self.sources[path.rsplit('/', 1)[-1]])]}}
        if method == "POST" and path.endswith("/reply"):
            self.calls.append((method, url, copy.deepcopy(body)))
            action = next(r["data"] for r in self.store.records(messages.KIND) if r["data"]["uuid"] == body["uuid"])
            assert action["status"] == "sending"
            assert action["reply_source"]["message_id"] in path
            assert len(body["uuid"]) <= 50
            assert set(body) == {"content", "msg_type", "uuid", "reply_in_thread"}
            if self.send_error:
                raise self.send_error
            ident = "om_reply_" + str(len(self.posts()))
            ack = dict(self.relation, message_id=ident, chat_id="oc_fixture")
            content = json.loads(body["content"])
            if body["msg_type"] == "interactive":
                content = card_projection(content)
            self.reply_messages[ident] = dict(ack, msg_type=body["msg_type"],
                sender={"id": "cli_fixture", "id_type": "app_id", "sender_type": "app"},
                body={"content": dumps(content)})
            ack.update(self.ack_changes)
            return {"code": 0, "data": ack}
        if method == "GET" and path.rsplit("/", 1)[-1] in self.reply_messages:
            self.calls.append((method, url, copy.deepcopy(body)))
            if self.get_error:
                raise self.get_error
            message = dict(self.reply_messages[path.rsplit('/', 1)[-1]], **self.message_changes)
            return {"code": 0, "data": {"items": [message]}}
        return super().__call__(method, url, body, headers)

    def posts(self):
        return [c for c in self.calls if c[0] == "POST" and
                (urlsplit(c[1]).path.endswith("/reply") or urlsplit(c[1]).path.endswith("/im/v1/messages"))]


class ReplyMessageTests(unittest.TestCase):
    def setUp(self):
        test_messages.MessageTests.setUp(self)
        self.transport = FakeReplyHTTP(self.store)

    def send(self, request="reply-request", source="om_source", thread=False, **kwargs):
        return messages.send(self.store, self.settings, request, "chat_id", "oc_fixture", self.source,
            confirm=True, transport=self.transport, reply_to_message_id=source, reply_in_thread=thread, **kwargs)

    def inspect(self, result):
        return messages.inspect(self.store, self.settings, result["action_id"], transport=self.transport)

    def test_official_source_relation_matrix_and_requested_vs_observed_placement(self):
        cases = [({}, False, {"parent_id": "om_source", "root_id": "om_source"}),
            ({}, False, {"parent_id": "om_source", "root_id": "om_source", "thread_id": "omt_forced"}),
            ({}, True, {"parent_id": "om_source", "root_id": "om_source", "thread_id": "omt_new"}),
            ({"thread_id": "omt_existing"}, False,
             {"parent_id": "om_source", "root_id": "om_source", "thread_id": "omt_existing"}),
            ({"parent_id": "om_parent", "root_id": "om_root", "thread_id": "omt_existing"}, False,
             {"parent_id": "om_root", "root_id": "om_root", "thread_id": "omt_existing"}),
            ({"parent_id": "om_parent", "root_id": "om_root", "thread_id": "omt_existing"}, True,
             {"parent_id": "om_root", "root_id": "om_root", "thread_id": "omt_existing"}),
            ({"parent_id": "om_root", "root_id": "om_root"}, False,
             {"parent_id": "om_source", "root_id": "om_root"}),
            ({"parent_id": "om_root", "root_id": "om_root"}, True,
             {"parent_id": "om_source", "root_id": "om_source", "thread_id": "omt_new"})]
        for index, (source, thread, relation) in enumerate(cases):
            with self.subTest(index=index):
                self.transport.sources["om_source"] = dict(source, message_id="om_source", chat_id="oc_fixture")
                self.transport.relation = relation
                result = self.send("matrix-" + str(index), thread=thread)
                self.assertEqual(result["status"], "completed")
                self.assertTrue(all(result["checks"].values()))
                self.assertEqual(result["target_sha256"], digest(["reply", "oc_fixture", "om_source", thread]))
                self.assertEqual(result["requested_placement"], "thread" if thread else "main")
                observed = "thread" if relation.get("thread_id") else "main"
                self.assertEqual(result["observed_placement"], observed)
                self.assertEqual(result["placement_matches_request"], observed == result["requested_placement"])
                self.assertEqual(result["observed_thread_id"], relation.get("thread_id"))
                post = self.transport.posts()[-1]
                self.assertTrue(post[1].endswith("/im/v1/messages/om_source/reply"))
                self.assertEqual(post[2]["reply_in_thread"], thread)
                self.assertEqual(json.loads(post[2]["content"]), {"text": self.text})
                before = len(self.transport.calls)
                self.assertTrue(self.send("matrix-" + str(index), thread=thread)["no_op"])
                self.assertEqual(len(self.transport.calls), before)

    def test_wrong_source_chat_deleted_or_bad_source_relationship_never_posts(self):
        for index, change in enumerate(({"chat_id": "oc_other"}, {"message_id": "om_wrong"},
                {"deleted": True}, {"root_id": "wrong"}, {"thread_id": 1})):
            self.transport.sources["om_source"] = dict({"message_id": "om_source", "chat_id": "oc_fixture"}, **change)
            result = self.send("bad-source-" + str(index))
            self.assertEqual(result["status"], "failed"); self.assertFalse(result["send_acknowledged"])
        self.assertEqual(self.transport.posts(), [])

    def test_readonly_source_failure_before_send_is_failed_not_unknown(self):
        self.transport.source_error = AmbiguousError("synthetic read timeout")
        result = self.send()
        self.assertEqual(result["status"], "failed")
        self.assertIn("reply_source_preflight_failed", result["error"])
        self.assertEqual(self.transport.posts(), [])

    def test_wrong_reply_parent_root_chat_or_missing_thread_stays_pending(self):
        self.transport.relation = {"parent_id": "om_source", "root_id": "om_source", "thread_id": "omt_expected"}
        cases = [{"parent_id": "om_wrong"}, {"root_id": "om_wrong"}, {"chat_id": "oc_wrong"},
                 {"thread_id": None}, {"thread_id": "omt_wrong"}, {"sender": {"id": "cli_other", "id_type": "app_id", "sender_type": "app"}}]
        for index, changes in enumerate(cases):
            self.source.write_text("Synthetic relation case " + str(index))
            self.transport.message_changes = changes
            result = self.send("bad-get-" + str(index), thread=True)
            self.assertEqual(result["status"], "verification_pending")
            self.assertIsNotNone(result["message_id"])
            before = len(self.transport.posts())
            self.transport.message_changes = {}
            self.assertEqual(self.inspect(result)["status"], "completed")
            self.assertEqual(len(self.transport.posts()), before)

    def test_bad_ack_relationship_never_claims_delivery_even_if_get_matches(self):
        self.transport.relation = {"parent_id": "om_source", "root_id": "om_source", "thread_id": "omt_expected"}
        for index, change in enumerate(({"parent_id": "om_wrong"}, {"root_id": None}, {"chat_id": "oc_wrong"}, {"thread_id": None})):
            self.source.write_text("Synthetic ack case " + str(index)); self.transport.ack_changes = change
            result = self.send("bad-ack-" + str(index), thread=True)
            self.assertEqual(result["status"], "verification_pending")
            self.assertFalse(result["checks"]["reply_acknowledgement"])
            self.assertEqual(self.inspect(result)["status"], "verification_pending")
        self.assertEqual(len(self.transport.posts()), 4)

    def test_unknown_reply_never_falls_back_or_replays_even_with_definite_error_code(self):
        for index, error in enumerate((APIError("230071 synthetic refusal"), AmbiguousError("lost response"))):
            self.source.write_text("Synthetic unknown case " + str(index)); self.transport.send_error = error
            result = self.send("unknown-" + str(index), thread=True)
            self.assertEqual(result["status"], "outcome_unknown")
            count = len(self.transport.calls)
            self.assertTrue(self.send("unknown-" + str(index), thread=True)["no_op"])
            self.assertEqual(self.inspect(result)["status"], "outcome_unknown")
            self.assertEqual(len(self.transport.calls), count)
            with self.assertRaises(messages.MessageError):self.send("bypass-" + str(index), thread=True)
        self.assertEqual(len(self.transport.posts()), 2)
        self.assertTrue(all(urlsplit(c[1]).path.endswith("/reply") for c in self.transport.posts()))

    def test_missing_acknowledged_message_id_is_unknown_and_not_resubmitted(self):
        self.transport.ack_changes = {'message_id': None}
        result = self.send()
        self.assertEqual(result['status'], 'outcome_unknown');self.assertIsNone(result['message_id'])
        self.assertTrue(self.send()['no_op'])
        self.assertEqual(self.inspect(result)['status'], 'outcome_unknown')
        self.assertEqual(len(self.transport.posts()), 1)

    def test_false_forced_thread_requires_exact_root_parent_and_same_readback_thread(self):
        self.transport.relation = {'parent_id': 'om_source', 'root_id': 'om_source', 'thread_id': 'omt_forced'}
        self.transport.message_changes = {'thread_id': 'omt_different'}
        result = self.send()
        self.assertEqual(result['status'], 'verification_pending')
        self.transport.message_changes = {}
        result = self.inspect(result)
        self.assertEqual(result['status'], 'completed');self.assertFalse(result['placement_matches_request'])
        self.assertEqual(result['observed_placement'], 'thread')
        self.assertEqual(len(self.transport.posts()), 1)

    def test_request_cannot_change_source_mode_or_direct_send(self):
        result = self.send()
        for source, thread in (("om_other", False), ("om_source", True), (None, False)):
            with self.assertRaises(messages.MessageError):self.send(source=source, thread=thread)
        self.assertEqual(self.send()["uuid"], result["uuid"])
        self.assertEqual(len(self.transport.posts()), 1)

    def test_dryrun_validation_bot_only_and_cli_binding(self):
        preview = messages.send(self.store, self.settings, "dry", "chat_id", "oc_fixture", self.source,
            reply_to_message_id="om_source", reply_in_thread=True, transport=self.transport)
        self.assertEqual(preview["status"], "dry_run"); self.assertEqual(self.transport.calls, [])
        args = messages.parser().parse_args(["send", "--request-id", "dry", "--receive-id-type", "chat_id",
            "--receive-id", "oc_fixture", "--text-file", str(self.source), "--reply-to", "om_source", "--reply-in-thread"])
        self.assertEqual(args.reply_to_message_id, "om_source"); self.assertTrue(args.reply_in_thread)
        for source, thread in ((None, True), ("bad", False), ("om_source", 1)):
            with self.assertRaises(messages.MessageError):self.send(source=source, thread=thread)
        user_settings = dict(self.settings, transport="lark_cli", message_user_open_id="ou_owner")
        with self.assertRaisesRegex(messages.MessageError, "requires bot"):
            messages.send(self.store, user_settings, "user-reply", "chat_id", "oc_fixture", self.source,
                identity="user", reply_to_message_id="om_source")
        self.assertEqual(self.transport.calls, [])

    def test_official_cli_uses_reply_path_and_stdin_without_send_query_or_receiver(self):
        calls = []
        settings = dict(self.settings, transport="lark_cli", expected_app_id="cli_fixture",
                        cli_argv=["synthetic-lark"], cli_config_dir=str(self.root))
        def process(argv, stdin, **kwargs):
            calls.append((argv, stdin))
            self.assertFalse(kwargs['inherit_env'])
            if argv[1:] == ["config", "show"]:
                return ProcessResult(dumps({"appId": "cli_fixture", "brand": "feishu"}))
            self.assertEqual(argv[1], 'api')
            self.assertEqual(argv[argv.index('--as') + 1], 'bot')
            method, path = argv[2:4]
            if path == '/open-apis/bot/v3/info':
                data = {'open_id': 'ou_synthetic_bot'}
            elif path == '/open-apis/im/v1/messages/om_source':
                self.assertEqual(method, 'GET')
                data = {'items': [{'message_id': 'om_source', 'chat_id': 'oc_fixture'}]}
            elif path == '/open-apis/im/v1/messages/om_source/reply':
                self.assertEqual(method, 'POST');self.assertNotIn('--params', argv)
                self.assertEqual(argv[-2:], ['--data', '-'])
                body = json.loads(stdin)
                self.assertEqual(set(body), {'content', 'msg_type', 'uuid', 'reply_in_thread'})
                self.assertIs(body['reply_in_thread'], False)
                self.assertEqual(json.loads(body['content']), {'text': self.text})
                data = {'message_id': 'om_reply', 'chat_id': 'oc_fixture', 'parent_id': 'om_source', 'root_id': 'om_source'}
            else:
                self.assertEqual((method, path), ('GET', '/open-apis/im/v1/messages/om_reply'))
                data = {'items': [{'message_id': 'om_reply', 'chat_id': 'oc_fixture', 'parent_id': 'om_source', 'root_id': 'om_source',
                    'msg_type': 'text', 'body': {'content': dumps({'text': self.text})},
                    'sender': {'id': 'cli_fixture', 'id_type': 'app_id', 'sender_type': 'app'}}]}
            return ProcessResult(dumps({'ok': True, 'identity': 'bot', 'data': data}))
        with patch('work_assistant.lark_cli.time.sleep'):
            result = messages.send(self.store, settings, 'cli-reply', 'chat_id', 'oc_fixture', self.source,
                confirm=True, process=process, reply_to_message_id='om_source')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(sum(argv[1:3] == ['api', 'POST'] for argv, _ in calls), 1)
