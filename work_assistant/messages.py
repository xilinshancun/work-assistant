"""Explicit outgoing text/display cards with durable receipts and no automatic retries."""
import argparse
import json
import os
import re
import sqlite3
import stat
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

from . import config
from .message_cards import parse_card, card_projection, matches_card_projection
from .feishu import APIError, AmbiguousError, BASE, http, make_client
from .process import TerminationRequested, termination_guard
from .store import Busy, Store
from .util import digest, dumps, now, outbound_guard, strict_json

MAX_TEXT_BYTES = 16 * 1024
KIND = "outgoing_message"
UNRESOLVED = {"preparing", "sending", "outcome_unknown", "verification_pending", "needs_user"}
REPLY_FIELDS = ("reply_to_message_id", "reply_in_thread", "reply_source", "reply_ack_checks",
                "observed_parent_id", "observed_root_id", "observed_thread_id", "requested_placement",
                "observed_placement", "placement_matches_request")


class MessageError(ValueError):
    """A diagnostic authored locally, without server output or credentials."""


def _settings(settings):
    if settings.get("mode") != "app":
        raise MessageError("ordinary messages require publisher.mode=app")


def _id(value, label, prefix=None):
    if (type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value)
            or (prefix and (not value.startswith(prefix) or len(value) <= len(prefix)))):
        raise MessageError(label + " must be a valid explicit ID")
    return value


