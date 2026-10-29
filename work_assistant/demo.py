"""Deterministic synthetic journey. Never invokes installed models or a network."""
import json
import re
from pathlib import Path
from unittest.mock import patch
from .config import load
from .store import Store
from .executors import Providers
from .process import ProcessResult
from .routing import route_task
from .runner import run_task
from .evidence import add_evidence, add_claim, change_claim
from .reports import generate_report
from .feishu import preview
from .util import atomic_write, dumps


def synthetic_process(argv, prompt, cwd, **kwargs):
    if argv[-3:] == ["mcp", "list", "--json"]:
        return ProcessResult("[]", latency_ms=1)
    if prompt.startswith("Classify"):
        task_id = re.search(r'"task_id":"([^"]+)"', prompt).group(1)
        planner = "exec" in argv
        complex_task = '"goal":"Review synthetic architecture"' in prompt
        route = {"schema": "wa.route.v1", "task_id": task_id, "complexity": "complex" if complex_task else "simple",
                 "executor": "claude_code" if complex_task else "codex", "requested_action": "read" if complex_task else "write_workspace",
                 "reason": "Synthetic offline classification", "confidence": 0.96 if planner or not complex_task else 0.6,
                 "needs_escalation": complex_task and not planner}
        text = dumps(route)
        if not planner:
            return ProcessResult('\n'.join(dumps(e) for e in [{"type": "session", "sessionId": "synthetic-dsh"},
                {"type": "text", "text": text}, {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
                {"type": "final", "text": text}]), latency_ms=5)
    elif prompt.startswith("Read-only independent review"):
        text = dumps({"verdict": "pass", "reason": "Synthetic review fixture; no live model validation"})
    else:
        text = "Synthetic provider reports: fixture work completed. This is an offline simulation."
        if "Authorized action: write_workspace" in prompt:
            atomic_write(Path(cwd) / "result.txt", "synthetic result\n")
    if "exec" in argv:
        return ProcessResult('\n'.join(dumps(e) for e in [{"type": "thread.started", "thread_id": "synthetic-codex"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}}, {"type": "turn.completed"}]), latency_ms=7)
    return ProcessResult(dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                               "session_id": "synthetic-claude", "permission_denials": []}), latency_ms=8)


def demo(output):
    root = Path(output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "demo.json"
    if manifest.exists():
        previous = json.loads(manifest.read_text(encoding="utf-8"))
        if not Path(previous["report_path"], "index.html").exists():
            raise ValueError("existing demo is incomplete; select a new output directory")
        return previous
    if (root / "state" / "state.sqlite3").exists():
        raise ValueError("partial demo state exists; select a new output directory")
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    atomic_write(workspace / "README.txt", "Synthetic offline workspace; no personal data.\n")
    config = load(root / "unused-config.json")
    store = Store(root / "state")
    providers = Providers(config, process=synthetic_process)
    sequence = [0]
    def stable_id(prefix):
        sequence[0] += 1
        return prefix + "_demo_%04d" % sequence[0]
    try:
        with patch("work_assistant.store.uid", stable_id), patch("work_assistant.evidence.uid", stable_id), \
             patch("work_assistant.store.now", lambda: "2026-01-15T12:00:00+00:00"), \
             patch("work_assistant.evidence.now", lambda: "2026-01-15T12:00:00+00:00"):
            project = store.add_project("Synthetic Lab", workspace, ["read", "write_workspace"])
            first = store.add_task(project["id"], "Create a synthetic result file", "write_workspace")
            route_task(store, providers, first["id"])
            run_task(store, providers, first["id"])
            run_task(store, providers, first["id"], review=True,
                     observe=lambda *a, **kw: ProcessResult(stdout="synthetic git observation\n"))
            second = store.add_task(project["id"], "Review synthetic architecture", "read")
            route_task(store, providers, second["id"])
            run_task(store, providers, second["id"])
            store.add_task(project["id"], "Collect the next synthetic sample", blockers="No additional sample supplied")
            for count in (12, 10):
                path = root / ("sample-%s.json" % count)
                atomic_write(path, dumps({"values": {"samples": {"value": count, "unit": "items"}}}))
                evidence = add_evidence(store, project["id"], path, "offline synthetic fixture", origin="demo fixture")
                claim = add_claim(store, project["id"], "Synthetic sample count: %s" % count, [evidence["id"]],
                                  attribution="synthetic fixture operator", numeric={"key": "samples", "value": count, "unit": "items"})
                if count == 12:
                    old_claim = claim
            change_claim(store, old_claim["id"], "supersede", "Two duplicate fixture rows removed", claim["id"])
            # Canonical synthetic source locators make reports reproducible across output directories.
            for source in store.records("evidence"):
                source["data"]["source"] = "synthetic://fixtures/" + source["id"]
                store.update_record(source["id"], source["data"])
            report = generate_report(store, project["id"], "2026-01-15", root / "reports")
            card = preview(store, report["id"])
            result = {"mode": "offline synthetic; no provider or Feishu validation", "report_id": report["id"],
                      "report_path": report["data"]["path"], "hash": report["data"]["hash"],
                      "state_dir": str(store.root), "routes": len(store.records("route")), "preview": card["status"]}
            atomic_write(manifest, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return result
    finally:
        store.close()
