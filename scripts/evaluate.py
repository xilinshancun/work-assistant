#!/usr/bin/env python3
"""Evaluate the real CLI using only isolated synthetic data and Python stdlib."""
import argparse
from contextlib import closing
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

SCHEMA = "wa.evaluation.v1"
ARTIFACTS = ("index.html", "daily-sync.md", "evidence.html", "evidence.json",
             "card.json", "document.json", "card-preview.html")
SOURCE = Path(__file__).resolve().parents[1]
MAX_BYTES = 4 * 1024 * 1024

# -S skips site/user startup customization. This fixed bootstrap runs the real
# __main__ entry, with Python audit checks against accidental network/child calls.
# It is an evaluation guard, not an OS sandbox or an adversarial security audit.
BOOTSTRAP = r'''
import json, os, pathlib, runpy, sys
marker = pathlib.Path(os.environ["WA_EVAL_MARKER"])
blocked = pathlib.Path(os.environ["WA_EVAL_BLOCKED"])
def audit(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn"}:
        blocked.write_text(json.dumps({"event": event}), encoding="utf-8")
        raise PermissionError("offline evaluation forbids network and external child processes")
sys.addaudithook(audit)
marker.write_text("active", encoding="utf-8")
sys.argv = ["work_assistant"] + sys.argv[1:]
runpy.run_module("work_assistant", run_name="__main__")
'''


