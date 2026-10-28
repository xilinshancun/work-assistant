import json
import math
from pathlib import Path
from .util import digest, now, atomic_write, uid

GRADES = {"measured", "reproduced", "derived", "code_observed", "reported"}


def add_evidence(store, project, source, scope, origin="manual", values=None):
    p = store.project(project)
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise ValueError("evidence source must be an existing file")
    if not scope.strip() or not origin.strip():
        raise ValueError("scope and origin required")
    raw = source.read_bytes()
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("evidence file exceeds 20 MiB")
    if values is None and source.suffix.lower() == ".json":
        value = json.loads(raw.decode("utf-8"))
        values = value.get("values", {}) if type(value) is dict else {}
    values = {} if values is None else values
    if type(values) is not dict:
        raise ValueError("values must be an object")
    for key, value in values.items():
        if (type(key) is not str or type(value) is not dict or set(value) != {"value", "unit"}
                or type(value["value"]) not in (int, float) or not math.isfinite(value["value"])
                or type(value["unit"]) is not str):
            raise ValueError("each metric requires finite value and explicit unit")
    ident = uid("evidence")
    artifact = store.root / "evidence" / (ident + ".bin")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    # Copy exact raw bytes; never modify the original source.
    with open(artifact, "xb") as f:
        f.write(raw)
    artifact.chmod(0o600)
    return store.record("evidence", {"source": str(source), "captured_at": now(), "scope": scope,
        "origin": origin, "hash": digest(raw), "artifact": str(artifact), "values": values}, project_id=p["id"], ident=ident)


def verify_evidence(record):
    path = Path(record["data"]["artifact"])
    if not path.is_file() or digest(path.read_bytes()) != record["data"]["hash"]:
        raise ValueError("evidence artifact missing or hash mismatch: " + record["id"])


def add_claim(store, project, text, evidence_ids, grade="reported", attribution="operator", numeric=None,
              derivation=None, inputs=None, task_id=None):
    p = store.project(project)
    if grade not in GRADES or not text.strip() or not attribution.strip():
        raise ValueError("claim requires valid grade, text and attribution")
    if not evidence_ids:
        raise ValueError("claim requires source evidence")
    sources = []
    for ident in evidence_ids:
        e = store.get(ident)
        if e["kind"] != "evidence" or e["project_id"] != p["id"]:
            raise ValueError("claim source must be evidence from the same project")
        verify_evidence(e)
        sources.append(e)
    if grade == "derived":
        if not derivation or not inputs:
            raise ValueError("derived claim requires formula/derivation and input claim IDs")
        for ident in inputs:
            previous = store.get(ident)
            if previous["kind"] != "claim" or previous["project_id"] != p["id"] or previous["data"]["status"] != "active":
                raise ValueError("invalid derived input")
    if numeric is not None:
        if (type(numeric) is not dict or set(numeric) != {"key", "value", "unit"}
                or type(numeric["value"]) not in (float, int) or not math.isfinite(numeric["value"])):
            raise ValueError("numeric assertion requires key/value/unit")
        matches = [e["data"]["values"].get(numeric["key"]) for e in sources]
        if not any(v and v["value"] == numeric["value"] and v["unit"] == numeric["unit"] for v in matches):
            raise ValueError("numeric assertion differs from supplied source values/units")
    if task_id and store.task(task_id)["project_id"] != p["id"]:
        raise ValueError("task belongs to another project")
    return store.record("claim", {"text": text, "evidence_ids": evidence_ids, "grade": grade,
        "attribution": attribution, "numeric": numeric, "derivation": derivation, "inputs": inputs or [],
        "status": "active", "verification_scope": "source existence/hash and supplied values checked; scientific reproduction not implied"}, task_id, p["id"])


def change_claim(store, ident, action, reason, replacement=None):
    record = store.get(ident)
    if record["kind"] != "claim" or record["data"]["status"] != "active":
        raise ValueError("expected an active claim")
    if action not in {"supersede", "retract"} or not reason.strip():
        raise ValueError("claim correction requires action and reason")
    if action == "supersede":
        other = store.get(replacement)
        if other["kind"] != "claim" or other["project_id"] != record["project_id"] or other["data"]["status"] != "active" or other["id"] == ident:
            raise ValueError("invalid replacement claim")
    with store.transaction():
        data = dict(record["data"], status="superseded" if action == "supersede" else "retracted", reason=reason, replacement=replacement)
        store.update_record(ident, data)
        return store.record("correction", {"claim_id": ident, "action": action, "reason": reason,
            "replacement": replacement}, record["task_id"], record["project_id"])


def ingest(store, config, project, path, scope):
    path = Path(path).expanduser().resolve()
    if str(path) not in config["ingest_paths"]:
        raise ValueError("ingest requires an exact path in configured ingest_paths")
    return add_evidence(store, project, path, scope, origin="configured report ingestion")
