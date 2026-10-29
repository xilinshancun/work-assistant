"""Feishu native card/docx publication with durable per-request checkpoints."""
import json
import os
import re
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
# Official schema: larksuite/oapi-sdk-go/service/docx/v1/model.go (Block,
# TextElementStyle, UpdateBlockRequest, BatchDeleteDocumentBlockChildrenReq).
TEXT_BLOCKS = {2: "text", 12: "bullet", 13: "ordered"}
TEXT_BLOCKS.update({level + 2: "heading%s" % level for level in range(1, 10)})


def block_text(block):
    kind = block.get("block_type") if type(block) is dict else None
    if type(kind) is not int or kind not in TEXT_BLOCKS or type(block.get(TEXT_BLOCKS[kind])) is not dict:
        raise APIError("unsupported native document text block")
    text = block[TEXT_BLOCKS[kind]]
    if type(text.get("elements")) is not list or not text["elements"]:
        raise APIError("native document block needs text elements")
    return text


def block_signature(block):
    """Compare rendered text/styles while accepting server-supplied defaults."""
    text = block_text(block)
    if block.get("children") or block.get("comment_ids"):
        raise APIError("document has nested blocks or comments; refusing replacement")
    runs = []
    for element in text["elements"]:
        if type(element) is not dict or set(element) != {"text_run"} or type(element["text_run"]) is not dict:
            raise APIError("unsupported native inline element; refusing replacement")
        run = element["text_run"]
        if type(run.get("content")) is not str or set(run) - {"content", "text_element_style"}:
            raise APIError("invalid native text run")
        style = dict(run.get("text_element_style") or {})
        for key in ("bold", "italic", "strikethrough", "underline", "inline_code"):
            if key in style and type(style[key]) is not bool:
                raise APIError("native inline style must use booleans")
            if style.get(key) is False:
                del style[key]
        for key in ("background_color", "text_color"):
            if type(style.get(key)) is int and style[key] == 0:
                del style[key]
        if runs and runs[-1]["style"] == style:
            runs[-1]["content"] += run["content"]
        else:
            runs.append({"content": run["content"], "style": style})
    style = dict(text.get("style") or {})
    for key, default in (("align", 1), ("folded", False)):
        if type(style.get(key)) is type(default) and style[key] == default:
            del style[key]
    return {"block_type": block["block_type"], "elements": runs, "style": style}


def document_blocks_snapshot(client, document_id):
    """Read every direct child at a stable revision; never resumes a mutation."""
    doc = urllib.parse.quote(document_id, safe="")
    metadata = client.request("GET", "/docx/v1/documents/" + doc).get("document", {})
    revision = metadata.get("revision_id")
    if type(revision) is not int or revision < 1:
        raise APIError("document revision unavailable; refusing replacement")
    blocks, token, seen = [], None, set()
    for _ in range(100):
        query = {"document_revision_id": "-1", "page_size": "200"}
        if token:
            query["page_token"] = token
        page = client.request("GET", "/docx/v1/documents/%s/blocks/%s/children?" % (doc, doc) + urllib.parse.urlencode(query))
        if type(page.get("items")) is not list or type(page.get("has_more")) is not bool:
            raise APIError("invalid document child pagination")
        blocks += page["items"]
        if len(blocks) > 2000:
            raise APIError("document exceeds replacement inspection bound")
        if not page["has_more"]:
            break
        token = page.get("page_token")
        if type(token) is not str or not token or token in seen:
            raise APIError("invalid document child page token")
        seen.add(token)
    else:
        raise APIError("document pagination incomplete")
    after = client.request("GET", "/docx/v1/documents/" + doc).get("document", {})
    if after.get("revision_id") != revision or after.get("title") != metadata.get("title"):
        raise APIError("document changed during inspection; refusing replacement")
    ids = [b.get("block_id") for b in blocks if type(b) is dict]
    if (len(ids) != len(blocks) or any(type(i) is not str or not i for i in ids) or
            len(set(ids)) != len(ids) or any(b.get("parent_id") != document_id for b in blocks)):
        raise APIError("unexpected or duplicate document child IDs")
    return {"revision": revision, "title": metadata.get("title"), "ids": ids,
            "signatures": [block_signature(b) for b in blocks]}