class EvaluationFailure(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise EvaluationFailure(code, message)


def strict_json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value
    def constant(_):
        raise ValueError("non-finite JSON")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def environment(root):
    """Complete replacement: no inherited HOME, auth, proxies or provider paths."""
    directories = {name: root / name for name in ("home", "config-home", "cache", "tmp", "empty-path", "guards")}
    for path in directories.values():
        path.mkdir(mode=0o700)
    env = {"HOME": str(directories["home"]), "USERPROFILE": str(directories["home"]),
           "XDG_CONFIG_HOME": str(directories["config-home"]), "XDG_CACHE_HOME": str(directories["cache"]),
           "APPDATA": str(directories["config-home"]), "LOCALAPPDATA": str(directories["cache"]),
           "PATH": str(directories["empty-path"]), "PYTHONPATH": str(SOURCE),
           "TMPDIR": str(directories["tmp"]), "TMP": str(directories["tmp"]), "TEMP": str(directories["tmp"]),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8"}
    if os.name == "nt":
        # Windows needs its OS library directory; no user/provider settings pass through.
        for key in ("SystemRoot", "WINDIR"):
            if key in os.environ:
                env[key] = os.environ[key]
    return env


def run_cli(root, env, label, args):
    marker, blocked = root / "guards" / (label + ".active"), root / "guards" / (label + ".blocked")
    child_env = dict(env, WA_EVAL_MARKER=str(marker), WA_EVAL_BLOCKED=str(blocked))
    # Output stays private and bounded when read. Raw stderr/stdout is never
    # copied to the receipt: an unexpected CLI error cannot dump secrets there.
    with tempfile.TemporaryFile(dir=root / "tmp") as stdout, tempfile.TemporaryFile(dir=root / "tmp") as stderr:
        try:
            result = subprocess.run([sys.executable, "-S", "-c", BOOTSTRAP] + list(args),
                cwd=root, env=child_env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                shell=False, timeout=60)
        except subprocess.TimeoutExpired:
            raise EvaluationFailure("cli_timeout", "CLI step %s exceeded 60 seconds; no retry was attempted." % label) from None
        require(marker.is_file() and marker.read_text(encoding="utf-8") == "active", "guard_not_active",
                "The isolated CLI evaluation guard did not initialize for step " + label)
        require(not blocked.exists(), "forbidden_operation", "CLI step %s attempted a network/child-process operation; it was blocked." % label)
        require(result.returncode == 0, "cli_exit", "CLI step %s exited with code %s; raw output is withheld." % (label, result.returncode))
        stdout.seek(0);raw = stdout.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "cli_output_limit", "CLI step " + label + " exceeded the JSON output limit.")
    try:
        value = strict_json(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise EvaluationFailure("invalid_cli_json", "CLI step " + label + " did not return readable strict UTF-8 JSON.") from None
    require(type(value) is dict, "invalid_cli_result", "CLI step " + label + " did not return an object.")
    return value


class ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = set();self.text = [];self.hidden = 0

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        if tag in {"style", "script"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"style", "script"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def report_artifacts(path, allowed):
    path = Path(path)
    require(path.is_absolute() and path.resolve().is_relative_to(allowed.resolve()),
            "report_location", "CLI report path must stay inside its synthetic output directory.")
    hashes = {}
    for name in ARTIFACTS:
        artifact = path / name
        require(not artifact.is_symlink() and artifact.is_file(), "missing_artifact", "Required regular report artifact missing: " + name)
        require(artifact.resolve().is_relative_to(allowed.resolve()), "artifact_location", "Report artifact escaped the synthetic directory: " + name)
        with artifact.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        require(0 < len(raw) <= MAX_BYTES, "artifact_size", "Empty or oversized report artifact: " + name)
        try:
            text = raw.decode("utf-8")
            if name.endswith(".json"):
                require(type(strict_json(text)) is dict, "artifact_json", "Report JSON must be an object: " + name)
            elif name.endswith(".html"):
                parser = ReadableHTML();parser.feed(text);parser.close()
                require({"html", "title"} <= parser.tags and bool("".join(parser.text).strip()),
                        "artifact_html", "Report HTML lacks a readable document/title: " + name)
        except (ValueError, UnicodeError):
            raise EvaluationFailure("artifact_parse", "Report artifact is not readable UTF-8/HTML/JSON: " + name) from None
        hashes[name] = hashlib.sha256(raw).hexdigest()
    return hashes


def demo_check(root, env, name):
    target = root / name
    value = run_cli(root, env, name, ["demo", "--output", str(target)])
    require(value.get("preview") == "dry_run" and "synthetic" in str(value.get("mode", "")).lower(),
            "demo_scope", "Demo must identify synthetic data and dry-run publication.")
    report_hash = value.get("hash")
    require(type(report_hash) is str and re.fullmatch(r"[0-9a-f]{64}", report_hash) is not None,
            "demo_hash", "Demo did not return a SHA-256 report hash.")
    hashes = report_artifacts(value.get("report_path", ""), target)
    return {"report_hash": report_hash, "artifact_sha256": hashes,
            "report_path": str(Path(value["report_path"]).relative_to(root)), "artifact_count": len(hashes)}


def lifecycle_check(root, env):
    flow = root / "lifecycle";flow.mkdir(mode=0o700)
    workspace = flow / "workspace";workspace.mkdir()
    (workspace / "README.txt").write_text("Synthetic provider-free evaluation workspace.\n", encoding="utf-8")
    settings = flow / "config.json"
    value = {"state_dir": str(flow / "state"), "providers": {name: {"argv": [str(flow / "absent-providers" / name)], "env": {}}
             for name in ("dsh", "codex", "claude_code")}, "publisher": {"mode": "app", "transport": "http", "grants": []}}
    with settings.open("x", encoding="utf-8") as stream:
        json.dump(value, stream)
    settings.chmod(0o600)
    commands = []
    def cli(label, *args):
        result = run_cli(root, env, "flow-" + label, ["--config", str(settings)] + list(args))
        commands.append(label)
        return result
    require(cli("init", "init").get("status") == "initialized", "init_status", "Provider-free init did not initialize.")
    doctor = cli("doctor", "doctor", "--json")
    require(all(doctor.get(name, {}).get("installed") is False for name in value["providers"]),
            "unexpected_provider", "Doctor must find none of the deliberately absent synthetic providers.")
    require(doctor.get("workflows", {}).get("offline", {}).get("readiness") == "available"
            and doctor.get("feishu", {}).get("configured") is False, "offline_readiness",
            "Doctor must keep offline workflows available without configured Feishu/providers.")
    project = cli("project", "project", "add", "evaluation", "--workspace", str(workspace))
    require(project.get("grants") == ["read"], "project_authority", "Evaluation project must remain read-only.")
    task = cli("task", "task", "add", project["id"], "Inspect the synthetic evaluation fixture")
    shown = cli("task-show", "task", "show", task["id"])
    require(shown.get("task", {}).get("status") == "queued" and shown["task"].get("verified") == 0
            and shown["task"].get("goal_status") == "unverified",
            "task_status", "Unexecuted synthetic task must remain queued and unverified.")
    report = cli("report", "report", project["id"], "--date", "2026-01-15")
    hashes = report_artifacts(report["data"]["path"], flow)
    dry = cli("publish-dry-run", "publish", report["id"], "--dry-run")
    require(dry.get("status") == "dry_run" and dry.get("hash") == report["data"]["hash"],
            "dry_run_result", "Publish must return a dry-run preview of the same report.")
    database = flow / "state" / "state.sqlite3"
    require(database.is_file() and not database.is_symlink(), "state_database", "Synthetic lifecycle database is missing.")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        effects = db.execute("SELECT COUNT(*) FROM records WHERE kind IN ('route','run','publication')").fetchone()[0]
    require(effects == 0, "unexpected_records", "Provider-free flow must not create route, run or publication records.")
    return {"commands": commands, "providers_installed": False, "task_status": "queued", "task_verified": False,
            "publish_status": "dry_run", "route_run_publication_records": effects, "artifact_count": len(hashes)}


def evaluate(output=None):
    receipt = {"schema": SCHEMA, "status": "failed", "checks": [],
        "scope": {"data": "synthetic", "live_tested": False, "cli": "real work_assistant.__main__ under isolated stdlib Python",
                  "guard": "Python audit guard rejects socket and external child-process operations"},
        "limitations": ["No live provider, Feishu or Paseo authentication, delivery or execution is validated.",
                        "HTML checks establish readable local artifacts, not visual/browser correctness.",
                        "This is an offline CLI evaluation, not an OS sandbox or a security audit."],
        "output_dir": None, "artifacts_retained": False}
    try:
        if output is None:
            root = Path(tempfile.mkdtemp(prefix="wa-evaluation-")).resolve()
        else:
            root = Path(output).expanduser().absolute()
            root.mkdir(mode=0o700, parents=True, exist_ok=False)
            root = root.resolve()
    except (OSError, ValueError, RuntimeError):
        receipt["checks"].append({"id": "output_directory", "status": "failed", "code": "output_unavailable",
                                  "diagnostic": "Output must be a new writable directory; existing output is never overwritten."})
        receipt["summary"] = {"passed": 0, "failed": 1, "skipped": 0}
        return receipt
    results = {}
    def check(name, function):
        try:
            details = function();results[name] = details
            receipt["checks"].append({"id": name, "status": "passed", "details": details})
        except EvaluationFailure as exc:
            receipt["checks"].append({"id": name, "status": "failed", "code": exc.code, "diagnostic": str(exc)})
        except Exception as exc:
            receipt["checks"].append({"id": name, "status": "failed", "code": "evaluation_error",
                "diagnostic": "Evaluation step failed with %s; exception payload withheld." % type(exc).__name__})
    try:
        env = environment(root)
        check("demo_a", lambda: demo_check(root, env, "demo-a"))
        check("demo_b", lambda: demo_check(root, env, "demo-b"))
        if "demo_a" in results and "demo_b" in results:
            def deterministic():
                require(results["demo_a"]["report_hash"] == results["demo_b"]["report_hash"],
                        "report_hash_mismatch", "Independent demo report hashes differ.")
                require(results["demo_a"]["artifact_sha256"] == results["demo_b"]["artifact_sha256"],
                        "artifact_hash_mismatch", "Independent demo artifact bytes differ.")
                return {"report_hash": results["demo_a"]["report_hash"], "equal_artifacts": len(ARTIFACTS)}
            check("demo_determinism", deterministic)
        else:
            receipt["checks"].append({"id": "demo_determinism", "status": "skipped", "diagnostic": "Both readable demos are required."})
        check("provider_free_lifecycle", lambda: lifecycle_check(root, env))
    except (Exception, KeyboardInterrupt) as exc:
        receipt["checks"].append({"id": "evaluation", "status": "failed", "code": "evaluation_interrupted",
                                 "diagnostic": "Evaluation could not continue (%s); no automatic retry." % type(exc).__name__})
    receipt["summary"] = {status: sum(c["status"] == status for c in receipt["checks"]) for status in ("passed", "failed", "skipped")}
    receipt["status"] = "passed" if not receipt["summary"]["failed"] and not receipt["summary"]["skipped"] else "failed"
    retained = output is not None or receipt["status"] == "failed"
    if retained:
        receipt.update(output_dir=str(root), artifacts_retained=True)
        try:
            with (root / "receipt.json").open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2);stream.write("\n")
            (root / "receipt.json").chmod(0o600)
        except OSError:
            receipt["status"] = "failed"
            receipt["checks"].append({"id": "receipt", "status": "failed", "code": "receipt_write", "diagnostic": "Could not save the evaluation receipt."})
            receipt["summary"]["failed"] += 1
    else:
        shutil.rmtree(root)
        for item in receipt["checks"]:
            if "report_path" in item.get("details", {}):
                item["details"]["report_path"] = None
    return receipt


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise EvaluationFailure("arguments", "Invalid arguments; use --help for supported evaluation options.")


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--output", help="Retain synthetic artifacts and receipt.json in a NEW directory. Without it, success is cleaned; failure is retained.")
    try:
        args = parser.parse_args(argv);receipt = evaluate(args.output)
    except EvaluationFailure as exc:
        receipt = {"schema": SCHEMA, "status": "failed", "checks": [{"id": "arguments", "status": "failed", "code": exc.code, "diagnostic": str(exc)}],
                   "scope": {"data": "synthetic", "live_tested": False}, "limitations": ["Evaluation did not run."],
                   "summary": {"passed": 0, "failed": 1, "skipped": 0}, "output_dir": None, "artifacts_retained": False}
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
