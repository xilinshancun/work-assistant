"""Explicit bot acknowledgements, with durable intent and GET-only recovery."""
import json
import sqlite3
import sys
from urllib.parse import quote, urlencode

from . import config, messages
from .feishu import APIError, AmbiguousError
from .lark_cli import LarkCLI
from .process import TerminationRequested, termination_guard
from .store import Busy, Store
from .util import digest, now

KIND = "message_reaction"
EMOJIS = ("Get", "OK", "OnIt")
UNRESOLVED = {"preparing", "sending", "outcome_unknown", "verification_pending"}


class ReactionError(ValueError):
    pass


def validate(settings, chat_id, message_id, emoji):
    if settings.get("mode") != "app" or settings.get("transport") != "lark_cli":
        raise ReactionError("reactions require app mode with official lark_cli bot transport")
    messages._id(settings.get("expected_app_id"), "reaction application ID", "cli_")
    messages._id(chat_id, "reaction chat ID", "oc_")
    messages._id(message_id, "reaction source message ID", "om_")
    if type(emoji) is not str or emoji not in EMOJIS:
        raise ReactionError("ack reaction must be Get, OK or OnIt")


def action_id(request_id):
    return "reaction_" + digest(messages._request(request_id))[:32]


def _find(store, ident):
    if not store.db.execute("SELECT 1 FROM records WHERE id=?", (ident,)).fetchone():
        return None
    record = store.get(ident)
    if record["kind"] != KIND:
        raise ReactionError("action is not a reaction")
    return record


def _receipt(record, no_op=False):
    fields = ("request_id", "status", "verification_status", "expected_app_id", "identity", "chat_id",
              "message_id", "emoji_type", "target_hash", "reaction_id", "post_intent", "adopted_existing",
              "verification_basis", "error", "verified_at", "last_readback")
    return dict({key: record["data"].get(key) for key in fields}, action_id=record["id"], no_op=no_op)


def _recover(store, record):
    if record["data"]["status"] == "sending":
        return messages._save(store, record, status="outcome_unknown", verification_status="unresolved",
                              error="interrupted_reaction_intent; inspect only, never repeat POST")
    if record["data"]["status"] == "preparing":
        return messages._save(store, record, status="failed", verification_status="not_added",
                              error="interrupted_before_reaction_intent; no POST started")
    return record


def _path(message_id):
    return "/im/v1/messages/" + quote(message_id, safe="") + "/reactions"


def _item(value):
    if type(value) is not dict or type(value.get("operator")) is not dict or type(value.get("reaction_type")) is not dict:
        raise AmbiguousError("invalid reaction record")
    ident = messages._id(value.get("reaction_id"), "reaction ID")
    operator, emoji = value["operator"], value["reaction_type"].get("emoji_type")
    if operator.get("operator_type") not in ("app", "user") or type(emoji) is not str or not emoji or len(emoji) > 100:
        raise AmbiguousError("invalid reaction operator/type")
    operator_id = messages._id(operator.get("operator_id"), "reaction operator ID")
    return {"reaction_id": ident, "operator_type": operator["operator_type"], "operator_id": operator_id, "emoji_type": emoji}


def _matches(client, message_id, app_id, emoji):
    matches, seen_ids, seen_tokens, token = [], set(), set(), None
    for _ in range(100):
        query = {"reaction_type": emoji, "page_size": "50"}
        if token:
            query["page_token"] = token
        page = client.request("GET", _path(message_id) + "?" + urlencode(query))
        if type(page) is not dict or type(page.get("items")) is not list or type(page.get("has_more")) is not bool:
            raise AmbiguousError("invalid reaction pagination; no partial adoption")
        for raw in page["items"]:
            item = _item(raw)
            if item["reaction_id"] in seen_ids:
                raise AmbiguousError("duplicate reaction ID across pages")
            seen_ids.add(item["reaction_id"])
            if len(seen_ids) > 10000:
                raise AmbiguousError("reaction list exceeds bound")
            if item["operator_type"] == "app" and item["operator_id"] == app_id and item["emoji_type"] == emoji:
                matches.append(item)
        if not page["has_more"]:
            return sorted(matches, key=lambda item: item["reaction_id"])
        token = page.get("page_token")
        if type(token) is not str or not token or len(token) > 4096 or token in seen_tokens:
            raise AmbiguousError("missing/repeated reaction page token")
        seen_tokens.add(token)
    raise AmbiguousError("reaction pagination exceeds bound")


