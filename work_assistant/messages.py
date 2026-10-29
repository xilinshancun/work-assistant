"""Explicit outgoing plain-text messages; no scheduler, inbound service, or retries."""
import argparse
import json
import os
import re
import sqlite3
import stat
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

from . import config
from .feishu import APIError, AmbiguousError, BASE, http, make_client
from .process import TerminationRequested, termination_guard
from .store import Busy, Store
from .util import digest, dumps, now, outbound_guard, strict_json

MAX_TEXT_BYTES = 16 * 1024
KIND = "outgoing_message"
UNRESOLVED = {"preparing", "sending", "outcome_unknown", "verification_pending"}


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
              "send_acknowledged", "checks", "error", "verified_at", "resolution")
    return dict({key: data.get(key) for key in fields}, action_id=record["id"], no_op=no_op)


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
        return _save(store, record, status="failed", verification_status="not_sent",
                     error="interrupted_before_send; no message request started")
    return record


def _readback(store, record, client):
    data = record["data"]
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
        result = client.request("GET", "/im/v1/messages/" + quote(data["message_id"], safe=""))
        if type(result) is not dict or type(result.get("items")) is not list or len(result["items"]) != 1:
            raise MessageError("readback must contain exactly the known message")
        message = result["items"][0]
        if type(message) is not dict:
            raise MessageError("invalid readback message")
        sender, body = message.get("sender"), message.get("body")
        content = strict_json(body["content"]) if type(body) is dict and type(body.get("content")) is str else None
        checks = {"message_id": message.get("message_id") == data["message_id"],
                  "sender": (type(sender) is dict and sender.get("sender_type") == "app"
                             and sender.get("id_type") == "app_id" and sender.get("id") == data["expected_sender_app_id"]),
                  "chat": bool(data.get("chat_id")) and message.get("chat_id") == data["chat_id"],
                  "text": type(content) is dict and set(content) == {"text"} and content["text"] == expected,
                  "msg_type": message.get("msg_type") == "text",
                  "not_deleted": message.get("deleted", False) is False}
        verified = all(checks.values())
        record = _save(store, record, checks=checks, status="completed" if verified else "verification_pending",
                       verification_status="verified" if verified else "verification_pending",
                       error=None if verified else "readback_does_not_match_expected_message",
                       verified_at=now() if verified else None)
    except BaseException as exc:
        record = _save(store, record, status="verification_pending", verification_status="verification_pending",
                       error="readback_unavailable_or_invalid; known sent ID retained; inspect only", verified_at=None, checks={})
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
    with store.transaction():
        store.record("message_observation", {"action_id": record["id"], "message_id": data["message_id"],
                     "verification_status": record["data"]["verification_status"], "checks": record["data"].get("checks", {})})
    return record


