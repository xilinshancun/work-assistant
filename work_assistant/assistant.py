"""A thin one-shot entry over the existing task commands; no scheduler or grants."""
import sqlite3
import sys
from datetime import date
from .policy import authorize
from .routing import route_task, validate_routing
from .runner import run_task
from .reports import generate_report
from .util import redact


def ask_task(store, providers, project, goal, intent="read", provider=None, review=False, report_date=None, routing="auto"):
    validate_routing(routing, provider)  # Before creating a task or selecting a project.
    if report_date is not None:
        date.fromisoformat(report_date)
    p = store.project(project)
    task = store.add_task(p["id"], goal, intent, provider)
    ident = task["id"]
    result = {"task_id": ident, "stage": "task", "routing": routing, "steps": []}

    def finish(status=None, diagnostic=None):
        current = store.task(ident)
        result.update(status=status or current["status"], task_status=current["status"],
                      goal_status=current["goal_status"], diagnostic=diagnostic or current["blockers"])
        if result["status"] != "completed":
            # Guidance is data only: never probe providers or repeat work while
            # explaining a failed/unknown result. Use the same installed runtime.
            command = [sys.executable, "-m", "work_assistant", "--config",
                       providers.config.get("config_path", "config.local.json")]
            result["next_steps"] = [{"action": "inspect_task", "argv": command + ["task", "show", ident]}]
            if result["stage"] in {"route", "run", "review"}:
                note = ("Independent review needs the opposite executor; inspect its local CLI readiness." if result["stage"] == "review" else
                        "Direct routing uses only the explicitly selected executor; inspect its CLI setup/authentication. No classifier or planner was used." if routing == "direct" else
                        "Inspect local CLI compatibility and authenticate providers separately; DSH is optional, Codex is required for GPT fallback. Continue the existing task after correcting setup.")
                result["next_steps"].append({"action": "check_provider_readiness", "argv": command + ["doctor", "--json"],
                    "note": note})
            if result["status"] == "outcome_unknown":
                run_id = next((step["id"] for step in reversed(result["steps"]) if step["kind"] in {"run", "review"}), None)
                if run_id:
                    result["next_steps"].append({"action": "inspect_unknown_run", "argv": command + ["reconcile", run_id],
                        "note": "Read-only inspection; preserve the unknown outcome before considering any new action."})
        return result

    try:
        if not authorize(p["grants"], intent):
            store.transition(ident, "needs_user", "project grants deny requested task intent; no authority created")
            return finish()
        result["stage"] = "route"
        route = route_task(store, providers, ident, routing=routing)
        result["steps"].append({"kind": "route", "id": route["id"]})
        if not route["data"]["accepted"]:
            return finish()
        result["stage"] = "run"
        run = run_task(store, providers, ident)
        result["steps"].append({"kind": "run", "id": run["id"]})
        if run["data"]["status"] != "completed":
            return finish()
        if review:
            result["stage"] = "review"
            check = run_task(store, providers, ident, review=True)
            result["steps"].append({"kind": "review", "id": check["id"]})
            if check["data"]["status"] != "completed":
                return finish(check["data"]["status"], check["data"].get("result", {}).get("error"))
            if check["data"].get("review_passed") is not True:
                return finish("needs_user", "independent review did not pass; inspect the saved review run")
        if report_date:
            result["stage"] = "report"
            report = generate_report(store, p["id"], report_date)
            result["steps"].append({"kind": "report", "id": report["id"]})
            result.update(report_id=report["id"], report_path=report["data"]["path"])
        return finish()
    except (ValueError, OSError, sqlite3.Error) as exc:
        current = store.task(ident)
        status = current["status"] if current["status"] in {"failed", "outcome_unknown", "needs_user"} else "needs_user"
        return finish(status, redact(str(exc)))
    except KeyboardInterrupt as exc:
        # The underlying route/run handlers have already persisted safe state.
        exc.task_id = ident
        raise
