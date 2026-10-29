import copy
import json
import unittest
import test_messages
from unittest.mock import patch
from work_assistant import messages
from work_assistant.message_cards import card_projection, matches_card_projection
from work_assistant.feishu import AmbiguousError


CARD = {"config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "Synthetic preview"}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "**Unverified example.**"}},
                     {"tag": "hr"},
                     {"tag": "div", "text": {"tag": "lark_md", "content": "\n· Next step: inspect."}},
                     {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "Open"},
                         "type": "default", "url": "https://example.com/report"}]},
                     {"tag": "note", "elements": [{"tag": "plain_text", "content": "Synthetic assistant"}]}]}
# A sanitized fixture from the actual message-GET projection contract; URLs/styles are absent.
PROJECTION = {"title": "Synthetic preview", "elements": [[{"tag": "text", "text": "Unverified example."}],
               [{"tag": "hr"}], [{"tag": "text", "text": "· Next step: inspect."}],
               [{"tag": "button", "text": "Open", "type": "default"}],
               [{"tag": "note", "elements": [{"tag": "text", "text": "Synthetic assistant"}]}]]}


class CardTests(unittest.TestCase):
    setUp = test_messages.MessageTests.setUp
    def card_send(self, card=None, request="card-fixture-1", confirm=True):
        source = self.root / "card.json"
        source.write_text(json.dumps(CARD if card is None else card))
        return messages.send(self.store, self.settings, request, "chat_id", "oc_fixture", None,
                             confirm=confirm, card_file=source, transport=self.transport)

    def card_transport(self, error=None, changed=False):
        def transport(method, url, body=None, headers=None):
            if method == "POST" and "/im/v1/messages?" in url:
                self.transport.calls.append((method, url, copy.deepcopy(body)))
                self.assertEqual(self.store.records(messages.KIND)[0]["data"]["status"], "sending")
                self.assertEqual(json.loads(body["content"]), CARD)
                if error:
                    raise error
                return {"code": 0, "data": {"message_id": "om_fixture", "chat_id": "oc_fixture"}}
            if method == "GET" and "/im/v1/messages/" in url:
                return {"code": 0, "data": {"items": [{"message_id": "om_fixture", "chat_id": "oc_fixture", "msg_type": "interactive",
                    "sender": {"id": "cli_fixture", "id_type": "app_id", "sender_type": "app"},
                    "body": {"content": json.dumps(dict(PROJECTION, title="wrong") if changed else PROJECTION)}}]}}
            return self.transport(method, url, body, headers)
        return transport

    def test_card_projection_and_exact_send_ack_noop(self):
        self.assertEqual(card_projection(CARD), PROJECTION)
        transport = self.card_transport()
        with patch.object(messages, "make_client", wraps=messages.make_client):
            source = self.root / "card.json"
            source.write_text(json.dumps(CARD))
            def send():
                return messages.send(self.store, self.settings, "card-1", "chat_id", "oc_fixture", None,
                                     confirm=True, card_file=source, transport=transport)
            result = send()
            self.assertEqual(result["status"], "completed")
            self.assertIn("not independently verified", result["verification_scope"])
            self.assertEqual(result["msg_type"], "interactive")
            self.assertTrue(send()["no_op"])
            self.assertEqual(len(self.transport.posts()), 1)
            with self.assertRaises(messages.MessageError):
                messages.send(self.store, self.settings, "card-1", "chat_id", "oc_fixture", source, confirm=True)

    def test_card_unknown_and_mismatch_never_resend(self):
        source = self.root / "card.json"
        source.write_text(json.dumps(CARD))
        transport = self.card_transport(error=AmbiguousError("lost reply"))
        kwargs = dict(confirm=True, card_file=source, transport=transport)
        result = messages.send(self.store, self.settings, "card-unknown", "chat_id", "oc_fixture", None, **kwargs)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertTrue(messages.send(self.store, self.settings, "card-unknown", "chat_id", "oc_fixture", None, **kwargs)["no_op"])
        self.assertEqual(len(self.transport.posts()), 1)

    def test_card_readback_changed_content_is_pending(self):
        source = self.root / "card.json"; source.write_text(json.dumps(CARD))
        result = messages.send(self.store, self.settings, "card-changed", "chat_id", "oc_fixture", None,
                               confirm=True, card_file=source, transport=self.card_transport(changed=True))
        self.assertEqual(result["status"], "verification_pending")
        self.assertEqual(result["message_id"], "om_fixture")

    def test_inspect_accepts_exact_flattened_note_without_post_and_rejects_content_changes(self):
        card = {"config": {"wide_screen_mode": True},
                "header": {"template": "blue", "title": {"tag": "plain_text", "content": "快速回答"}},
                "elements": [{"tag": "note", "elements": [{"tag": "plain_text", "content": "未查询项目状态 · 未执行操作"}]},
                             {"tag": "hr"}, {"tag": "div", "text": {"tag": "plain_text", "content": "原文 **符号** 😀"}}]}
        flat = {"title": "快速回答", "elements": [[{"tag": "text", "text": "未查询项目状态 · 未执行操作"}],
                    [{"tag": "hr"}], [{"tag": "text", "text": "原文 **符号** 😀"}]]}
        current = dict(flat, title="not yet verified")
        calls = []
        def request(method, path, body=None):
            calls.append((method, path, copy.deepcopy(body)))
            if method == "POST":
                self.assertEqual(json.loads(body["content"]), card)
                return {"message_id": "om_fixture", "chat_id": "oc_fixture"}
            self.assertEqual((method, path), ("GET", "/im/v1/messages/om_fixture"))
            return {"items": [{"message_id": "om_fixture", "chat_id": "oc_fixture", "msg_type": "interactive",
                    "sender": {"id": "cli_fixture", "id_type": "app_id", "sender_type": "app"},
                    "body": {"content": json.dumps(current)}}]}
        client = type("Client", (), {"app_id": lambda _: "cli_fixture", "authenticate": lambda _: None})()
        client.request = request
        source = self.root / "note-card.json"; source.write_text(json.dumps(card))
        with patch.object(messages, "make_client", return_value=client):
            sent = messages.send(self.store, self.settings, "note-shape", "chat_id", "oc_fixture", None,
                                 confirm=True, card_file=source)
            self.assertEqual(sent["status"], "verification_pending")
            current = copy.deepcopy(flat); count = len(calls)
            verified = messages.inspect(self.store, self.settings, sent["action_id"])
            self.assertEqual(verified["status"], "completed"); self.assertTrue(verified["checks"]["content"])
            self.assertEqual(calls[count:], [("GET", "/im/v1/messages/om_fixture", None)])
            self.assertEqual(verified["message_id"], sent["message_id"])
            self.assertEqual(verified["text_sha256"], sent["text_sha256"])
            bad = []
            wrong = copy.deepcopy(flat); wrong["elements"][0][0]["text"] += " wrong"; bad.append(wrong)
            missing = copy.deepcopy(flat); missing["elements"].pop(0); bad.append(missing)
            reordered = copy.deepcopy(flat); reordered["elements"].reverse(); bad.append(reordered)
            extra = copy.deepcopy(flat); extra["elements"][0].append({"tag": "text", "text": "extra"}); bad.append(extra)
            whitespace = copy.deepcopy(flat); whitespace["elements"][0][0]["text"] += " "; bad.append(whitespace)
            wrong_tag = copy.deepcopy(flat); wrong_tag["elements"][0][0]["tag"] = "note"; bad.append(wrong_tag)
            bad.append(dict(flat, extra="ignored field must not be accepted"))
            for current in bad:
                with self.subTest(content=current):
                    count = len(calls)
                    pending = messages.inspect(self.store, self.settings, sent["action_id"])
                    self.assertEqual(pending["status"], "verification_pending")
                    self.assertFalse(pending["checks"]["content"])
                    self.assertEqual(calls[count:], [("GET", "/im/v1/messages/om_fixture", None)])
        self.assertEqual(sum(call[0] == "POST" for call in calls), 1)

    def test_note_normalization_preserves_existing_wrapped_markdown_card_and_all_note_texts(self):
        self.assertTrue(matches_card_projection(CARD, PROJECTION))
        flat = copy.deepcopy(PROJECTION)
        flat["elements"][-1] = flat["elements"][-1][0]["elements"]
        self.assertTrue(matches_card_projection(CARD, flat))
        card = copy.deepcopy(CARD)
        card["elements"][-1]["elements"].append({"tag": "plain_text", "content": "Second note"})
        projection = card_projection(card)
        projection["elements"][-1] = projection["elements"][-1][0]["elements"]
        self.assertTrue(matches_card_projection(card, projection))
        for row in (projection["elements"][-1][::-1], projection["elements"][-1][:1]):
            changed = copy.deepcopy(projection); changed["elements"][-1] = row
            self.assertFalse(matches_card_projection(card, changed))

    def test_card_dryrun_and_unsafe_callback_target_rejected(self):
        result = self.card_send(confirm=False)
        self.assertEqual(result["msg_type"], "interactive")
        self.assertEqual(self.transport.calls, [])
        for value in ["**see http://127.0.0.1/x**", "<at id=all>everyone</at>"]:
            card = copy.deepcopy(CARD); card["elements"][0]["text"]["content"] = value
            with self.assertRaises(messages.MessageError):
                self.card_send(card)
        for url in ["javascript:alert(1)", "http://localhost/x", "https://example.com/?token=abcdefg123456"]:
            card = copy.deepcopy(CARD); card["elements"][3]["actions"][0]["url"] = url
            with self.assertRaises(messages.MessageError):
                self.card_send(card)
        card = copy.deepcopy(CARD); card["elements"][3]["actions"][0]["value"] = {"action": "write"}
        with self.assertRaises(messages.MessageError):
            self.card_send(card)
        self.assertEqual(self.transport.calls, [])