def send(store, settings, request_id, receive_id_type, receive_id, text_file, confirm=False,
         transport=http, base=BASE, process=None):
    """One explicit user request. Reentry never performs a second POST."""
    _settings(settings)
    if type(confirm) is not bool:
        raise MessageError("confirm must be an explicit boolean; model content is not permission")
    _request(request_id)
    if type(receive_id_type) is not str or receive_id_type not in {"chat_id", "open_id"}:
        raise MessageError("receive-id-type must be chat_id or open_id")
    _id(receive_id, "receive-id", "oc_" if receive_id_type == "chat_id" else "ou_")
    text, raw = _text_file(text_file)
    ident = _action_id(request_id)
    target_hash, text_hash = digest([receive_id_type, receive_id]), digest(raw)
    resource = "message:" + digest(request_id)
    payload_resource = "message-payload:" + digest([target_hash, text_hash])
    with store.inspection_lock([resource, payload_resource]):
        record = _find(store, ident)
        if record and (record["data"]["request_id"] != request_id or record["data"]["target_sha256"] != target_hash
                       or record["data"]["text_sha256"] != text_hash):
            raise MessageError("request-id already belongs to different target/content; do not replace it")
        if not confirm:
            return {"status": "dry_run", "action_id": ident, "request_id": request_id,
                    "receive_id_type": receive_id_type, "receive_id": receive_id,
                    "text": text, "text_sha256": text_hash, "target_sha256": target_hash,
                    "prior_status": record["data"]["status"] if record else None, "confirm_required": True}
        if record:
            return _receipt(_recover(store, record), no_op=True)
        for prior in store.records(KIND):
            previous = prior["data"]
            if (previous["target_sha256"] == target_hash and previous["text_sha256"] == text_hash
                    and previous["status"] in UNRESOLVED):
                raise MessageError("identical message has an unresolved action " + prior["id"] + "; inspect it, do not replace request-id")
        artifact = _persist_text(store, ident, raw)
        with store.transaction():
            record = store.record(KIND, {"schema": "wa.message.v1", "request_id": request_id,
                "receive_id_type": receive_id_type, "receive_id": receive_id, "target_sha256": target_hash,
                "text_sha256": text_hash, "source_artifact": artifact,
                "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, "wa.outgoing-message:" + request_id)),
                "status": "preparing", "verification_status": "not_sent", "expected_sender_app_id": None,
                "message_id": None, "chat_id": receive_id if receive_id_type == "chat_id" else None,
                "send_acknowledged": False}, ident=ident)
        try:
            client = make_client(dict(settings), transport=transport, base=base, process=process)
            app_id = _id(client.app_id(), "application ID", "cli_")
            client.authenticate()  # All authentication occurs before write-ahead message intent.
            record = _save(store, record, expected_sender_app_id=app_id)
        except BaseException as exc:
            record = _save(store, record, status="failed", verification_status="not_sent",
                           error="authentication_failed_before_send; no message request started")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return _receipt(record)
        record = _save(store, record, status="sending", verification_status="unresolved")
        try:
            body = {"receive_id": receive_id, "msg_type": "text", "content": dumps({"text": text}),
                    "uuid": record["data"]["uuid"]}
            result = client.request("POST", "/im/v1/messages?" + urlencode({"receive_id_type": receive_id_type}), body)
            if type(result) is not dict:
                raise AmbiguousError("invalid send acknowledgement")
            message_id = result.get("message_id")
            if type(message_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", message_id):
                raise AmbiguousError("send acknowledgement missing message ID")
            chat_id = result.get("chat_id") if receive_id_type == "open_id" else receive_id
            if type(chat_id) is not str or not re.fullmatch(r"oc_[A-Za-z0-9_-]{1,200}", chat_id):
                chat_id = None
            record = _save(store, record, message_id=message_id, chat_id=chat_id, send_acknowledged=True,
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


def inspect(store, settings, action_id, transport=http, base=BASE, process=None):
    _settings(settings)
    _id(action_id, "action ID")
    record = _find(store, action_id)
    if record is None:
        raise MessageError("unknown outgoing message action ID")
    with store.inspection_lock(["message:" + digest(record["data"]["request_id"])]):
        record = _recover(store, store.get(action_id))
        if record["data"]["status"] == "resolved_absent" or not record["data"].get("message_id"):
            return _receipt(record, no_op=True)
        client = make_client(dict(settings), transport=transport, base=base, process=process)
        if client.app_id() != record["data"]["expected_sender_app_id"]:
            raise MessageError("inspect requires the original application identity")
        return _receipt(_readback(store, record, client))


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
    command.add_argument("--text-file", required=True)
    modes = command.add_mutually_exclusive_group()
    modes.add_argument("--confirm", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    command = commands.add_parser("inspect", help="Read the known sent message only; never resend")
    command.add_argument("action_id")
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
                                      args.text_file, confirm=args.confirm)
                    elif args.command == "resolve":
                        result = resolve(store, args.action_id, absent=args.absent, reason=args.reason, confirm=args.confirm)
                    else:
                        result = inspect(store, conf["publisher"], args.action_id)
                finally:
                    store.close()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 3 if result["status"] in {"outcome_unknown", "verification_pending"} else 2 if result["status"] == "failed" else 0
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
