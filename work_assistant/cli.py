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
from .daily_import import import_daily
from .feishu import preview, publish, reconcile
from .demo import demo
from .assistant import ask_task
from .util import redact
from .process import termination_guard, TerminationRequested


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
    a = commands.add_parser("ask", help="Create, route and run one task; stops on unresolved outcomes")
    a.add_argument("project")
    a.add_argument("goal")
    a.add_argument("--intent", choices=["read", "write_workspace"], default="read")
    a.add_argument("--provider", choices=["codex", "claude_code"])
    a.add_argument("--routing", choices=["auto", "direct"], default="auto",
                   help="auto: classification (default); direct: requires --provider and skips classifier/planner calls")
    a.add_argument("--review", action="store_true")
    a.add_argument("--report-date")
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
            a.add_argument("--routing", choices=["auto", "direct"], default="auto",
                           help="auto: classification (default); direct: use the task's saved provider with no classifier call")
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
    a = commands.add_parser("import-daily", help="Import a validated local daily bundle without sending")
    a.add_argument("project")
    a.add_argument("--source-root", required=True)
    a.add_argument("--date", required=True)
    a.add_argument("--timezone", default="Asia/Shanghai")
    a.add_argument("--title", help="Override display title (nonempty, max 200 characters)")
    a.add_argument("--body-scope", choices=["strict", "main"], default="strict",
                   help="Explicit main-only export for a verified template; default rejects outside content")
    a = commands.add_parser("publish")
    a.add_argument("id", help="REPORT_ID, adopt-timeline, or rename-timeline")
    a.add_argument("target_id", nargs="?", help="publication/binding ID for explicit adopt/rename")
    a.add_argument("--title", help="explicit canonical document title for adopt/rename only")
    a.add_argument("--tree-journal", help="explicit completed native surgery acknowledgement for adopt-timeline only")
    a.add_argument("--document-only", action="store_true", help="Sync only the app timeline document; never create/update a card")
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
    if c == "ask":
        return ask_task(store, Providers(conf), args.project, args.goal, args.intent, args.provider,
                        review=args.review, report_date=args.report_date, routing=args.routing)
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
        return route_task(store, Providers(conf), args.id, use_cache=not args.no_cache, routing=args.routing)
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
    if c == "import-daily":
        return import_daily(store, args.project, args.source_root, args.date, args.timezone, title=args.title,
                            body_scope=args.body_scope, card_signature=conf["publisher"].get("card_signature"))
    if c == "publish":
        if args.id in {"adopt-timeline", "rename-timeline"}:
            if args.document_only:
                raise ValueError('--document-only applies only to a report, not adopt/rename')
            if not args.target_id or not args.confirm or args.dry_run:
                raise ValueError("adopt/rename requires a target ID and explicit --confirm")
            from .timeline import adopt_timeline, rename_timeline
            if args.id == "adopt-timeline":
                if args.retry:
                    raise ValueError("adoption is not retried; reconcile binding and use rename-timeline if needed")
                return adopt_timeline(store, conf["publisher"], args.target_id, confirm=True, title=args.title, tree_journal=args.tree_journal)
            if args.tree_journal:raise ValueError('--tree-journal is only accepted for adopt-timeline')
            return rename_timeline(store, conf["publisher"], args.target_id, args.title, confirm=True, retry=args.retry)
        if args.target_id or args.title is not None or args.tree_journal:
            raise ValueError("target ID and --title are only accepted for explicit adopt/rename")
        if args.document_only:
            from .timeline import publish_timeline
            return publish_timeline(store, conf["publisher"], args.id, confirm=args.confirm and not args.dry_run,
                                    retry=args.retry, document_only=True)
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
    with termination_guard():
        return _main(argv)


def _main(argv=None):
    command_parser = parser()
    args = command_parser.parse_args(argv)
    if args.command == "ask" and args.routing == "direct" and args.provider is None:
        command_parser.error("ask --routing direct requires --provider")
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
        if args.command == "review" and data.get("review_passed") is not True:
            return 2
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print("wa: " + redact(str(exc)), file=sys.stderr)
        return 2
    except TerminationRequested as exc:
        print("wa: terminated; inspect journal before another attempt" + ("; task_id=" + exc.task_id if hasattr(exc, "task_id") else ""), file=sys.stderr)
        return 143
    except KeyboardInterrupt as exc:
        print("wa: interrupted; inspect journal before another attempt" + ("; task_id=" + exc.task_id if hasattr(exc, "task_id") else ""), file=sys.stderr)
        return 130
