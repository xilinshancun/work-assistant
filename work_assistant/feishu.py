"""Feishu native card/docx publication with durable per-request checkpoints."""
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from .reports import load_report
from .util import dumps, digest, redact

BASE = "https://open.feishu.cn/open-apis"


class APIError(ValueError):
    pass


class AmbiguousError(ValueError):
    pass


def http(method, url, body=None, headers=None, timeout=20):
    data = dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=dict({"Content-Type": "application/json; charset=utf-8"}, **(headers or {})))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise AmbiguousError("oversized response; inspect remote state")
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise AmbiguousError("invalid response after request; inspect remote state")
    except urllib.error.HTTPError as exc:
        # A 5xx response may occur after upstream processing.
        if exc.code >= 500:
            raise AmbiguousError("Feishu HTTP server error %s" % exc.code)
        raise APIError("Feishu HTTP error %s" % exc.code)
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
        raise AmbiguousError("network outcome unknown; no automatic retry")


class Feishu:
    def __init__(self, settings, transport=http, base=BASE):
        self.settings, self.transport, self.base = settings, transport, base.rstrip("/")
        self.token = None
        self.last_request = 0.0

    def env(self, key, required=True):
        reference = self.settings.get(key)
        value = os.environ.get(reference, "") if reference else ""
        if required and not value:
            raise APIError("Feishu not configured: set environment variable referenced by " + key)
        return value

    def destination(self):
        mode = self.settings["mode"]
        if mode not in {"app", "webhook"}:
            raise APIError("publisher mode must be app or webhook")
        if mode == "webhook":
            url = self.env("webhook_env")
            if not url.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/"):
                raise APIError("webhook must be a Feishu HTTPS webhook")
            return "webhook:" + digest(url)
        kind = self.settings["receive_id_type"]
        if kind not in {"chat_id", "open_id", "user_id", "union_id", "email"}:
            raise APIError("invalid receive_id_type")
        return "app:" + digest([self.env("app_id_env"), kind, self.env("receive_id_env"), self.env("folder_token_env", False)])

    def request(self, method, path, body=None):
        if not self.token:
            response = self.transport("POST", self.base + "/auth/v3/tenant_access_token/internal",
                {"app_id": self.env("app_id_env"), "app_secret": self.env("app_secret_env")}, {})
            self.check(response)
            self.token = response.get("tenant_access_token")
            if not self.token:
                raise APIError("tenant token missing")
        if self.transport is http and self.base == BASE:
            # Keep a single CLI process below common docx per-app limits. No automatic mutation retry.
            time.sleep(max(0, 0.4 - (time.monotonic() - self.last_request)))
        result = self.transport(method, self.base + path, body, {"Authorization": "Bearer " + self.token})
        self.last_request = time.monotonic()
        self.check(result)
        return result.get("data", {})

    @staticmethod
    def check(result):
        if type(result) is not dict or type(result.get("code")) is not int or result["code"] != 0:
            # Avoid echoing API text, which can contain destination identifiers/credentials.
            raise APIError("Feishu application error code: " + str(result.get("code") if isinstance(result, dict) else "invalid response"))


def preview(store, report_id):
    report = load_report(store, report_id)
    root = Path(report["data"]["path"])
    card = json.loads((root / "card.json").read_text(encoding="utf-8"))
    document = json.loads((root / "document.json").read_text(encoding="utf-8"))
    if (type(card.get("elements")) is not list or not card["elements"] or
            type(document.get("children")) is not list or not document["children"]):
        raise ValueError("invalid card/document payload")
    return {"status": "dry_run", "report": report_id, "hash": report["data"]["hash"], "path": str(root),
            "card": card, "document": document, "note": "local preview only; webhook supports cards only; no network requests"}