def migration_observation(store, client, data):
    migration = data["document_migration"]
    source = preview(store, migration["source_report_id"])["document"]
    target = preview(store, migration["target_report_id"])["document"]
    if digest(source) != migration["source_hash"] or digest(target) != migration["target_hash"]:
        raise APIError("migration immutable document hash mismatch")
    actual = document_blocks_snapshot(client, data["document_id"])
    old_ids, new_ids = migration["old_block_ids"], migration["new_block_ids"]
    old = [block_signature(b) for b in source["children"]]
    new = [block_signature(b) for b in target["children"]]
    if actual["title"] != source["title"]:
        raise APIError("document title was edited; refusing replacement")
    ids, signatures = actual["ids"], actual["signatures"]
    if ids == new_ids and len(new_ids) == len(new) and signatures == new:
        return dict(actual, state="complete", new_block_ids=new_ids)
    count = len(ids) - len(old_ids)
    if (0 <= count <= len(new) and ids[:len(old_ids)] == old_ids and
            ids[len(old_ids):len(old_ids) + len(new_ids)] == new_ids and
            len(new_ids) <= count <= len(new_ids) + migration.get("pending_batch_count", 0) and signatures == old + new[:count]):
        return dict(actual, state="appending" if count < len(new) else "ready_delete",
                    new_block_ids=ids[len(old_ids):])
    raise APIError("document IDs/content/styles differ from acknowledged migration; inspect user edits")


