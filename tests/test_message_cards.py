import copy
import json
import unittest
import test_messages
from unittest.mock import patch
from work_assistant import messages
from work_assistant.message_cards import card_projection
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
               [{"tag": "hr"}], [{"tag": "text", "text": "\n· Next step: inspect."}],
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