def list_reactions(settings, chat_id, message_id, emoji="Get", process=None):
    """List only this configured bot's matching emoji; no mutation or raw output."""
    validate(settings, chat_id, message_id, emoji)
    client = LarkCLI(settings, process=process)
    client.authenticate()
    messages._reply_source(client, message_id, chat_id)
    return {"status": "completed", "mode": "read_only", "identity": "bot", "expected_app_id": client.app_id(),
            "chat_id": chat_id, "message_id": message_id, "emoji_type": emoji,
            "items": _matches(client, message_id, client.app_id(), emoji)}


def add(store, settings, request_id, chat_id, message_id, emoji="Get", confirm=False, process=None):
    validate(settings, chat_id, message_id, emoji)
    if type(confirm) is not bool:
        raise ReactionError("confirm must be an explicit boolean")
    ident = action_id(request_id)
    app = settings["expected_app_id"]
    target = digest(["reaction", "bot", app, chat_id, message_id, emoji])
    with store.inspection_lock(["reaction-request:" + digest(request_id), "reaction-source:" + target]):
        record = _find(store, ident)
        if record and (record["data"]["request_id"] != request_id or record["data"]["target_hash"] != target):
            raise ReactionError("request-id belongs to a different reaction source/app/emoji")
        if not confirm:
            return {"status": "dry_run", "action_id": ident, "request_id": request_id, "identity": "bot",
                    "chat_id": chat_id, "message_id": message_id, "emoji_type": emoji, "expected_app_id": app,
                    "target_hash": target, "confirm_required": True}
        if record:
            return _receipt(_recover(store, record), no_op=True)
        if any(r["data"]["target_hash"] == target and r["data"]["status"] in UNRESOLVED for r in store.records(KIND)):
            raise ReactionError("matching reaction action unresolved; inspect original, never replace request-id")
        with store.transaction():
            record = store.record(KIND, {"schema": "wa.reaction.v1", "request_id": request_id,
                "identity": "bot", "expected_app_id": app, "chat_id": chat_id, "message_id": message_id,
                "emoji_type": emoji, "target_hash": target, "status": "preparing", "verification_status": "not_added",
                "reaction_id": None, "post_intent": None}, ident=ident)
        try:
            client = LarkCLI(settings, process=process)
            client.authenticate()
            source = messages._reply_source(client, message_id, chat_id)
            matches = _matches(client, message_id, app, emoji)
            record = messages._save(store, record, source=source)
        except BaseException as exc:
            record = messages._save(store, record, status="failed", verification_status="not_added",
                                    error="reaction_preflight_failed; no reaction POST started")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return _receipt(record)
        if matches:
            return _receipt(messages._save(store, record, status="completed", verification_status="verified",
                reaction_id=matches[0]["reaction_id"], adopted_existing=True, verified_at=now(),
                verification_basis="existing_app_emoji_observed_in_complete_list"))
        body = {"reaction_type": {"emoji_type": emoji}}
        intent = {"method": "POST", "path": _path(message_id), "body_hash": digest(body), "at": now()}
        record = messages._save(store, record, status="sending", verification_status="unresolved", post_intent=intent)
        try:
            item = _item(client.request("POST", _path(message_id), body))
            if item["operator_type"] != "app" or item["operator_id"] != app or item["emoji_type"] != emoji:
                raise AmbiguousError("reaction acknowledgement identity/type mismatch")
            record = messages._save(store, record, reaction_id=item["reaction_id"], status="verification_pending")
            matches = _matches(client, message_id, app, emoji)
            if not any(match["reaction_id"] == item["reaction_id"] for match in matches):
                raise AmbiguousError("acknowledged reaction not found in complete readback")
            return _receipt(messages._save(store, record, status="completed", verification_status="verified",
                adopted_existing=False, verified_at=now(), verification_basis="acknowledged_app_emoji_and_same_list_id", error=None))
        except BaseException as exc:
            record = messages._save(store, record, status="outcome_unknown", verification_status="unresolved",
                                    error="reaction_outcome_unknown; inspect only, never automatically repeat POST")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return _receipt(record)