def document_settings(settings):
    member_env = settings.get("document_member_env")
    if member_env is not None and (type(member_env) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", member_env)):
        raise ValueError("document_member_env must be an environment variable name")
    if settings.get("document_member_perm", "edit") not in {"edit", "full_access"}:
        raise ValueError("document_member_perm must be edit or full_access")
    if settings.get("mode") == "webhook" and member_env:
        raise ValueError("document sharing requires app mode")


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
        self.settings, self.transport, self.base = dict(settings), transport, base.rstrip("/")
        document_settings(self.settings)
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
        identity = [self.app_id(), kind, self.env("receive_id_env"), self.env("folder_token_env", False)]
        member = self.document_member()
        if member:
            identity += [member, self.settings.get("document_member_perm", "edit")]
        return "app:" + digest(identity)

    def document_member(self):
        if not self.settings.get("document_member_env"):
            return None
        member = self.env("document_member_env")
        if not re.fullmatch(r"ou_[A-Za-z0-9_-]+", member):
            raise APIError("document member must be an explicitly configured user open_id")
        return member

    def app_id(self):
        return self.env("app_id_env")

    def authenticate(self):
        if not self.token:
            try:
                response = self.transport("POST", self.base + "/auth/v3/tenant_access_token/internal",
                    {"app_id": self.env("app_id_env"), "app_secret": self.env("app_secret_env")}, {})
            except (AmbiguousError, OSError, TimeoutError):
                raise APIError("tenant authentication failed before any publication request; explicit retry is safe")
            self.check(response)
            self.token = response.get("tenant_access_token")
            if type(self.token) is not str or not self.token:
                raise APIError("tenant token missing")

    def request(self, method, path, body=None):
        self.authenticate()
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
    for block in document["children"]:
        block_signature(block)
    return {"status": "dry_run", "report": report_id, "hash": report["data"]["hash"], "path": str(root),
            "card": card, "document": document, "note": "local preview only; webhook supports cards only; no network requests"}


def make_client(settings, transport=http, base=BASE, process=None):
    kind = settings.get("transport", "http")
    if kind == "lark_cli":
        from .lark_cli import LarkCLI
        return LarkCLI(settings, process=process)
    if kind != "http":
        raise APIError("invalid publisher transport")
    return Feishu(settings, transport, base)


def document_card(card, document_id):
    card = json.loads(dumps(card))
    url = "https://www.feishu.cn/docx/" + urllib.parse.quote(document_id, safe="")
    def has_button(node):
        if type(node) is dict:
            if node.get("tag") == "button" and (node.get("url") == url or
                    (type(node.get("multi_url")) is dict and node["multi_url"].get("url") == url)):
                return True
            return any(has_button(value) for value in node.values())
        return type(node) is list and any(has_button(value) for value in node)
    if not has_button(card):
        button = {"tag": "action", "actions": [{"tag": "button", "type": "default",
            "text": {"tag": "plain_text", "content": "打开 Daily Sync"}, "url": url}]}
        index = len(card["elements"]) - (card["elements"][-1].get("tag") == "note")
        card["elements"].insert(index, button)
    return card


def publish(store, settings, report_id, confirm=False, transport=http, base=BASE, retry=False, process=None):
    payload = preview(store, report_id)
    report_hash = payload["hash"]
    client = make_client(settings, transport, base, process)
    if settings["mode"] == "app":
        # Outbound card revision includes the runtime cloud-document button.
        payload["hash"] = digest([payload["hash"], "feishu-docx-button-v1"])
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
        if data.get("layout_lock"):
            raise ValueError("document layout is locked for an explicit native-tree transition")
        if data.get("status") in {"outcome_unknown", "publishing"}:
            raise ValueError("publication outcome unknown; reconcile before explicit retry")
        if data.get("status") == "completed" and (data.get("hash") == payload["hash"] or
                ("report_hash" not in data and data.get("hash") == report_hash)):
            return dict(data, no_op=True)
        if data.get("document_member_status") == "outcome_unknown":
            raise ValueError("document sharing outcome unknown; explicitly confirm member grant via reconcile before retry")
        if data.get("status") in {"failed", "retry_authorized", "cancelled"} and not retry:
            raise ValueError("previous attempt requires explicit --retry after inspection")
        if prior and data["mode"] == "webhook" and data.get("hash") != payload["hash"] and data.get("status") == "completed":
            raise ValueError("webhook cannot update prior cards; use app mode for period updates")
        document_hash = digest(payload["document"])
        acknowledged_report = data.get("document_report_id")
        previous_attempt_report = data.get("report_id")
        if not acknowledged_report and data.get("status") == "completed":
            acknowledged_report = data.get("report_id")
        prior_document_hash = data.get("document_payload_hash")
        if not prior_document_hash and acknowledged_report:
            prior_document_hash = digest(preview(store, acknowledged_report)["document"])
        if acknowledged_report and data.get("document_id"):
            data.update(document_report_id=acknowledged_report, document_payload_hash=prior_document_hash)
        migration = data.get("document_migration")
        if migration and migration.get("recovery_required"):
            raise ValueError("migration outcome unknown; explicit retry_authorized reconciliation required")
        data.update(status="publishing", hash=payload["hash"], report_hash=report_hash, report_id=report_id, pending=None)
        data.pop("error", None)
        data.pop("authentication_incomplete", None)
        publication = store.record("publication", data, project_id=report["project_id"])

        def checkpoint():
            store.update_record(publication["id"], data)

        def effect(stage, method, path, body):
            try:
                client.authenticate()
            except BaseException:
                data["authentication_incomplete"] = True
                raise
            data.pop("authentication_incomplete", None)
            data["pending"] = {"stage": stage, "method": method, "path": path, "body_hash": digest(body)}
            checkpoint()  # Write-ahead intent before *each* potential side effect.
            result = client.request(method, path, body)
            data["steps"] = data["steps"] + [{"stage": stage, "body_hash": digest(body)}]
            data["pending"] = None
            return result

        def migrate_document(source_report):
            nonlocal migration
            target = payload["document"]
            if not migration or migration["state"] == "complete":
                source = preview(store, source_report)["document"]
                actual = document_blocks_snapshot(client, data["document_id"])
                if (source["title"] != target["title"] or actual["title"] != source["title"] or
                        actual["ids"] != data["block_ids"] or
                        actual["signatures"] != [block_signature(b) for b in source["children"]]):
                    raise APIError("document IDs/content/styles/title differ from acknowledged report; refusing replacement")
                migration = {"id": str(uuid.uuid4()), "state": "appending", "source_report_id": source_report,
                    "target_report_id": report_id, "source_hash": digest(source), "target_hash": document_hash,
                    "old_block_ids": list(data["block_ids"]), "new_block_ids": [], "pending_batch_count": 0,
                    "initial_revision": actual["revision"], "last_ack_revision": actual["revision"],
                    "concurrency_scope": "read checks only; document_revision_id is not atomic compare-and-swap"}
                data["document_migration"] = migration
                checkpoint()
            if migration["target_hash"] != document_hash:
                raise APIError("finish or reconcile the existing migration before changing its target")
            observation = migration_observation(store, client, data)
            if observation["new_block_ids"] != migration["new_block_ids"]:
                raise APIError("unacknowledged migration IDs require explicit reconciliation")
            if migration.get("last_ack_revision") is not None and observation["revision"] != migration["last_ack_revision"]:
                raise APIError("document revision changed since migration checkpoint; inspect before continuing")
            revision = observation["revision"]
            doc = urllib.parse.quote(data["document_id"], safe="")
            while observation["state"] != "complete" and len(migration["new_block_ids"]) < len(target["children"]):
                offset = len(migration["new_block_ids"])
                batch = target["children"][offset:offset + 50]
                token = str(uuid.uuid5(uuid.UUID(migration["id"]), "append:%s" % offset))
                migration.update(state="appending", pending_batch_count=len(batch), append_client_token=token)
                checkpoint()
                query = urllib.parse.urlencode({"document_revision_id": revision, "client_token": token})
                result = effect("migration_append:%s" % offset, "POST",
                    "/docx/v1/documents/%s/blocks/%s/children?%s" % (doc, doc, query), {"children": batch, "index": -1})
                created = result.get("children", [])
                ids = [b.get("block_id") for b in created if type(b) is dict]
                if (len(ids) != len(batch) or any(type(i) is not str or not i for i in ids) or
                        len(set(ids)) != len(ids) or set(ids) & set(migration["old_block_ids"] + migration["new_block_ids"])):
                    raise AmbiguousError("migration append response missing unique IDs; inspect before retry")
                revision = result.get("document_revision_id")
                revision = revision if type(revision) is int and revision > 0 else None
                migration.update(new_block_ids=migration["new_block_ids"] + ids, pending_batch_count=0,
                                 last_ack_revision=revision)
                checkpoint()
                if revision is None:
                    observation = migration_observation(store, client, data)
                    revision = observation["revision"]
            observation = migration_observation(store, client, data)
            if observation["state"] != "complete":
                if observation["state"] != "ready_delete" or observation["new_block_ids"] != migration["new_block_ids"]:
                    raise APIError("migration is not ready for old-block removal")
                if migration.get("last_ack_revision") is not None and observation["revision"] != migration["last_ack_revision"]:
                    raise APIError("document changed after native append; refusing deletion")
                token = str(uuid.uuid5(uuid.UUID(migration["id"]), "delete-old"))
                migration.update(state="ready_delete", verified_delete_revision=observation["revision"], delete_client_token=token)
                checkpoint()
                # The API revision is not CAS: a concurrent edit after this read
                # cannot be atomically excluded. Preserve that limitation explicitly.
                query = urllib.parse.urlencode({"document_revision_id": observation["revision"], "client_token": token})
                result = effect("migration_delete_old", "DELETE",
                    "/docx/v1/documents/%s/blocks/%s/children/batch_delete?%s" % (doc, doc, query),
                    {"start_index": 0, "end_index": len(migration["old_block_ids"])})
                revision = result.get("document_revision_id")
                migration.update(state="verifying_new", last_ack_revision=revision if type(revision) is int and revision > 0 else None)
                checkpoint()
                observation = migration_observation(store, client, data)
            if observation["state"] != "complete":
                raise APIError("migration post-delete content/IDs mismatch; card was not updated")
            if migration.get("last_ack_revision") is not None and observation["revision"] != migration["last_ack_revision"]:
                raise APIError("document changed before migration verification completed")
            migration.update(state="complete", verified_revision=observation["revision"])
            data["block_ids"] = list(migration["new_block_ids"])
            checkpoint()

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
                member = client.document_member()
                if member:
                    perm = client.settings.get("document_member_perm", "edit")
                    fingerprint = digest([member, perm])
                    if data.get("document_member_fingerprint") not in (None, fingerprint):
                        raise APIError("document member changed; refusing to share existing document")
                    if data.get("document_member_fingerprint") != fingerprint:
                        effect("share_document_member", "POST", "/drive/v1/permissions/%s/members?type=docx&need_notification=false" % doc,
                               {"member_type": "openid", "member_id": member, "perm": perm, "type": "user"})
                        data.update(document_member_fingerprint=fingerprint, document_member_status="granted",
                                    document_member_verification="api_acknowledged")
                        checkpoint()
                children = payload["document"]["children"]
                update_document = prior_document_hash != document_hash
                source_document = preview(store, acknowledged_report)["document"] if update_document and acknowledged_report else None
                if source_document and source_document["title"] != payload["document"]["title"]:
                    raise APIError("document metadata title change requires a separate explicit operation")
                type_change = source_document and (len(children) < len(source_document["children"]) or any(
                    old["block_type"] != new["block_type"] or block_signature(old)["style"] != block_signature(new)["style"]
                    for old, new in zip(source_document["children"], children)))
                active_migration = migration and migration["state"] != "complete"
                if (data["block_ids"] and not source_document and not active_migration and
                        previous_attempt_report != report_id and any(b["block_type"] != 2 for b in children)):
                    raise APIError("native update requires the previous acknowledged document snapshot")
                if active_migration or (type_change and data["block_ids"]):
                    migrate_document(acknowledged_report)
                    update_document = False
                for index, block in enumerate(children if update_document else []):
                    if index < len(data["block_ids"]):
                        block_id = urllib.parse.quote(data["block_ids"][index], safe="")
                        effect("update_block:%s" % index, "PATCH", "/docx/v1/documents/%s/blocks/%s" % (doc, block_id),
                               {"update_text_elements": {"elements": block_text(block)["elements"]}})
                    else:
                        result = effect("create_block:%s" % index, "POST", "/docx/v1/documents/%s/blocks/%s/children" % (doc, doc),
                                        {"children": [block], "index": index})
                        created = result.get("children", [])
                        if not created or not created[0].get("block_id"):
                            raise AmbiguousError("created block response missing ID")
                        data["block_ids"] = data["block_ids"] + [created[0]["block_id"]]
                    checkpoint()
                if update_document and len(data["block_ids"]) > len(children):
                    effect("delete_obsolete_blocks", "DELETE", "/docx/v1/documents/%s/blocks/%s/children/batch_delete" % (doc, doc),
                           {"start_index": len(children), "end_index": len(data["block_ids"])})
                    data["block_ids"] = data["block_ids"][:len(children)]
                    checkpoint()
                data.update(document_payload_hash=document_hash, document_report_id=report_id)
                checkpoint()
                card = document_card(payload["card"], data["document_id"])
                if data["message_id"]:
                    effect("update_card", "PATCH", "/im/v1/messages/" + urllib.parse.quote(data["message_id"], safe=""),
                           {"content": dumps(card)})
                else:
                    # Feishu documents uuid deduplication for send-message, not for doc creation.
                    message_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, identity + payload["hash"]))
                    result = effect("create_card", "POST", "/im/v1/messages?receive_id_type=" + settings["receive_id_type"],
                                    {"receive_id": client.env("receive_id_env"), "msg_type": "interactive",
                                     "content": dumps(card), "uuid": message_uuid})
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
            if data.get("authentication_incomplete"):
                data.update(status="failed", pending=None, error="authentication interrupted before any publication request")
            else:
                data.update(status="outcome_unknown", error="request interrupted or response ambiguous; inspect before retry")
                if (data.get("pending") or {}).get("stage") == "share_document_member":
                    data["document_member_status"] = "outcome_unknown"
                if migration and migration.get("state") != "complete":
                    migration["recovery_required"] = True
            checkpoint()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        return dict(data, publication_id=publication["id"])


