import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from work_assistant.feishu import http, publish, preview, reconcile, APIError, AmbiguousError, Feishu
from work_assistant.reports import generate_report
from work_assistant.util import dumps
from test_runtime import Base


class FakeServer:
    def __init__(self):
        self.calls, self.blocks = [], []
        self.fail_card = False
        self.delay_card = False
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                count = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(count)) if count else None
                outer.calls.append((self.command, self.path, body))
                data, code = {}, 0
                if self.path.endswith("/tenant_access_token/internal"):
                    envelope = {"code": 0, "tenant_access_token": "synthetic-token", "expire": 7200}
                else:
                    if self.path.endswith("/docx/v1/documents"):
                        data = {"document": {"document_id": "doc-synthetic"}}
                    elif self.path.endswith("/children"):
                        ident = "block-%s" % len(outer.blocks)
                        outer.blocks.append(ident)
                        data = {"children": [{"block_id": ident}]}
                    elif self.path.startswith("/im/v1/messages?"):
                        if outer.fail_card:
                            code = 99991663
                            outer.fail_card = False
                        if outer.delay_card:
                            outer.delay_card = False
                            time.sleep(0.15)
                        data = {"message_id": "message-synthetic"}
                    elif self.command == "GET":
                        data = {"content": "synthetic remote observation"}
                    envelope = {"code": code, "data": data}
                raw = dumps(envelope).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = do_PATCH = do_DELETE = handle_request
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%s" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def count(self, method, suffix):
        return sum(1 for m, p, _ in self.calls if m == method and p.endswith(suffix))


class FeishuTests(Base):
    def setUp(self):
        super().setUp()
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        self.environment = patch.dict(os.environ, {"FEISHU_APP_ID": "synthetic-app", "FEISHU_APP_SECRET": "synthetic-secret",
                                                  "FEISHU_RECEIVE_ID": "synthetic-chat"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = self.conf["publisher"]
        self.task()
        self.report = generate_report(self.s, self.p["id"], "2026-01-15")

    def send(self, **kwargs):
        return publish(self.s, self.settings, self.report["id"], confirm=True, base=self.server.base, **kwargs)

    def test_preview_requires_no_credentials_or_requests(self):
        with patch.dict(os.environ, {}, clear=True):
            payload = preview(self.s, self.report["id"])
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(self.server.calls, [])

    def test_explicit_authority_create_update_and_noop(self):
        with self.assertRaises(ValueError):
            publish(self.s, self.settings, self.report["id"], base=self.server.base)
        first = self.send()
        self.assertEqual(first["status"], "completed")
        count = len(self.server.calls)
        self.assertTrue(self.send()["no_op"])
        self.assertEqual(count, len(self.server.calls))
        self.task()
        self.report = generate_report(self.s, self.p["id"], "2026-01-15")
        second = self.send()
        self.assertEqual(second["document_id"], first["document_id"])
        self.assertEqual(second["message_id"], first["message_id"])
        self.assertEqual(self.server.count("POST", "/docx/v1/documents"), 1)
        self.assertEqual(self.server.count("PATCH", "/im/v1/messages/message-synthetic"), 1)
        sends = [b for m, p, b in self.server.calls if m == "POST" and p.startswith("/im/v1/messages?")]
        self.assertEqual(len(sends), 1)
        self.assertIn("uuid", sends[0])

    def test_application_error_keeps_document_checkpoint(self):
        self.server.fail_card = True
        failed = self.send()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["document_id"], "doc-synthetic")
        self.assertIsNone(failed["message_id"])
        with self.assertRaises(ValueError):
            self.send()
        done = self.send(retry=True)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.server.count("POST", "/docx/v1/documents"), 1)
        self.assertEqual(len(self.s.records("report")), 1)

    def test_timeout_unknown_inspect_then_operator_resolution(self):
        self.server.delay_card = True
        def short_transport(*args):
            return http(*args, timeout=0.03)
        unknown = self.send(transport=short_transport)
        self.assertEqual(unknown["status"], "outcome_unknown")
        before = len(self.server.calls)
        with self.assertRaises(ValueError):
            self.send(retry=True)
        self.assertEqual(before, len(self.server.calls))
        inspected = reconcile(self.s, self.settings, unknown["publication_id"], base=self.server.base)
        self.assertIn("document", inspected["inspection"])
        self.assertTrue(all(m == "GET" or p.endswith("/tenant_access_token/internal") for m, p, b in self.server.calls[before:]))
        with self.assertRaises(ValueError):
            reconcile(self.s, self.settings, unknown["publication_id"], "completed", "", base=self.server.base)
        resolved = reconcile(self.s, self.settings, unknown["publication_id"], "completed", "operator confirmed card ID in dedicated test destination",
                             {"message_id": "message-synthetic"}, base=self.server.base)
        self.assertEqual(resolved["publication"]["status"], "completed")
        self.assertTrue(self.send()["no_op"])

    def test_webhook_card_only_and_no_update(self):
        self.settings["mode"] = "webhook"
        with patch.dict(os.environ, {"FEISHU_WEBHOOK": "https://open.feishu.cn/open-apis/bot/v2/hook/synthetic"}):
            requests = []
            def transport(*args):
                requests.append(args)
                return {"code": 0}
            first = self.send(transport=transport)
            self.assertEqual(first["status"], "completed")
            self.assertIsNone(first["document_id"])
            self.assertEqual(len(requests), 1)
            self.task()
            self.report = generate_report(self.s, self.p["id"], "2026-01-15")
            with self.assertRaises(ValueError):
                self.send(transport=transport)

    def test_missing_live_configuration_is_honest(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(APIError):
            self.send()
        self.assertEqual(self.server.calls, [])

    def test_stale_publication_lock_requires_resolution_and_is_recoverable(self):
        self.server.fail_card = True
        publication = self.send()
        record = self.s.get(publication["publication_id"])
        data = record["data"]
        data["status"] = "publishing"
        self.s.update_record(record["id"], data)
        resource = "publish:" + data["identity"]
        self.s.db.execute("INSERT INTO locks VALUES(?,?,?)", (resource, "crashed-parent", "synthetic-time"))
        reconcile(self.s, self.settings, record["id"], base=self.server.base)
        self.assertIsNotNone(self.s.db.execute("SELECT 1 FROM locks WHERE resource=?", (resource,)).fetchone())
        reconcile(self.s, self.settings, record["id"], "retry_authorized", "operator inspected destination after crash", base=self.server.base)
        self.assertIsNone(self.s.db.execute("SELECT 1 FROM locks WHERE resource=?", (resource,)).fetchone())
        self.assertEqual(self.send(retry=True)["status"], "completed")