def _request(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
        raise MessageError("request-id must be a stable original logical request ID (1..200 ASCII characters)")
    return value


def _identity(settings, identity):
    if identity not in ("bot", "user"):
        raise MessageError("message identity must be bot or user")
    if identity == "user":
        if settings.get("transport") != "lark_cli":
            raise MessageError("user messages require publisher.transport=lark_cli")
        return _id(settings.get("message_user_open_id"), "trusted message user", "ou_")
    return None


def _client(settings, identity, transport, base, process):
    if identity == "user":
        from .user_lark_cli import UserLarkCLI
        return UserLarkCLI(dict(settings), process=process)
    return make_client(dict(settings), transport=transport, base=base, process=process)


def _reply_source(client, message_id, chat_id):
    result = client.request("GET", "/im/v1/messages/" + quote(message_id, safe=""))
    if type(result) is not dict or type(result.get("items")) is not list or len(result["items"]) != 1:
        raise MessageError("reply source must be exactly the known message")
    source = result["items"][0]
    if (type(source) is not dict or source.get("message_id") != message_id
            or source.get("chat_id") != chat_id or source.get("deleted", False) is not False):
        raise MessageError("reply source does not match the original chat/message or was deleted")
    frozen = {"message_id": message_id, "chat_id": chat_id}
    for field in ("parent_id", "root_id", "thread_id"):
        value = source.get(field)
        frozen[field] = None if value in (None, "") else _id(value, "source " + field,
            "om_" if field != "thread_id" else "omt_")
    return frozen


def _reply_checks(data, message, readback=False):
    source = data["reply_source"]
    # Official replies inside an existing topic point parent/root to the topic
    # root, including replies to a child. The requested source remains frozen.
    thread = message.get("thread_id")
    # false requests may still be placed in a topic by the service. Accept that
    # placement only with the same exact topic-root/parent/thread relationships.
    threaded = data["reply_in_thread"] or bool(source.get("thread_id")) or thread not in (None, "")
    root = (source.get("root_id") or source["message_id"]) if source.get("thread_id") or not threaded else source["message_id"]
    parent = root if threaded else source["message_id"]
    valid_thread = type(thread) is str and re.fullmatch(r"omt_[A-Za-z0-9_-]{1,196}", thread) is not None
    checks = {"reply_parent": message.get("parent_id") == parent,
              "reply_root": message.get("root_id") == root,
              "reply_chat": message.get("chat_id") == source["chat_id"],
              "reply_thread": (valid_thread and (not source.get("thread_id") or thread == source["thread_id"]))
                              if threaded else thread in (None, "")}
    if readback:
        checks["reply_thread"] = bool(checks["reply_thread"] and
            (None if thread in (None, "") else thread) == data.get("observed_thread_id"))
    return checks


def _placement(message):
    thread = message.get("thread_id")
    if thread in (None, ""):
        return "main"
    return "thread" if type(thread) is str and re.fullmatch(r"omt_[A-Za-z0-9_-]{1,196}", thread) else "unknown"


def _text_file(path):
    with os.fdopen(os.open(str(Path(path).expanduser()), os.O_RDONLY | os.O_NONBLOCK), "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise MessageError("text-file must be a regular file")
        raw = source.read(MAX_TEXT_BYTES + 1)
    if len(raw) > MAX_TEXT_BYTES:
        raise MessageError("text-file exceeds 16 KiB; message is not truncated")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise MessageError("text-file must contain valid UTF-8") from None
    if not text.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise MessageError("text-file must contain nonempty plain text without control characters")
    try:
        outbound_guard(text)
    except ValueError:
        raise MessageError("text-file contains an obvious credential; remove it before sending") from None
    return text, raw


def _action_id(request_id):
    return "message_" + digest(request_id)[:32]


def _find(store, ident):
    if not store.db.execute("SELECT 1 FROM records WHERE id=?", (ident,)).fetchone():
        return None
    record = store.get(ident)
    if record["kind"] != KIND:
        raise MessageError("action ID is not an outgoing message")
    return record


def _receipt(record, no_op=False):
    data = record["data"]
    fields = ("request_id", "status", "verification_status", "uuid", "receive_id_type", "receive_id",
              "text_sha256", "target_sha256", "expected_sender_app_id", "message_id", "chat_id",
              "send_acknowledged", "checks", "error", "verified_at", "resolution", "msg_type", "verification_scope",
              "operation", "update_parent", "superseded_by", "pending", "expected_sender_user_open_id") + REPLY_FIELDS
    return dict({key: data.get(key) for key in fields}, identity=data.get("identity", "bot"), action_id=record["id"], no_op=no_op)


def _persist_text(store, ident, raw):
    directory = store.root / "messages"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.resolve() != directory:
        raise MessageError("private message directory must not be a symlink")
    path = directory / (ident + ".txt")
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise MessageError("existing private source differs or is not mode 0600; inspect local state")
    else:
        with os.fdopen(descriptor, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
    return str(path)


def _save(store, record, **changes):
    data = dict(record["data"], **changes)
    with store.transaction():
        store.update_record(record["id"], data)
    return store.get(record["id"])


def _recover(store, record):
    if record["data"]["status"] == "sending":
        return _save(store, record, status="outcome_unknown", verification_status="unresolved",
                     error="interrupted_after_send_intent; inspect before any new authorization")
    if record["data"]["status"] == "preparing":
        if record["data"].get("operation") == "update_card":
            return _save(store, record, status="failed", verification_status="not_updated",
                         error="interrupted_before_update; no PATCH started")
        return _save(store, record, status="failed", verification_status="not_sent",
                     error="interrupted_before_send; no message request started")
    return record


def _remote_resources(data):
    if data.get("message_id") and data.get("expected_sender_app_id"):
        return ["message-remote:" + digest([data["expected_sender_app_id"], data["message_id"]])]
    return []


def _readback(store, record, client, persist=True):
    data = record["data"]
    def save(**changes):
        return _save(store, record, **changes) if persist else dict(record, data=dict(data, **changes))
    try:
        source = Path(data["source_artifact"])
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(str(source), flags), "rb") as saved:
            info = os.fstat(saved.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise MessageError("private source must be a mode-0600 regular file")
            raw = saved.read(MAX_TEXT_BYTES + 1)
        if digest(raw) != data["text_sha256"]:
            raise MessageError("private source is missing or modified")
        expected = raw.decode("utf-8")
        card = parse_card(expected) if data.get("msg_type", "text") == "interactive" else None
        result = client.request("GET", "/im/v1/messages/" + quote(data["message_id"], safe=""))
        if type(result) is not dict or type(result.get("items")) is not list or len(result["items"]) != 1:
            raise MessageError("readback must contain exactly the known message")
        message = result["items"][0]
        if type(message) is not dict:
            raise MessageError("invalid readback message")
        sender, body = message.get("sender"), message.get("body")
        content = strict_json(body["content"]) if type(body) is dict and type(body.get("content")) is str else None
        user = data.get("identity", "bot") == "user"
        checks = {"message_id": message.get("message_id") == data["message_id"],
                  "sender": (type(sender) is dict and sender.get("sender_type") == ("user" if user else "app")
                             and sender.get("id_type") == ("open_id" if user else "app_id")
                             and sender.get("id") == data.get("expected_sender_user_open_id" if user else "expected_sender_app_id")),
                  "chat": bool(data.get("chat_id")) and message.get("chat_id") == data["chat_id"],
                  "content": (matches_card_projection(card, content) or content == card) if card is not None else (type(content) is dict and set(content) == {"text"} and content["text"] == expected),
                  "msg_type": message.get("msg_type") == data.get("msg_type", "text"),
                  "not_deleted": message.get("deleted", False) is False}
        if card is None:
            checks["text"] = checks.pop("content")
        if data.get("reply_to_message_id"):
            checks.update(_reply_checks(data, message, readback=True))
            checks["reply_acknowledgement"] = bool(data.get("reply_ack_checks")) and all(data["reply_ack_checks"].values())
        verified = all(checks.values())
        indistinguishable = (data.get('operation') == 'update_card' and bool(data.get('pending'))
                             and data.get('previous_projection_hash') == digest(card_projection(card))) if card else False
        if indistinguishable:
            verified = False
        scope = "readable_card_content_identity_and_chat; URLs/styles retained in sent payload, not independently verified by GET" if card is not None else "exact_text_identity_and_chat"
        if data.get("reply_to_message_id"):
            scope += "; original_reply_parent_root_and_thread"
        placement = {}
        if data.get("reply_to_message_id"):
            placement = {"observed_placement": _placement(message),
                         "placement_matches_request": _placement(message) == data["requested_placement"]}
        record = save(checks=checks, status="needs_user" if indistinguishable else "completed" if verified else "verification_pending",
                       verification_status="verified" if verified else "verification_pending",
                       error="readable projections indistinguishable; URL/style update outcome cannot be proved by GET" if indistinguishable else None if verified else "readback_does_not_match_expected_message",
                       verified_at=now() if verified else None, verification_scope=scope,
                       **({"pending": None} if verified and data.get("operation") == "update_card" else {}),
                       readback_create_time=message.get("create_time"), card_update_multi=card["config"].get("update_multi") if card else None,
                       readable_card_hash=digest(card_projection(card)) if card else None, **placement)
    except BaseException as exc:
        record = save(status="verification_pending", verification_status="verification_pending",
                       error="readback_unavailable_or_invalid; known sent ID retained; inspect only", verified_at=None, checks={})
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
    if persist:
        with store.transaction():
            store.record("message_observation", {"action_id": record["id"], "message_id": data["message_id"],
                         "verification_status": record["data"]["verification_status"], "checks": record["data"].get("checks", {})})
    return record


def send(store, settings, request_id, receive_id_type, receive_id, text_file, confirm=False,
         transport=http, base=BASE, process=None, card_file=None, identity="bot",
         reply_to_message_id=None, reply_in_thread=False):
    """One explicit user request. Reentry never performs a second POST."""
    _settings(settings)
    owner = _identity(settings, identity)
    if type(confirm) is not bool:
        raise MessageError("confirm must be an explicit boolean; model content is not permission")
    _request(request_id)
    if type(receive_id_type) is not str or receive_id_type not in {"chat_id", "open_id"}:
        raise MessageError("receive-id-type must be chat_id or open_id")
    _id(receive_id, "receive-id", "oc_" if receive_id_type == "chat_id" else "ou_")
    if type(reply_in_thread) is not bool:
        raise MessageError("reply-in-thread must be an explicit boolean")
    if reply_to_message_id is not None:
        _id(reply_to_message_id, "reply source message ID", "om_")
        if identity != "bot" or receive_id_type != "chat_id":
            raise MessageError("reply currently requires bot identity and an explicit chat_id")
    elif reply_in_thread:
        raise MessageError("reply-in-thread requires a reply source message ID")
    if (text_file is None) == (card_file is None):
        raise MessageError("provide exactly one text-file or card-file")
    text, raw = _text_file(card_file if card_file is not None else text_file)
    try:
        card = parse_card(text) if card_file is not None else None
    except ValueError:
        raise MessageError("card-file must be a supported display-only card") from None
    msg_type = "interactive" if card is not None else "text"
    ident = _action_id(request_id)
    target_hash = digest(["reply", receive_id, reply_to_message_id, reply_in_thread]) if reply_to_message_id else digest([receive_id_type, receive_id])
    text_hash = digest(raw)
    resource = "message:" + digest(request_id)
    payload_resource = "message-payload:" + digest([target_hash, text_hash])
    with store.inspection_lock([resource, payload_resource]):
        record = _find(store, ident)
        if record and (record["data"]["request_id"] != request_id or record["data"]["target_sha256"] != target_hash
                       or record["data"]["text_sha256"] != text_hash or record["data"].get("msg_type", "text") != msg_type
                       or record["data"].get("operation", "send") != "send"
                       or record["data"].get("identity", "bot") != identity
                       or record["data"].get("reply_to_message_id") != reply_to_message_id
                       or record["data"].get("reply_in_thread", False) != reply_in_thread
                       or (identity == "user" and record["data"].get("expected_sender_user_open_id") != owner)):
            raise MessageError("request-id already belongs to different target/content; do not replace it")
        if not confirm:
            return {"status": "dry_run", "action_id": ident, "request_id": request_id,
                    "receive_id_type": receive_id_type, "receive_id": receive_id,
                    "text": text, "text_sha256": text_hash, "target_sha256": target_hash,
                    "prior_status": record["data"]["status"] if record else None, "confirm_required": True, "msg_type": msg_type,
                    "identity": identity, "expected_sender_user_open_id": owner,
                    "reply_to_message_id": reply_to_message_id, "reply_in_thread": reply_in_thread,
                    "requested_placement": ("thread" if reply_in_thread else "main") if reply_to_message_id else None}
        if record:
            return _receipt(_recover(store, record), no_op=True)
        for prior in store.records(KIND):
            previous = prior["data"]
            if (previous["target_sha256"] == target_hash and previous["text_sha256"] == text_hash
                    and previous.get("identity", "bot") == identity
                    and (identity == "bot" or previous.get("expected_sender_user_open_id") == owner)
                    and previous["status"] in UNRESOLVED):
                raise MessageError("identical message has an unresolved action " + prior["id"] + "; inspect it, do not replace request-id")
        artifact = _persist_text(store, ident, raw)
        with store.transaction():
            record = store.record(KIND, {"schema": "wa.message.v1", "request_id": request_id,
                "identity": identity, "expected_sender_user_open_id": owner,
                "reply_to_message_id": reply_to_message_id, "reply_in_thread": reply_in_thread,
                "requested_placement": ("thread" if reply_in_thread else "main") if reply_to_message_id else None,
                "receive_id_type": receive_id_type, "receive_id": receive_id, "target_sha256": target_hash,
                "text_sha256": text_hash, "source_artifact": artifact, "msg_type": msg_type,
                "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, "wa.outgoing-message:" + request_id)),
                "status": "preparing", "verification_status": "not_sent", "expected_sender_app_id": None,
                "message_id": None, "chat_id": receive_id if receive_id_type == "chat_id" else None,
                "send_acknowledged": False}, ident=ident)
        try:
            client = _client(settings, identity, transport, base, process)
            app_id = _id(client.app_id(), "application ID", "cli_")
            client.authenticate()  # All authentication occurs before write-ahead message intent.
            record = _save(store, record, expected_sender_app_id=app_id)
        except BaseException as exc:
            record = _save(store, record, status="failed", verification_status="not_sent",
                           error="authentication_failed_before_send; no message request started")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return _receipt(record)
        if reply_to_message_id:
            try:
                source = _reply_source(client, reply_to_message_id, receive_id)
                record = _save(store, record, reply_source=source)
            except BaseException as exc:
                record = _save(store, record, status="failed", verification_status="not_sent",
                               error="reply_source_preflight_failed; no reply request started")
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                return _receipt(record)
        record = _save(store, record, status="sending", verification_status="unresolved")
        try:
            body = {"receive_id": receive_id, "msg_type": msg_type, "content": dumps(card if card is not None else {"text": text}),
                    "uuid": record["data"]["uuid"]}
            path = "/im/v1/messages?" + urlencode({"receive_id_type": receive_id_type})
            if reply_to_message_id:
                body.pop("receive_id")
                body["reply_in_thread"] = reply_in_thread
                path = "/im/v1/messages/" + quote(reply_to_message_id, safe="") + "/reply"
            result = client.request("POST", path, body)
            if type(result) is not dict:
                raise AmbiguousError("invalid send acknowledgement")
            message_id = result.get("message_id")
            if type(message_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", message_id):
                raise AmbiguousError("send acknowledgement missing message ID")
            chat_id = result.get("chat_id") if receive_id_type == "open_id" else receive_id
            if type(chat_id) is not str or not re.fullmatch(r"oc_[A-Za-z0-9_-]{1,200}", chat_id):
                chat_id = None
            relations = {}
            if reply_to_message_id:
                relations["reply_ack_checks"] = _reply_checks(record["data"], result)
                relations["observed_placement"] = _placement(result)
                relations["placement_matches_request"] = _placement(result) == record["data"]["requested_placement"]
                for field in ("parent_id", "root_id", "thread_id"):
                    value = result.get(field)
                    relations["observed_" + field] = value if type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value) else None
            record = _save(store, record, message_id=message_id, chat_id=chat_id, send_acknowledged=True,
                           **relations,
                           status="verification_pending", verification_status="verification_pending")
        except BaseException as exc:
            # The shared adapter's APIError has no structured guarantee of absence.
            # Every failure after POST intent is unknown, regardless of error text.
            record = _save(store, record, status="outcome_unknown", verification_status="unresolved",
                           error="send_outcome_unknown; never automatically resend")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return _receipt(record)
        return _receipt(_readback(store, record, client))


def update_card(store, settings, action_id, request_id, card_file, confirm=False,
                transport=http, base=BASE, process=None):
    """One explicit replacement of a known current card, never a new message POST."""
    _settings(settings)
    _id(action_id, "action ID")
    _request(request_id)
    if type(confirm) is not bool:
        raise MessageError("confirm must be an explicit boolean")
    text, raw = _text_file(card_file)
    try:
        card = parse_card(text)
    except ValueError:
        raise MessageError("card-file must be a supported display-only card") from None
    if card["config"].get("update_multi") is not True:
        raise MessageError("update-card requires new card config.update_multi=true")
    original = _find(store, action_id)
    if original is None:
        raise MessageError("unknown outgoing message action ID")
    old = original["data"]
    if old.get("identity", "bot") != "bot":
        raise MessageError("update-card does not support user identity")
    if old.get("msg_type") != "interactive" or not old.get("message_id") or not old.get("chat_id") or not old.get("expected_sender_app_id"):
        raise MessageError("update-card requires an original app/card/chat receipt")
    ident, text_hash = _action_id(request_id), digest(raw)
    resources = ["message:" + digest(request_id), "message:" + digest(old["request_id"]),
                 "message-payload:" + digest([old["target_sha256"], text_hash])] + _remote_resources(old)
    with store.inspection_lock(resources):
        record = _find(store, ident)
        if record and (record["data"].get("operation") != "update_card" or record["data"].get("update_parent") != action_id
                       or record["data"]["text_sha256"] != text_hash):
            raise MessageError("request-id already belongs to a different action/target/content")
        if record:
            return _receipt(_recover(store, record), no_op=True) if confirm else dict(_receipt(record), status="dry_run", confirm_required=True)
        original = store.get(action_id)
        old = original["data"]
        if old["status"] != "completed" or old.get("verification_status") != "verified" or old.get("superseded_by"):
            raise MessageError("update-card requires the current verified action; inspect unresolved or use superseding action")
        if not confirm:
            return {"status": "dry_run", "action_id": ident, "request_id": request_id, "update_parent": action_id,
                    "message_id": old["message_id"], "chat_id": old["chat_id"], "text_sha256": text_hash,
                    "card": card, "confirm_required": True}
        artifact = _persist_text(store, ident, raw)
        data = {key: old[key] for key in ("receive_id_type", "receive_id", "target_sha256", "expected_sender_app_id", "message_id", "chat_id")}
        data.update({key: old[key] for key in REPLY_FIELDS if key in old})
        data.update(schema="wa.message.v1", operation="update_card", update_parent=action_id,
                    request_id=request_id, uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, "wa.update-card:" + request_id)),
                    msg_type="interactive", text_sha256=text_hash, source_artifact=artifact, status="preparing",
                    verification_status="not_updated", send_acknowledged=False, pending=None)
        with store.transaction():
            record = store.record(KIND, data, ident=ident)
        try:
            client = make_client(dict(settings), transport=transport, base=base, process=process)
            if client.app_id() != old["expected_sender_app_id"]:
                raise MessageError("update-card requires the original application identity")
            client.authenticate()
            checked = _readback(store, original, client, persist=False)
            if checked["data"]["status"] != "completed":
                raise MessageError("original card readback differs or is unavailable; no update permitted")
            if checked["data"].get("card_update_multi") is not True:
                raise MessageError("update-card requires original private card config.update_multi=true")
            created = checked["data"].get("readback_create_time")
            if type(created) is not str or not re.fullmatch(r"[0-9]{1,17}", created):
                raise MessageError("update-card requires a valid GET create_time to verify the 14-day limit")
            age_ms = time.time() * 1000 - int(created)
            if not 0 <= age_ms <= 14 * 24 * 60 * 60 * 1000:
                raise MessageError("update-card is outside the supported 14-day update window")
        except BaseException as exc:
            record = _save(store, record, status="failed", verification_status="not_updated",
                           error=str(exc) if isinstance(exc, MessageError) else "update_preflight_failed; no PATCH started")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return _receipt(record)
        path = "/im/v1/messages/" + quote(old["message_id"], safe="")
        pending = {"method": "PATCH", "path": path, "body_hash": digest({"content": dumps(card)})}
        with store.transaction():
            store.record("message_update_intent", {"action_id": ident, "update_parent": action_id,
                         "message_id": old["message_id"], "pending": pending, "text_sha256": text_hash})
            store.record("message_supersession", {"action_id": action_id, "superseded_by": ident,
                         "prior_state": checked["data"], "basis": "explicit_update_intent"})
            store.update_record(action_id, dict(checked["data"], status="superseded", verification_status="superseded",
                                               verified_at=None, superseded_by=ident))
            store.update_record(ident, dict(record["data"], status="sending", verification_status="unresolved", pending=pending,
                                           previous_projection_hash=checked['data']['readable_card_hash']))
        record = store.get(ident)
        try:
            client.request("PATCH", path, {"content": dumps(card)})
            record = _save(store, record, send_acknowledged=True, pending=None,
                           status="verification_pending", verification_status="verification_pending")
        except BaseException as exc:
            record = _save(store, record, status="outcome_unknown", verification_status="unresolved",
                           error="update_outcome_unknown; inspect only; never automatically repeat PATCH")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return _receipt(record)
        return _receipt(_readback(store, record, client))


def inspect(store, settings, action_id, transport=http, base=BASE, process=None):
    _settings(settings)
    _id(action_id, "action ID")
    record = _find(store, action_id)
    if record is None:
        raise MessageError("unknown outgoing message action ID")
    identity = record["data"].get("identity", "bot")
    owner = _identity(settings, identity)
    if identity == "user" and record["data"].get("expected_sender_user_open_id") != owner:
        raise MessageError("inspect requires the original trusted user identity")
    with store.inspection_lock(["message:" + digest(record["data"]["request_id"])] + _remote_resources(record["data"])):
        record = _recover(store, store.get(action_id))
        if record["data"]["status"] in {"resolved_absent", "superseded", "not_applied"} or not record["data"].get("message_id"):
            return _receipt(record, no_op=True)
        if record["data"].get("operation") == "update_card" and record["data"]["status"] == "failed":
            return _receipt(record, no_op=True)
        client = _client(settings, identity, transport, base, process)
        if client.app_id() != record["data"]["expected_sender_app_id"]:
            raise MessageError("inspect requires the original application identity")
        return _receipt(_readback(store, record, client))


def resolve_update(store, settings, action_id, reason, confirm=False, transport=http, base=BASE, process=None):
    """Explicit local not-applied resolution; GET only, never PATCH or POST."""
    _settings(settings)
    _id(action_id, 'action ID')
    if (type(confirm) is not bool or type(reason) is not str or not reason.strip() or len(reason)>2000
            or any(ord(c)<32 and c not in '\n\r\t' for c in reason)):
        raise MessageError('resolution requires boolean confirm and a nonempty reason (max 2000 characters)')
    action = _find(store, action_id)
    if action is None or action['data'].get('operation') != 'update_card':
        raise MessageError('resolve-update requires an update-card action')
    parent = store.get(action['data']['update_parent'])
    resources = ['message:'+digest(action['data']['request_id']), 'message:'+digest(parent['data']['request_id'])] + _remote_resources(action['data'])
    with store.inspection_lock(resources):
        action, parent = store.get(action_id), store.get(parent['id'])
        data = action['data']
        if (data['status'] not in {'sending','outcome_unknown','verification_pending','needs_user'}
                or parent['data'].get('superseded_by') != action_id or parent['data']['status'] != 'superseded'):
            raise MessageError('resolution requires the current unresolved update and its superseded parent')
        client = make_client(dict(settings), transport=transport, base=base, process=process)
        if client.app_id() != data['expected_sender_app_id']:
            raise MessageError('resolution requires the original application identity')
        new = _readback(store, action, client, persist=False)['data']
        old = _readback(store, parent, client, persist=False)['data']
        if not new.get('readable_card_hash') or not old.get('readable_card_hash') or new['readable_card_hash'] == old['readable_card_hash']:
            raise MessageError('indistinguishable projections; GET cannot prove URL/style outcome; keep needs_user')
        if new['status'] == 'completed':
            raise MessageError('new projection matches; use inspect to complete, not not-applied resolution')
        if old['status'] != 'completed':
            raise MessageError('parent projection/identity/chat not verified; no restoration permitted')
        resolution = {'status':'not_applied', 'reason':reason, 'basis':'operator_assertion_with_readable_parent_match',
                      'scope':'readable content/identity/chat only; URL/style absence is not verified by GET'}
        if not confirm:
            return dict(_receipt(action),status='dry_run',proposed_resolution=resolution,confirm_required=True)
        resolution['resolved_at'] = now()
        with store.transaction():
            store.record('message_update_resolution',{'action_id':action_id,'parent_id':parent['id'],
                         'resolution':resolution,'prior_state':data,'parent_prior_state':parent['data'],
                         'parent_readback_checks':old['checks']})
            store.update_record(action_id,dict(data,status='not_applied',verification_status='operator_confirmed_not_applied',
                                               resolution=resolution,pending=None))
            restored = dict(parent['data'],status='completed',verification_status='verified',superseded_by=None,
                            restored_by=action_id,verified_at=old['verified_at'],verification_scope=old['verification_scope'])
            store.update_record(parent['id'],restored)
        return _receipt(store.get(action_id))


def resolve(store, action_id, absent=False, reason=None, confirm=False):
    """Record an explicit operator assertion locally; never query or resend."""
    _id(action_id, "action ID")
    if type(absent) is not bool or not absent or type(confirm) is not bool:
        raise MessageError("resolve requires explicit --absent; confirm must be boolean")
    if (type(reason) is not str or not reason.strip() or len(reason) > 2000
            or any(ord(c) < 32 and c not in "\n\r\t" for c in reason)):
        raise MessageError("resolve reason must be nonempty text (max 2000 characters), without control characters")
    record = _find(store, action_id)
    if record is None:
        raise MessageError("unknown outgoing message action ID")
    data = record["data"]
    resources = ["message:" + digest(data["request_id"]),
                 "message-payload:" + digest([data["target_sha256"], data["text_sha256"]])]
    with store.inspection_lock(resources):
        # Re-read under both send locks: inspection or another resolver may have finished.
        record = store.get(action_id)
        data = record["data"]
        if data.get("operation") == "update_card":
            raise MessageError("update-card absence cannot resolve a known existing message; inspect the update action")
        if data["status"] not in {"outcome_unknown", "verification_pending"}:
            raise MessageError("absent resolution requires outcome_unknown or verification_pending")
        resolution = {"status": "resolved_absent", "basis": "operator_assertion",
                      "reason": reason, "prior_status": data["status"]}
        if not confirm:
            return dict(_receipt(record), status="dry_run", prior_status=data["status"],
                        proposed_resolution=resolution, confirm_required=True)
        resolution["resolved_at"] = now()
        updated = dict(data, status="resolved_absent", verification_status="operator_confirmed_absent",
                       resolution=resolution)
        with store.transaction():
            store.record("message_resolution", {"action_id": action_id, "resolution": resolution,
                         "prior_state": data})
            store.update_record(action_id, updated)
        return _receipt(store.get(action_id))


def chats(settings, transport=http, base=BASE, process=None):
    _settings(settings)
    client = make_client(dict(settings), transport=transport, base=base, process=process)
    items, token, seen_tokens, seen_ids = [], None, set(), set()
    for page_number in range(1, 101):
        query = {"page_size": "100", "sort_type": "ByCreateTimeAsc"}
        if token:
            query["page_token"] = token
        page = client.request("GET", "/im/v1/chats?" + urlencode(query))
        if type(page) is not dict or type(page.get("items")) is not list or type(page.get("has_more")) is not bool:
            raise MessageError("invalid chat-list pagination; no partial list adopted")
        for item in page["items"]:
            if type(item) is not dict:
                raise MessageError("invalid chat-list item")
            ident = _id(item.get("chat_id"), "chat ID", "oc_")
            if ident in seen_ids or type(item.get("name")) is not str or len(item["name"]) > 2048:
                raise MessageError("duplicate chat or invalid name; list may have changed")
            seen_ids.add(ident)
            items.append({"chat_id": ident, "name": item["name"]})
        if len(items) > 10000:
            raise MessageError("chat list exceeds 10000 items")
        if not page["has_more"]:
            return {"status": "completed", "mode": "read_only", "chats": items, "pages": page_number}
        token = page.get("page_token")
        if type(token) is not str or not token or len(token) > 4096 or token in seen_tokens:
            raise MessageError("missing/repeated chat page token; no partial list adopted")
        seen_tokens.add(token)
    raise MessageError("chat pagination exceeds 100 pages; no partial list adopted")


class MessageParser(argparse.ArgumentParser):
    def error(self, message):
        raise MessageError("invalid command arguments; use --help")


def parser():
    result = MessageParser(prog="python3 -m work_assistant.messages", description=__doc__)
    result.add_argument("--config", default="config.local.json")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("chats", help="Read all accessible group IDs/names; no message is sent")
    command = commands.add_parser("send", help="Preview by default; --confirm is explicit sending authority")
    command.add_argument("--request-id", required=True)
    command.add_argument("--receive-id-type", choices=["chat_id", "open_id"], required=True)
    command.add_argument("--receive-id", required=True)
    command.add_argument("--identity", choices=["bot", "user"], default="bot", help="Explicit user OAuth messages only; default bot")
    command.add_argument("--reply-to-message-id", "--reply-to", dest="reply_to_message_id", help="Known source message to quote/reply to; bot and chat_id only")
    command.add_argument("--reply-in-thread", action="store_true", help="Reply in the source topic; requires --reply-to")
    files = command.add_mutually_exclusive_group(required=True)
    files.add_argument("--text-file")
    files.add_argument("--card-file", help="Display-only interactive card JSON; no callbacks or forms")
    modes = command.add_mutually_exclusive_group()
    modes.add_argument("--confirm", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    command = commands.add_parser("inspect", help="Read the known sent message only; never resend")
    command.add_argument("action_id")
    command = commands.add_parser("update-card", help="Replace one known verified card in place; never create a new message")
    command.add_argument("action_id")
    command.add_argument("--request-id", required=True)
    command.add_argument("--card-file", required=True)
    command.add_argument("--confirm", action="store_true")
    command = commands.add_parser('resolve-update', help='GET-only inspection and explicit local not-applied resolution')
    command.add_argument('action_id')
    command.add_argument('--not-applied', action='store_true', required=True)
    command.add_argument('--reason', required=True)
    command.add_argument('--confirm', action='store_true')
    command = commands.add_parser("resolve", help="Record operator-confirmed absence locally; never query or resend")
    command.add_argument("action_id")
    command.add_argument("--absent", action="store_true", required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--confirm", action="store_true")
    return result


def main(argv=None):
    try:
        with termination_guard():
            args = parser().parse_args(argv)
            conf = config.load(args.config)
            if args.command == "chats":
                result = chats(conf["publisher"])
            else:
                store = Store(conf["state_dir"])
                try:
                    if args.command == "send":
                        result = send(store, conf["publisher"], args.request_id, args.receive_id_type, args.receive_id,
                                      args.text_file, confirm=args.confirm, card_file=args.card_file, identity=args.identity,
                                      reply_to_message_id=args.reply_to_message_id, reply_in_thread=args.reply_in_thread)
                    elif args.command == "resolve":
                        result = resolve(store, args.action_id, absent=args.absent, reason=args.reason, confirm=args.confirm)
                    elif args.command == "update-card":
                        result = update_card(store, conf["publisher"], args.action_id, args.request_id, args.card_file, confirm=args.confirm)
                    elif args.command == 'resolve-update':
                        result = resolve_update(store, conf['publisher'], args.action_id, args.reason, confirm=args.confirm)
                    else:
                        result = inspect(store, conf["publisher"], args.action_id)
                finally:
                    store.close()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 3 if result["status"] in {"outcome_unknown", "verification_pending", "needs_user"} else 2 if result["status"] == "failed" else 0
    except (MessageError, Busy) as exc:
        result, code = {"status": "failed", "error": str(exc)}, 2
    except (APIError, AmbiguousError, ValueError, OSError, sqlite3.Error):
        result, code = {"status": "failed", "error": "request/configuration/local state unavailable; no automatic retry"}, 2
    except TerminationRequested:
        result, code = {"status": "interrupted", "error": "inspect original action before any new authorization"}, 143
    except KeyboardInterrupt:
        result, code = {"status": "interrupted", "error": "inspect original action before any new authorization"}, 130
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