def inspect(store, settings, ident, process=None):
    record = _find(store, messages._id(ident, "reaction action ID"))
    if record is None:
        raise ReactionError("unknown reaction action")
    data = record["data"]
    validate(settings, data["chat_id"], data["message_id"], data["emoji_type"])
    if settings["expected_app_id"] != data["expected_app_id"]:
        raise ReactionError("inspect requires the original reaction application")
    with store.inspection_lock(["reaction-request:" + digest(data["request_id"]), "reaction-source:" + data["target_hash"]]):
        record = _recover(store, store.get(ident));data = record["data"]
        historical_verified = data["status"] == "completed" and data["verification_status"] == "verified"
        observation = {"at": now(), "confirmed": False, "status": "unavailable"}
        try:
            result = list_reactions(settings, data["chat_id"], data["message_id"], data["emoji_type"], process=process)
            matches = [item for item in result["items"] if not data.get("reaction_id") or item["reaction_id"] == data["reaction_id"]]
            if matches:
                observation.update(confirmed=True, status="matched")
                if not historical_verified:
                    record = messages._save(store, record, status="completed", verification_status="verified",
                        reaction_id=matches[0]["reaction_id"], verified_at=now(), error=None,
                        verification_basis="read_only_app_emoji_list_observation")
            else:
                observation["status"] = "not_observed"
                if not historical_verified:
                    record = messages._save(store, record, status="outcome_unknown" if data.get("post_intent") else data["status"],
                        verification_status="unresolved", error="reaction_not_observed; no retry or absence claim")
        except (APIError, AmbiguousError, ValueError, OSError):
            if not historical_verified:
                record = messages._save(store, record, status="outcome_unknown" if data.get("post_intent") else data["status"],
                    verification_status="unresolved", error="reaction_inspection_unavailable; no retry")
        record = messages._save(store, record, last_readback=observation)
        with store.transaction():
            store.record("reaction_observation", {"action_id": ident, "status": record["data"]["status"],
                "reaction_id": record["data"].get("reaction_id"), "verification_status": record["data"]["verification_status"],
                "readback": observation})
        return _receipt(record)


def parser():
    result = messages.MessageParser(prog="python3 -m work_assistant.reactions", description=__doc__)
    result.add_argument("--config", default="config.local.json")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("add", "list"):
        command = commands.add_parser(name)
        command.add_argument("--chat-id", required=True)
        command.add_argument("--message-id", required=True)
        command.add_argument("--emoji", choices=EMOJIS, default="Get")
        if name == "add":
            command.add_argument("--request-id", required=True)
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--confirm", action="store_true")
            mode.add_argument("--dry-run", action="store_true")
    commands.add_parser("inspect").add_argument("action_id")
    return result


def main(argv=None):
    store = None
    try:
        with termination_guard():
            args = parser().parse_args(argv);conf = config.load(args.config)
            if args.command == "list":
                result = list_reactions(conf["publisher"], args.chat_id, args.message_id, args.emoji)
            else:
                store = Store(conf["state_dir"])
                if args.command == "inspect":
                    result = inspect(store, conf["publisher"], args.action_id)
                else:
                    result = add(store, conf["publisher"], args.request_id, args.chat_id, args.message_id, args.emoji, confirm=args.confirm)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 3 if result["status"] in {"outcome_unknown", "verification_pending"} else 2 if result["status"] == "failed" else 0
    except (ReactionError, messages.MessageError, Busy) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}));return 2
    except (APIError, AmbiguousError, ValueError, OSError, sqlite3.Error):
        print(json.dumps({"status": "failed", "error": "reaction request/configuration unavailable; no automatic retry"}));return 2
    except TerminationRequested:
        print(json.dumps({"status": "interrupted", "error": "inspect original reaction before any new authorization"}));return 143
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "error": "inspect original reaction before any new authorization"}));return 130
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
