"""Synthetic in-place card updates. No real accounts, network, or credentials."""
import copy
import io
import json
import time
from contextlib import redirect_stdout
from unittest.mock import patch
import test_messages
from test_message_cards import CARD
from work_assistant import messages
from work_assistant.feishu import APIError, AmbiguousError
from work_assistant.message_cards import card_projection
from work_assistant.util import dumps, digest
from work_assistant.store import Busy
import unittest


class UpdateCardTests(unittest.TestCase):
    setUp = test_messages.MessageTests.setUp

    def prepare(self):
        self.remote = copy.deepcopy(CARD)
        if self.store.records(messages.KIND):
            self.remote["header"]["title"]["content"] += str(len(self.store.records(messages.KIND)))
        self.calls, self.failure, self.after_effect, self.get_changes = [], None, True, {}
        self.source = self.root / "original-card.json"
        self.source.write_text(dumps(self.remote))
        self.send_request = "send-fixture-" + str(len(self.store.records(messages.KIND)))
        self.original = messages.send(self.store, self.settings, self.send_request, "chat_id", "oc_fixture", None,
                                      confirm=True, card_file=self.source, transport=self.http)
        self.assertEqual(self.original["status"], "completed")
        self.changed = copy.deepcopy(CARD)
        self.changed["header"]["title"]["content"] = "Synthetic current daily report"
        self.card_file = self.root / "updated-card.json"
        self.card_file.write_text(dumps(self.changed))

    def http(self, method, url, body=None, headers=None):
        self.calls.append((method, url, copy.deepcopy(body)))
        if method == "POST" and "/im/v1/messages?" in url:
            self.remote = json.loads(body["content"])
            return {"code": 0, "data": {"message_id": "om_fixture", "chat_id": "oc_fixture"}}
        if method == "PATCH":
            action = self.store.records(messages.KIND)[0]["data"]
            assert action["status"] == "sending" and action["pending"]["method"] == "PATCH"
            assert self.store.get(action["update_parent"])["data"]["status"] == "superseded"
            if self.after_effect:
                self.remote = json.loads(body["content"])
            if self.failure:
                raise self.failure
            return {"code": 0, "data": {}}
        if method == "GET" and "/im/v1/messages/" in url:
            message = {"message_id": "om_fixture", "chat_id": "oc_fixture", "msg_type": "interactive", "create_time": str(int(time.time()*1000)),
                       "sender": {"id": "cli_fixture", "id_type": "app_id", "sender_type": "app"},
                       "body": {"content": dumps(card_projection(self.remote))}}
            message.update(self.get_changes)
            return {"code": 0, "data": {"items": [message]}}
        return self.transport(method, url, body, headers)

    def update(self, request="update-fixture", action=None, confirm=True):
        return messages.update_card(self.store, self.settings, action or self.original["action_id"], request,
                                    self.card_file, confirm=confirm, transport=self.http)

    def test_update_patch_only_exact_content_preflight_and_readback_supersedes_original(self):
        self.prepare()
        original = copy.deepcopy(self.store.get(self.original["action_id"])["data"])
        before = len(self.calls)
        result = self.update()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["message_id"], self.original["message_id"])
        self.assertEqual([c[0] for c in self.calls[before:] if "/im/v1/messages" in c[1]], ["GET", "PATCH", "GET"])
        self.assertEqual(self.remote, self.changed)
        self.assertIsNone(result["pending"])
        old = self.store.get(self.original["action_id"])["data"]
        self.assertEqual(old["superseded_by"], result["action_id"])
        self.assertEqual(old["verification_status"], "superseded")
        for key in ("message_id", "uuid", "text_sha256", "source_artifact", "chat_id"):
            self.assertEqual(old[key], original[key])
        audit = self.store.records("message_supersession")[0]["data"]
        self.assertEqual(audit["prior_state"]["text_sha256"], original["text_sha256"])
        self.assertEqual(self.store.records("message_update_intent")[0]["data"]["pending"]["method"], "PATCH")
        before = len(self.calls)
        self.assertTrue(self.update()["no_op"])
        old_receipt = messages.send(self.store, self.settings, self.send_request, "chat_id", "oc_fixture", None,
                                    confirm=True, card_file=self.source, transport=self.http)
        self.assertEqual(old_receipt["status"], "superseded")
        self.assertEqual(messages.inspect(self.store, self.settings, self.original["action_id"], transport=self.http)["status"], "superseded")
        self.assertEqual(len(self.calls), before)
        self.card_file.write_text(dumps(dict(self.changed, header=copy.deepcopy(CARD["header"]))))
        with self.assertRaises(messages.MessageError): self.update()

    def test_update_dry_run_no_client_or_state_change(self):
        self.prepare()
        before = self.store.records()
        with patch.object(messages, "make_client", side_effect=AssertionError("dry-run")):
            result = self.update(confirm=False)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.store.records(), before)

    def test_new_and_original_update_multi_must_be_explicitly_true_without_parent_mutation(self):
        for which in ('new', 'original'):
            for value in (None, False):
                self.prepare()
                parent = copy.deepcopy(self.store.get(self.original['action_id']))
                if which == 'new':
                    card = copy.deepcopy(self.changed)
                    if value is None: card['config'].pop('update_multi')
                    else: card['config']['update_multi'] = value
                    self.card_file.write_text(dumps(card))
                    with self.assertRaises(messages.MessageError): self.update(request='bad-new-'+str(value))
                else:
                    # Create a legitimate independently sent source payload with no update grant.
                    card = copy.deepcopy(CARD)
                    card['header']['title']['content'] += str(len(self.store.records()))
                    if value is None: card['config'].pop('update_multi')
                    else: card['config']['update_multi'] = value
                    self.source.write_text(dumps(card))
                    self.original = messages.send(self.store,self.settings,'no-update-'+str(value),'chat_id','oc_fixture',None,
                                                  confirm=True,card_file=self.source,transport=self.http)
                    parent = copy.deepcopy(self.store.get(self.original['action_id']))
                    result = self.update(request='bad-original-'+str(value))
                    self.assertEqual(result['status'], 'failed')
                    self.assertIn('original private card', result['error'])
                self.assertEqual(self.store.get(self.original['action_id']), parent)
                self.assertFalse(any(c[0] == 'PATCH' for c in self.calls))
        self.assertEqual(self.store.records('message_supersession'), [])

    def test_old_missing_future_or_malformed_create_time_rejected_without_parent_changes(self):
        self.prepare()
        parent = copy.deepcopy(self.store.get(self.original['action_id']))
        for index, created in enumerate((str(int((time.time()-15*86400)*1000)), None, 'invalid', str(int((time.time()+86400)*1000)))):
            self.get_changes = {'create_time': created}
            result = self.update(request='bad-age-'+str(index))
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(self.store.get(self.original['action_id']), parent)
        self.assertFalse(any(c[0] == 'PATCH' for c in self.calls))
        self.assertEqual(self.store.records('message_supersession'), [])

    def test_preflight_rejects_changed_content_sender_chat_type_or_deleted_without_patch(self):
        for index, changes in enumerate(({"chat_id": "oc_other"}, {"sender": None}, {"msg_type": "text"},
                                        {"deleted": True}, {"body": {"content": dumps({"title": "user edit"})}})):
            self.prepare()
            self.get_changes = changes
            before = len(self.calls)
            result = self.update(request="changed-fixture-%s" % index)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["verification_status"], "not_updated")
            self.assertFalse(any(c[0] == "PATCH" for c in self.calls[before:]))
            self.assertFalse(self.store.get(self.original["action_id"])["data"].get("superseded_by"))

    def test_wrong_app_and_tampered_source_fail_before_update(self):
        self.prepare()
        with patch.dict("os.environ", {"FEISHU_APP_ID": "cli_other"}):
            self.assertEqual(self.update()["status"], "failed")
        self.assertFalse(any(c[0] == "PATCH" for c in self.calls))
        original = self.store.get(self.original["action_id"])["data"]
        from pathlib import Path
        Path(original["source_artifact"]).write_text(dumps(self.changed))
        result = self.update(request="tamper-fixture")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(any(c[0] == "PATCH" for c in self.calls))

    def test_unknown_patch_never_repeats_or_allows_old_parent_bypass_and_inspect_only_gets(self):
        self.prepare()
        self.failure = APIError("no structured absence guarantee")
        result = self.update()
        self.assertEqual(result["status"], "outcome_unknown")
        before = len(self.calls)
        self.assertEqual(self.update()["status"], "outcome_unknown")
        with self.assertRaises(messages.MessageError): self.update(request="bypass-new-id")
        self.assertEqual(len(self.calls), before)
        self.failure = None
        inspected = messages.inspect(self.store, self.settings, result["action_id"], transport=self.http)
        self.assertEqual(inspected["status"], "completed")
        self.assertIsNone(inspected["pending"])
        self.assertTrue(all(c[0] == "GET" or "/auth/" in c[1] for c in self.calls[before:]))

    def test_unknown_absent_patch_stays_pending_without_auto_retry_or_absence_resolution(self):
        self.prepare()
        self.after_effect, self.failure = False, AmbiguousError("synthetic lost request")
        result = self.update()
        before = len(self.calls)
        inspected = messages.inspect(self.store, self.settings, result["action_id"], transport=self.http)
        self.assertEqual(inspected["status"], "verification_pending")
        self.assertIsNotNone(inspected["pending"])
        self.assertEqual(self.update()["status"], "verification_pending")
        with self.assertRaises(messages.MessageError):
            messages.resolve(self.store, result["action_id"], absent=True, reason="update absent", confirm=True)
        self.assertFalse(any(c[0] == "PATCH" for c in self.calls[before:]))

    def test_update_lock_and_cli_binding_and_operation_collision(self):
        self.prepare()
        resource = "message-remote:" + digest(["cli_fixture", "om_fixture"])
        with self.store.inspection_lock([resource]), self.assertRaises(Busy): self.update()
        with self.assertRaises(messages.MessageError): self.update(request=self.send_request)
        conf = self.root / "update-config.json"
        conf.write_text(dumps({"state_dir": str(self.store.root)}))
        args = ["--config", str(conf), "update-card", self.original["action_id"], "--request-id", "cli-update", "--card-file", str(self.card_file)]
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(messages.main(args), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "dry_run")
        from work_assistant.feishu import make_client
        with patch.object(messages, "make_client", return_value=make_client(self.settings, transport=self.http)), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(messages.main(args + ["--confirm"]), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "completed")

    def test_next_update_requires_current_verified_action_in_same_message_chain(self):
        self.prepare()
        first = self.update()
        with self.assertRaises(messages.MessageError): self.update(request="next-update")
        self.changed["header"]["title"]["content"] = "Synthetic second revision"
        self.card_file.write_text(dumps(self.changed))
        second = self.update(request="next-update", action=first["action_id"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["message_id"], self.original["message_id"])
        self.assertEqual(sum(c[0] == "POST" and "/im/v1/messages?" in c[1] for c in self.calls), 1)