def reconcile(store, settings, ident, resolution=None, reason=None, remote_ids=None, transport=http, base=BASE, process=None):
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
        client = make_client(settings, transport, base, process)
        if (data.get("document_id") or data.get("message_id")) and client.destination() != data["destination"]:
            raise ValueError("reconcile destination differs from original publication")
        sharing_unknown = (data.get("document_member_status") == "outcome_unknown" or
            (data["status"] == "outcome_unknown" and (data.get("pending") or {}).get("stage") == "share_document_member"))
        if sharing_unknown:
            data["document_member_status"] = "outcome_unknown"
        if remote_ids is not None and not resolution:
            raise ValueError("remote confirmations require an explicit resolution and reason")
        if remote_ids is not None and type(remote_ids) is not dict:
            raise ValueError("remote IDs must be an object")
        inspected = {}
        if data.get("document_id"):
            doc = urllib.parse.quote(data["document_id"], safe="")
            inspected["document"] = client.request("GET", "/docx/v1/documents/" + doc)
            inspected["content"] = client.request("GET", "/docx/v1/documents/" + doc + "/raw_content")
        if data.get("message_id"):
            inspected["message"] = client.request("GET", "/im/v1/messages/" + urllib.parse.quote(data["message_id"], safe=""))
        migration = data.get("document_migration")
        observation = None
        if migration and migration.get("state") != "complete":
            observation = migration_observation(store, client, data)
            inspected["migration"] = {"state": observation["state"], "revision": observation["revision"],
                "old_block_ids": migration["old_block_ids"], "new_block_ids": observation["new_block_ids"],
                "content_hash": digest(observation["signatures"]),
                "note": "read-only comparison; explicit resolution required before continuing"}
        if resolution:
            if resolution not in {"completed", "retry_authorized", "cancelled"} or not reason or not reason.strip():
                raise ValueError("explicit resolution requires a valid status and reason")
            if observation and resolution in {"retry_authorized", "completed"}:
                if resolution == "completed" and observation["state"] != "complete":
                    raise ValueError("partial document migration cannot be marked completed")
                if remote_ids and (remote_ids.get("document_id", data["document_id"]) != data["document_id"] or
                        remote_ids.get("block_ids", observation["new_block_ids"] if observation["state"] == "complete" else migration["old_block_ids"]) !=
                        (observation["new_block_ids"] if observation["state"] == "complete" else migration["old_block_ids"])):
                    raise ValueError("migration remote IDs must match the read-only inspected document")
                migration.update(state=observation["state"], new_block_ids=observation["new_block_ids"],
                    pending_batch_count=0, last_ack_revision=observation["revision"], recovery_required=False)
                if observation["state"] == "complete":
                    data.update(block_ids=list(observation["new_block_ids"]), document_payload_hash=migration["target_hash"],
                                document_report_id=migration["target_report_id"])
            if remote_ids:
                if type(remote_ids) is not dict or set(remote_ids) - {"document_id", "message_id", "block_ids", "document_member_granted"}:
                    raise ValueError("invalid remote ID mapping")
                for key in ("document_id", "message_id"):
                    if key in remote_ids and type(remote_ids[key]) is not str:
                        raise ValueError("remote IDs must be strings")
                if "block_ids" in remote_ids and (type(remote_ids["block_ids"]) is not list or any(type(i) is not str for i in remote_ids["block_ids"])):
                    raise ValueError("block_ids must be string list")
                confirmation = remote_ids.get("document_member_granted")
                if "document_member_granted" in remote_ids:
                    if type(confirmation) is not bool:
                        raise ValueError("document_member_granted must be an exact JSON boolean after manual verification")
                    if confirmation is False and (resolution != "retry_authorized" or not sharing_unknown):
                        raise ValueError("confirming absent sharing requires an unknown share and retry_authorized resolution")
                    member = client.document_member()
                    if not member or client.destination() != data["destination"]:
                        raise ValueError("document member confirmation requires the original explicit member and destination")
                    document_id = remote_ids.get("document_id", data.get("document_id"))
                    if not document_id or (data.get("document_id") and document_id != data["document_id"]):
                        raise ValueError("document member confirmation must refer to the original known document")
                    if confirmation is False and not data.get("document_id"):
                        raise ValueError("confirming absent sharing requires the original known document ID")
                    fingerprint = digest([member, client.settings.get("document_member_perm", "edit")])
                    if data.get("document_member_fingerprint") not in (None, fingerprint):
                        raise ValueError("document member confirmation differs from recorded member")
                    if confirmation:
                        data.update(document_member_fingerprint=fingerprint, document_member_status="granted",
                                    document_member_verification="operator_confirmed")
                    else:
                        data.pop("document_member_fingerprint", None)
                        data.update(document_member_status="operator_verified_absent",
                                    document_member_verification="operator_confirmed_absent")
                data.update({key: value for key, value in remote_ids.items() if key != "document_member_granted"})
            if (sharing_unknown and resolution in {"completed", "retry_authorized"} and
                    data.get("document_member_status") not in {"granted", "operator_verified_absent"}):
                raise ValueError("unknown document sharing requires explicit document_member_granted confirmation before continuing")
            if resolution == "completed" and data.get("document_member_status") == "operator_verified_absent":
                raise ValueError("absent document sharing must be retried or confirmed granted before completion")
            data.update(status=resolution, resolution_reason=reason, pending=None)
            with store.transaction():
                store.update_record(ident, data)
                store.db.execute("DELETE FROM locks WHERE resource=?", (resource,))
                store.record("event", {"action": "publication_resolution", "publication": ident, "resolution": resolution,
                                      "reason": reason, "remote_ids": remote_ids}, project_id=record["project_id"])
        return {"publication": data, "inspection": inspected, "note": "GET inspection only; no agent resumed or publication resent"}
