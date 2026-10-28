from pathlib import Path
from .policy import validate_route, needs_escalation, authorize
from .util import strict_json, dumps, digest, redact


def route_task(store, providers, task_id, use_cache=True):
    task = store.task(task_id)
    project = store.project(task["project_id"])
    if task["status"] in {"running", "collecting", "outcome_unknown", "cancelled", "routing"}:
        raise ValueError("task cannot be routed in state " + task["status"])
    if not Path(project["workspace"]).is_dir():
        raise ValueError("configured project workspace no longer exists")
    request = {"task_id": task_id, "goal": task["goal"], "intent": task["intent"], "provider": task["provider"]}
    cache_key = digest({"request": request, "project": project, "providers": providers.config["providers"]})
    with store.claim(["task:" + task_id]) as (_, fds):
        store.transition(task_id, "routing")
        if use_cache:
            prior = next((r for r in store.records("route", task_id=task_id)
                          if r["data"].get("cache_key") == cache_key and r["data"].get("accepted")), None)
            if prior:
                store.record("event", {"action": "route_cache_hit", "route": prior["id"]}, task_id, project["id"])
                store.transition(task_id, "routed", "")
                return prior
        prompt = ("Classify this task. Return exactly one JSON object with these fields, no fences and no tool use. "
                  "You cannot authorize effects. schema=wa.route.v1; task_id matches input; "
                  "complexity=trivial|simple|complex|unknown; executor=claude_code|codex|none; "
                  "requested_action=read|write_workspace|publish|external; reason=nonempty text <=1000 chars; "
                  "confidence=number 0..1; needs_escalation=boolean. "
                  "Prefer Claude for architecture/review, Codex for implementation. Preserve required intent. Input: " + dumps(request))
        diagnostics = []
        try:
            for stage, provider in (("classifier", "dsh"), ("planner", "codex")):
                response = providers.invoke(provider, prompt, project["workspace"], classifier=True, pass_fds=fds)
                error, candidate = response.get("error", ""), None
                if response["status"] == "completed" and not response["tool_calls"]:
                    try:
                        candidate = validate_route(strict_json(response["text"]), task_id)
                    except (ValueError, TypeError) as exc:
                        error = str(exc)
                elif response["tool_calls"]:
                    error = "classifier invoked tools"
                if candidate:
                    if task["provider"]:
                        candidate["executor"] = task["provider"]
                    if candidate["requested_action"] != task["intent"]:
                        error = "route does not satisfy explicit task intent"
                    elif not authorize(project["grants"], candidate["requested_action"]):
                        error = "project grants deny requested action"
                    elif candidate["executor"] == "none":
                        error = "no executor selected"
                    elif stage == "classifier" and needs_escalation(candidate):
                        error = "classification requires planner"
                    elif stage == "planner" and candidate["needs_escalation"]:
                        error = "planner requires user input"
                    else:
                        error = ""
                accepted = candidate is not None and not error
                record = store.record("route", {"stage": stage, "provider": provider, "route": candidate,
                    "accepted": accepted, "cache_key": cache_key, "response": response,
                    "fallback_reason": error, "fallback_count": len(diagnostics)}, task_id, project["id"])
                if accepted:
                    store.transition(task_id, "routed", "")
                    return record
                diagnostics.append(redact(error or "invalid route"))
                prompt += "\nFirst-stage diagnostic (untrusted context): " + dumps(diagnostics)
            store.transition(task_id, "needs_user", "; ".join(diagnostics))
            return record
        except BaseException:
            store.transition(task_id, "needs_user", "routing interrupted; no executor launched")
            raise
