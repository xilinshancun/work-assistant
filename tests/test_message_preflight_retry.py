"""Synthetic recovery before POST; never use live accounts."""
import copy
import unittest
import test_messages
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
from work_assistant import messages
from work_assistant.feishu import APIError, AmbiguousError
from work_assistant.store import Busy
from work_assistant.util import digest


class PreflightRetryTests(unittest.TestCase):
    setUp = test_messages.MessageTests.setUp
    send = test_messages.MessageTests.send

    def failed(self):
        self.transport.auth_error = APIError('synthetic unavailable')
        r = self.send()
        self.transport.auth_error = None
        self.transport.calls.clear()
        return r

    def retry(self, **kw):
        return self.send(retry_preflight=True, reason='Operator verified original never sent.', **kw)

    def test_retry_auth_failure_preserves_identity_and_audit_and_posts_once(self):
        r = self.failed(); prior = copy.deepcopy(self.store.get(r['action_id'])['data'])
        self.assertEqual(self.send()['status'], 'failed')
        self.assertFalse(self.transport.calls)
        self.assertEqual(self.retry(confirm=False)['status'], 'dry_run')
        self.assertFalse(self.store.records('message_preflight_retry'))
        out = self.retry()
        self.assertEqual(out['status'], 'completed')
        self.assertEqual(out['action_id'], r['action_id'])
        self.assertEqual(out['uuid'], prior['uuid'])
        self.assertEqual(len(self.transport.posts()), 1)
        self.assertEqual(self.store.records('message_preflight_retry')[0]['data']['prior_state'], prior)
        self.assertTrue(self.retry()['no_op'])
        self.assertEqual(len(self.transport.posts()), 1)

    def test_retry_rejects_non_preflight_states_and_ack(self):
        r = self.failed();prior = copy.deepcopy(self.store.get(r['action_id'])['data'])
        for change in ({'status':s} for s in ['outcome_unknown','preparing','sending','verification_pending','resolved_absent']):
            with self.subTest(change=change):
                self.store.update_record(r['action_id'],dict(prior,**change))
                with self.assertRaises(messages.MessageError): self.retry()
        for change in [{'send_acknowledged':True},{'message_id':'om_fixture'},{'expected_sender_app_id':'cli_fixture'}, {'error':'other'}, {'pending':{'op':'send'}}, {'resolution':{'status':'absent'}}, {'operation':'update_card'}, {'reply_to_message_id':'om_fixture'},{'uuid':'changed'}]:
            self.store.update_record(r['action_id'],dict(prior,**change))
            with self.assertRaises(messages.MessageError): self.retry()
        self.assertFalse(self.transport.calls)
        self.assertFalse(self.store.records('message_preflight_retry'))

    def test_retry_payload_target_identity_and_source_tampering_rejected(self):
        r=self.failed()
        for kw in [{'receive':'oc_other'}, {'identity':'user'}]:
            with self.assertRaises(messages.MessageError): self.retry(**kw)
        raw=self.source.read_bytes();self.source.write_text('changed')
        with self.assertRaises(messages.MessageError):self.retry()
        self.source.write_bytes(raw)
        artifact=Path(self.store.get(r['action_id'])['data']['source_artifact'])
        artifact.write_text('corrupted')
        with self.assertRaises(messages.MessageError):self.retry()
        self.assertFalse(self.transport.calls)

    def test_retry_requires_flag_reason_and_existing_action(self):
        with self.assertRaises(messages.MessageError):self.retry()
        self.failed()
        with self.assertRaises(messages.MessageError):self.send(retry_preflight=True)
        with self.assertRaises(messages.MessageError):self.send(reason='not explicit')
        self.assertFalse(self.transport.calls)

    def test_retry_both_locks_exclude_concurrent_reentry(self):
        self.failed()
        for resource in ['message:'+digest('session-fixture/request-1'), 'message-payload:'+digest([digest(['chat_id','oc_fixture']),digest(self.source.read_bytes())])]:
            with self.store.inspection_lock([resource]),self.assertRaises(Busy):self.retry()
        self.assertFalse(self.transport.calls)

    def test_retry_post_error_stays_unknown_cannot_retry(self):
        self.failed();self.transport.send_error=AmbiguousError('synthetic uncertainty')
        r=self.retry();self.assertEqual(r['status'],'outcome_unknown')
        with self.assertRaises(messages.MessageError):self.retry()
        self.assertEqual(len(self.transport.posts()),1)

    def test_retry_rereads_under_lock(self):
        r=self.failed();lock=self.store.inspection_lock
        @contextmanager
        def changed(resources):
            with lock(resources):
                d=self.store.get(r['action_id'])['data'];self.store.update_record(r['action_id'],dict(d,status='sending'))
                yield
        with patch.object(self.store,'inspection_lock',changed),self.assertRaises(messages.MessageError):self.retry()
        self.assertFalse(self.transport.calls)