def publish(store, settings, report_id, confirm=False, transport=http, base=BASE, retry=False):
    payload = preview(store, report_id)
    client = Feishu(settings, transport, base)
    destination = client.destination()
    grants = settings.get("grants", [])
    if not confirm and (type(grants) is not list or any(type(g) is not str for g in grants) or destination not in grants):
        raise ValueError("live publish requires --confirm or an exact configured destination grant")
    report = store.get(report_id)
    identity = digest([destination, report["project_id"], report["data"]["period"]])
    with store.claim(["publish:" + identity]):
        prior = next((r for r in store.records("publication") if r["data"]["identity"] == identity), None)
        data = dict(prior["data"]) if prior else {"identity": identity, "destination": destination,
            "document_id": None, "block_ids": [], "message_id": None, "mode": settings["mode"], "steps": []}
        if data.get("status") in {"outcome_unknown", "publishing"}:
            raise ValueError("publication outcome unknown; reconcile before explicit retry")
        if data.get("status") == "completed" and data.get("hash") == payload["hash"]:
            return dict(data, no_op=True)
        if data.get("status") in {"failed", "retry_authorized", "cancelled"} and not retry:
            raise ValueError("previous attempt requires explicit --retry after inspection")
        if prior and data["mode"] == "webhook" and data.get("hash") != payload["hash"] and data.get("status") == "completed":
            raise ValueError("webhook cannot update prior cards; use app mode for period updates")
        data.update(status="publishing", hash=payload["hash"], report_id=report_id, pending=None)
        publication = store.record("publication", data, project_id=report["project_id"])

        def checkpoint():
            store.update_record(publication["id"], data)

        def effect(stage, method, path, body):
            data["pending"] = {"stage": stage, "method": method, "path": path, "body_hash": digest(body)}
            checkpoint()  # Write-ahead intent before *each* potential side effect.
            result = client.request(method, path, body)
            data["steps"] = data["steps"] + [{"stage": stage, "body_hash": digest(body)}]
            data["pending"] = None
            return result

        try:
            if settings["mode"] == "webhook":
                data["pending"] = {"stage": "webhook_card", "body_hash": digest(payload["card"])}
                checkpoint()
                response = transport("POST", client.env("webhook_env"), {"msg_type": "interactive", "card": payload["card"]}, {})
                # Webhook responds with code=0; no message_id/update API guarantee.
                client.check(response)
                data["pending"] = None
                data["note"] = "webhook card only; document retained locally; remote ID unavailable"
            else:
                if not data["document_id"]:
                    body = {"title": payload["document"]["title"]}
                    folder = client.env("folder_token_env", False)
                    if folder:
                        body["folder_token"] = folder
                    result = effect("create_document", "POST", "/docx/v1/documents", body)
                    data["document_id"] = result.get("document", {}).get("document_id")
                    if not data["document_id"]:
                        raise AmbiguousError("create document response missing ID")
                    checkpoint()
                doc = urllib.parse.quote(data["document_id"], safe="")
                children = payload["document"]["children"]
                for index, block in enumerate(children):
                    if index < len(data["block_ids"]):
                        block_id = urllib.parse.quote(data["block_ids"][index], safe="")
                        effect("update_block:%s" % index, "PATCH", "/docx/v1/documents/%s/blocks/%s" % (doc, block_id),
                               {"update_text_elements": {"elements": block["text"]["elements"]}})
                    else:
                        result = effect("create_block:%s" % index, "POST", "/docx/v1/documents/%s/blocks/%s/children" % (doc, doc),
                                        {"children": [block], "index": index})
                        created = result.get("children", [])
                        if not created or not created[0].get("block_id"):
                            raise AmbiguousError("created block response missing ID")
                        data["block_ids"] = data["block_ids"] + [created[0]["block_id"]]
                    checkpoint()
                if len(data["block_ids"]) > len(children):
                    effect("delete_obsolete_blocks", "DELETE", "/docx/v1/documents/%s/blocks/%s/children/batch_delete" % (doc, doc),
                           {"start_index": len(children), "end_index": len(data["block_ids"])})
                    data["block_ids"] = data["block_ids"][:len(children)]
                    checkpoint()
                if data["message_id"]:
                    effect("update_card", "PATCH", "/im/v1/messages/" + urllib.parse.quote(data["message_id"], safe=""),
                           {"content": dumps(payload["card"])})
                else:
                    # Feishu documents uuid deduplication for send-message, not for doc creation.
                    message_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, identity + payload["hash"]))
                    result = effect("create_card", "POST", "/im/v1/messages?receive_id_type=" + settings["receive_id_type"],
                                    {"receive_id": client.env("receive_id_env"), "msg_type": "interactive",
                                     "content": dumps(payload["card"]), "uuid": message_uuid})
                    data["message_id"] = result.get("message_id")
                    if not data["message_id"]:
                        raise AmbiguousError("send card response missing ID")
                    checkpoint()
            data.update(status="completed", pending=None)
            checkpoint()
        except APIError as exc:
            data.update(status="failed", error=redact(str(exc)))
            checkpoint()
        except BaseException as exc:
            data.update(status="outcome_unknown", error="request interrupted or response ambiguous; inspect before retry")
            checkpoint()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        return dict(data, publication_id=publication["id"])


def reconcile(store, settings, ident, resolution=None, reason=None, remote_ids=None, transport=http, base=BASE):
    record = store.get(ident)
    if record["kind"] != "publication":
        raise ValueError("expected publication ID")
    data = dict(record["data"])
    resource = "publish:" + data["identity"]
    with store.inspection_lock([resource]):
        if data["status"] == "publishing":
            data.update(status="outcome_unknown", error="interrupted publisher; no live process lock")
            with store.transaction():
                store.update_record(ident, data)
                store.record("event", {"action": "publication_interruption_observed", "publication": ident}, project_id=record["project_id"])
        client = Feishu(settings, transport, base)
        if (data.get("document_id") or data.get("message_id")) and client.destination() != data["destination"]:
            raise ValueError("reconcile destination differs from original publication")
        inspected = {}
        if data.get("document_id"):
            doc = urllib.parse.quote(data["document_id"], safe="")
            inspected["document"] = client.request("GET", "/docx/v1/documents/" + doc)
            inspected["content"] = client.request("GET", "/docx/v1/documents/" + doc + "/raw_content")
        if data.get("message_id"):
            inspected["message"] = client.request("GET", "/im/v1/messages/" + urllib.parse.quote(data["message_id"], safe=""))
        if resolution:
            if resolution not in {"completed", "retry_authorized", "cancelled"} or not reason or not reason.strip():
                raise ValueError("explicit resolution requires a valid status and reason")
            if remote_ids:
                if type(remote_ids) is not dict or set(remote_ids) - {"document_id", "message_id", "block_ids"}:
                    raise ValueError("invalid remote ID mapping")
                for key in ("document_id", "message_id"):
                    if key in remote_ids and type(remote_ids[key]) is not str:
                        raise ValueError("remote IDs must be strings")
                if "block_ids" in remote_ids and (type(remote_ids["block_ids"]) is not list or any(type(i) is not str for i in remote_ids["block_ids"])):
                    raise ValueError("block_ids must be string list")
                data.update(remote_ids)
            data.update(status=resolution, resolution_reason=reason, pending=None)
            with store.transaction():
                store.update_record(ident, data)
                store.db.execute("DELETE FROM locks WHERE resource=?", (resource,))
                store.record("event", {"action": "publication_resolution", "publication": ident, "resolution": resolution,
                                      "reason": reason, "remote_ids": remote_ids}, project_id=record["project_id"])
        return {"publication": data, "inspection": inspected, "note": "GET inspection only; no agent resumed or publication resent"}
