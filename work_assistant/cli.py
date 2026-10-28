import argparse
import json
import sqlite3
import sys
from pathlib import Path
from . import __version__
from . import config
from .store import Store
from .executors import Providers
from .routing import route_task
from .runner import run_task
from .evidence import add_evidence, add_claim, change_claim, ingest
from .reports import generate_report
from .feishu import preview, publish, reconcile
from .demo import demo
from .util import redact


def parser():
    p = argparse.ArgumentParser(prog="wa", description="Local-first work assistant / 本地工作助理")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", default="config.local.json")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    d = commands.add_parser("demo")
    d.add_argument("--output", default="out/demo")
    project = commands.add_parser("project").add_subparsers(dest="operation", required=True)
    project.add_parser("list")
    a = project.add_parser("add")
    a.add_argument("name")
    a.add_argument("--workspace", required=True)
    a.add_argument("--allow", action="append", choices=["read", "write_workspace", "publish", "external"])
    task = commands.add_parser("task").add_subparsers(dest="operation", required=True)
    a = task.add_parser("add")
    a.add_argument("project")
    a.add_argument("goal")
    a.add_argument("--intent", choices=["read", "write_workspace"], default="read")
    a.add_argument("--provider", choices=["codex", "claude_code"])
    a.add_argument("--blockers", default="")
    a = task.add_parser("list")
    a.add_argument("--project")
    a = task.add_parser("show")
    a.add_argument("id")
    for command in ("route", "run", "review"):
        a = commands.add_parser(command)
        a.add_argument("id")
        if command == "route":
            a.add_argument("--no-cache", action="store_true")
        if command == "run":
            a.add_argument("--followup", help="Explicit new action in saved provider session")
    evidence = commands.add_parser("evidence").add_subparsers(dest="operation", required=True)
    for command in ("add", "ingest"):
        a = evidence.add_parser(command)
        a.add_argument("project")
        a.add_argument("path")
        a.add_argument("--scope", required=True)
        if command == "add":
            a.add_argument("--origin", default="manual")
    claim = commands.add_parser("claim").add_subparsers(dest="operation", required=True)
    a = claim.add_parser("add")
    a.add_argument("project")
    a.add_argument("text")
    a.add_argument("--evidence", action="append", required=True)
    a.add_argument("--grade", default="reported", choices=["measured", "reproduced", "derived", "code_observed", "reported"])
    a.add_argument("--attribution", default="operator")
    a.add_argument("--numeric", type=json.loads, help='JSON {"key":"samples","value":10,"unit":"items"}')
    a.add_argument("--derivation")
    a.add_argument("--input", action="append")
    for command in ("supersede", "retract"):
        a = claim.add_parser(command)
        a.add_argument("id")
        a.add_argument("--reason", required=True)
        if command == "supersede":
            a.add_argument("--replacement", required=True)
    a = commands.add_parser("report")
    a.add_argument("project")
    a.add_argument("--date", required=True)
    a.add_argument("--output")
    a = commands.add_parser("publish")
    a.add_argument("id")
    modes = a.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--confirm", action="store_true")
    a.add_argument("--retry", action="store_true")
    a = commands.add_parser("reconcile")
    a.add_argument("id")
    a.add_argument("--resolution")
    a.add_argument("--reason")
    a.add_argument("--remote-ids", type=json.loads)
    return p


def dispatch(args, conf, store):
    c = args.command
    if c == "project":
        if args.operation == "list":
            return store.projects()
        # Relative workspaces follow config location, like every other configured path.
        workspace = config.resolve_path(args.workspace, Path(conf["config_path"]).parent)
        return store.add_project(args.name, workspace, ["read"] if args.allow is None else args.allow)
    if c == "task":
        if args.operation == "add":
            return store.add_task(args.project, args.goal, args.intent, args.provider, args.blockers)
        if args.operation == "list":
            return store.tasks(args.project)
        return {"task": store.task(args.id), "history": store.records(task_id=args.id)}
    if c == "route":
        return route_task(store, Providers(conf), args.id, use_cache=not args.no_cache)
    if c in {"run", "review"}:
        return run_task(store, Providers(conf), args.id, followup=getattr(args, "followup", None), review=c == "review")
    if c == "evidence":
        if args.operation == "ingest":
            return ingest(store, conf, args.project, args.path, args.scope)
        return add_evidence(store, args.project, args.path, args.scope, args.origin)
    if c == "claim":
        if args.operation == "add":
            return add_claim(store, args.project, args.text, args.evidence, args.grade, args.attribution,
                             numeric=args.numeric, derivation=args.derivation, inputs=args.input)
        return change_claim(store, args.id, args.operation, args.reason, getattr(args, "replacement", None))
    if c == "report":
        return generate_report(store, args.project, args.date, args.output)
    if c == "publish":
        if args.dry_run or (not args.confirm and not conf["publisher"].get("grants")):
            return preview(store, args.id)
        return publish(store, conf["publisher"], args.id, confirm=args.confirm, retry=args.retry)
    if c == "reconcile":
        if args.id.startswith("t_"):
            if args.resolution or args.remote_ids:
                raise ValueError("routing reconciliation only inspects stale routing; no resolution accepted")
            return store.reconcile_routing(args.id)
        kind = store.get(args.id)["kind"]
        if kind == "run":
            return store.reconcile_run(args.id, args.resolution, args.reason)
        return reconcile(store, conf["publisher"], args.id, args.resolution, args.reason, args.remote_ids)
    raise ValueError("unsupported command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            result = demo(args.output)
        else:
            conf = config.init(args.config) if args.command == "init" else config.load(args.config)
            if args.command == "doctor":
                result = config.doctor(conf)
            elif args.command == "init":
                store = Store(conf["state_dir"])
                store.close()
                result = {"status": "initialized", "config": conf["config_path"], "state_dir": conf["state_dir"]}
            else:
                store = Store(conf["state_dir"])
                try:
                    result = dispatch(args, conf, store)
                finally:
                    store.close()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        data = result.get("data", result) if isinstance(result, dict) else {}
        if data.get("status") in {"failed", "outcome_unknown", "needs_user"} or (args.command == "route" and data.get("accepted") is False):
            return 2
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print("wa: " + redact(str(exc)), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("wa: interrupted; inspect journal before another attempt", file=sys.stderr)
        return 130
