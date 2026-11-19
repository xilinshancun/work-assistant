#!/usr/bin/env python3
"""Prepare a Feishu card from a synchronized timeline section without contacting Feishu."""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from work_assistant.config import load
from work_assistant.feishu import document_card
from work_assistant.message_cards import parse_card
from work_assistant.messages import MAX_TEXT_BYTES
from work_assistant.reports import load_report
from work_assistant.util import digest, dumps


class Snapshot:
    """Record lookup used by load_report inside one read-only SQLite transaction."""

    def __init__(self, db):
        self.db = db

    def get(self, ident):
        row = self.db.execute("SELECT * FROM records WHERE id = ?", (ident,)).fetchone()
        if row is None:
            raise ValueError("unknown report or binding")
        return dict(dict(row), data=json.loads(row["data"]))


def keep_file(path, content):
    # A crash during a write cannot leave a partial file at its final name.
    with tempfile.NamedTemporaryFile(dir=path.parent) as staged:
        staged.write(content)
        staged.flush()
        os.fsync(staged.fileno())
        try:
            os.link(staged.name, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise ValueError("existing prepared artifact differs")


def prepare(config_path, report_id, binding_id, document_id):
    if not Path(config_path).expanduser().is_file() or not document_id:
        raise ValueError("supply the shared configuration and expected document ID")
    root = Path(load(config_path)["state_dir"])
    database = (root / "state.sqlite3").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        snapshot = Snapshot(db)
        report = load_report(snapshot, report_id)
        binding = snapshot.get(binding_id)
        data, state = report["data"], binding["data"]
        section = state.get("sections", {}).get(data["period"], {})
        if (binding["kind"] != "doc_binding" or binding["project_id"] != report["project_id"]
                or state.get("document_id") != document_id or state.get("status") != "completed"
                or state.get("pending") or state.get("operation") or state.get("layout_lock")
                or state.get("rename_request") or section.get("report_id") != report_id
                or section.get("report_hash") != data["hash"]):
            raise ValueError("the fixed document must acknowledge this exact report")

        original = (Path(data["path"]) / "card.json").read_bytes()
        if digest(original) != data["files"]["card.json"]:
            raise ValueError("report card changed")
        card = document_card(json.loads(original), document_id)
        buttons = [button for e in card["elements"] if e["tag"] == "action" for button in e["actions"]]
        if len(buttons) != 1 or buttons[0].get("url") != "https://www.feishu.cn/docx/" + document_id:
            raise ValueError("expected one button pointing to the bound document")
        raw = (dumps(card) + "\n").encode("utf-8")
        if len(raw) > MAX_TEXT_BYTES:
            raise ValueError("card exceeds outgoing message size limit")
        parse_card(raw.decode("utf-8"))

    card_hash = digest(raw)
    output = root / "prepared-daily-cards" / digest([report_id, binding_id]) / card_hash
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = {"status": "prepared_not_sent", "external_requests": 0, "report_id": report_id,
                "report_hash": data["hash"], "source_hashes": data.get("source_hashes"),
                "date": data["period"], "document_id": document_id, "binding_id": binding_id,
                "card_file": str(output / "card.json"), "card_sha256": card_hash}
    keep_file(output / "card.json", raw)
    keep_file(output / "manifest.json", (dumps(manifest) + "\n").encode("utf-8"))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("report_id")
    args = parser.parse_args()
    try:
        result = prepare(args.config, args.report_id, args.binding_id, args.document_id)
    except (ValueError, OSError, KeyError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
