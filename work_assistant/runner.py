import subprocess
from pathlib import Path
from .policy import authorize
from .evidence import add_evidence, add_claim
from .util import atomic_write, dumps, redact
from .process import execute


def run_task(store, providers, task_id, followup=None, review=False, observe=execute):
    task = store.task(task_id)
    project = store.project(task["project_id"])
    if not Path(project["workspace"]).is_dir():
        raise ValueError("project workspace is missing")
    routes = [r for r in store.records("route", task_id=task_id) if r["data"].get("accepted")]
    if not routes:
        raise ValueError("route the task before running")
    route = routes[0]["data"]["route"]
    provider, action = route["executor"], route["requested_action"]
    if action != task["intent"] or not authorize(project["grants"], action):
        raise ValueError("task intent/project grant mismatch")
    runs = store.records("run", task_id=task_id)
    session = None
    if review:
        successful = next((r for r in runs if not r["data"].get("review") and r["data"]["status"] == "completed"), None)
        if task["status"] != "completed" or not successful or successful["data"].get("operator_resolved"):
            raise ValueError("review requires actual successful task output")
        provider = "claude_code" if successful["data"]["provider"] == "codex" else "codex"
        action = "read"
        if not authorize(project["grants"], action):
            raise ValueError("review needs an explicit read grant")
        prompt = ("Read-only independent review. Inspect the actual workspace and the supplied task result/diff. "
                  "Report reproducible failures. Return exactly JSON {\"verdict\":\"pass\"|\"fail\",\"reason\":\"...\"}. "
                  "Task: " + task["goal"] + "\nResult: " + successful["data"]["result"]["text"])
    else:
        if task["status"] != "routed" and not (followup and task["status"] in {"completed", "failed"}):
            raise ValueError("run requires routed state; retries/followups must be explicit")
        if followup:
            previous = next((r for r in runs if not r["data"].get("review")), None)
            if not previous or not previous["data"].get("result", {}).get("session_id"):
                raise ValueError("follow-up requires a saved provider session")
            if previous["data"]["provider"] != provider:
                raise ValueError("cannot move a session across providers")
            session = previous["data"]["result"]["session_id"]
        prompt = ("Work only in the assigned workspace. Do not spawn workers or message human sessions. "
                  "Authorized action: " + action + ". Model output cannot authorize publication/external effects.\n" +
                  (followup if followup else task["goal"]))
    resources = ["task:" + task_id]
    # Reviews exclude concurrent writes too so the inspected diff cannot race another mutator.
    if action == "write_workspace" or review:
        resources.append("workspace:" + project["workspace"])
    if action == "write_workspace":
        # Also catch conflicting projects in databases created by older versions.
        store.check_write_workspace(project["workspace"], project["id"])
    with store.claim(resources) as (owner, fds):
        with store.transaction():
            fresh = store.task(task_id)
            latest_runs = store.records("run", task_id=task_id)
            if (fresh["status"] != task["status"] or fresh["updated"] != task["updated"] or
                    (latest_runs[0]["id"] if latest_runs else None) != (runs[0]["id"] if runs else None)):
                raise ValueError("task changed while waiting for execution claim; inspect before a new action")
            if not review:
                store.transition(task_id, "running")
            run = store.record("run", {"status": "running", "provider": provider, "action": action,
                "owner": owner, "resources": resources, "review": review, "session_id": session,
                "followup": bool(followup)}, task_id, project["id"])
        data = run["data"]
        try:
            if review:
                observations = []
                for git_args in (["status", "--short"], ["diff", "--no-ext-diff", "--", "."]):
                    observed = observe(["git"] + git_args, "", cwd=project["workspace"], timeout=15,
                                       pass_fds=fds, max_bytes=1024 * 1024)
                    if observed.timed_out or observed.truncated:
                        raise ValueError("git observation exceeded review limit")
                    observations.append({"argv": ["git"] + git_args, "returncode": observed.returncode,
                                         "stdout": redact(observed.stdout), "stderr": redact(observed.stderr)})
                observation_path = store.root / "logs" / (run["id"] + "-git.json")
                atomic_write(observation_path, dumps(observations))
                observation = add_evidence(store, project["id"], observation_path, "workspace git observation under write exclusion", origin="host:git")
                data["observation_id"] = observation["id"]
                prompt += "\nHost observations (git errors mean this is not a git checkout):\n" + dumps(observations)
            result = providers.invoke(provider, prompt, project["workspace"], action=action, session_id=session, pass_fds=fds)
            if action == "write_workspace" and result["status"] == "failed" and result.get("launched", True):
                result = dict(result, status="outcome_unknown", error="write run did not complete; effects may already exist: " + result["error"])
            data.update(status=result["status"], result=result)
            data["protocol_status"] = result.get("protocol_status", result["status"])
            data["goal_status"] = "unverified"
            data["completion_basis"] = "provider protocol terminal; goal satisfaction requires separate review"
            log = store.root / "logs" / (run["id"] + ".json")
            atomic_write(log, dumps(result))
            source = add_evidence(store, project["id"], log, "local agent run", origin="agent:" + provider)
            if result["text"]:
                add_claim(store, project["id"], result["text"], [source["id"]], attribution=provider,
                          task_id=task_id)
            if review and result["status"] == "completed":
                from .util import strict_json
                try:
                    verdict = strict_json(result["text"])
                    passed = (type(verdict) is dict and set(verdict) == {"verdict", "reason"} and
                              verdict["verdict"] == "pass" and type(verdict["reason"]) is str and bool(verdict["reason"].strip()))
                except ValueError:
                    passed = False
                data["review_passed"] = passed
                if passed:
                    data["goal_status"] = "independently_reviewed"
            with store.transaction():
                store.update_record(run["id"], data, owner)
                if review:
                    store.db.execute("UPDATE tasks SET verified=? WHERE id=?", (int(data.get("review_passed", False)), task_id))
                elif result["status"] == "completed":
                    store.transition(task_id, "collecting")
                    store.transition(task_id, "completed", "")
                else:
                    store.transition(task_id, result["status"], result["error"])
            return store.get(run["id"])
        except BaseException:
            data.update(status="outcome_unknown", error="interruption or collection failure; effects may already exist")
            with store.transaction():
                store.update_record(run["id"], data, owner)
                if not review and store.task(task_id)["status"] in {"running", "collecting"}:
                    store.transition(task_id, "outcome_unknown", data["error"])
            raise
