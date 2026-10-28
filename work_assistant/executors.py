"""Adapters for installed CLI protocols. Unknown/missing terminal events never imply success."""
import json
import tempfile
from pathlib import Path
from .policy import execution_policy
from .process import execute
from .util import strict_json, redact


ROUTE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "schema": {"type": "string", "enum": ["wa.route.v1"]}, "task_id": {"type": "string"},
    "complexity": {"type": "string", "enum": ["trivial", "simple", "complex", "unknown"]},
    "executor": {"type": "string", "enum": ["claude_code", "codex", "none"]},
    "requested_action": {"type": "string", "enum": ["read", "write_workspace", "publish", "external"]},
    "reason": {"type": "string"}, "confidence": {"type": "number"}, "needs_escalation": {"type": "boolean"}}}
ROUTE_SCHEMA["required"] = list(ROUTE_SCHEMA["properties"])


def parse(provider, result):
    answer = {"status": "failed", "text": "", "session_id": None, "tool_calls": False,
              "error": "", "latency_ms": result.latency_ms, "stderr": redact(result.stderr)}
    if result.timed_out or result.truncated or result.returncode < 0:
        answer.update(status="outcome_unknown", error="timeout, killed process or output limit")
        return answer
    try:
        if provider == "claude_code":
            event = strict_json(result.stdout)
            if type(event) is not dict:
                raise ValueError("Claude result must be an object")
            answer.update(text=event.get("result", ""), session_id=event.get("session_id"))
            denied = event.get("permission_denials")
            success = event.get("type") == "result" and event.get("subtype") == "success" and event.get("is_error") is False and not denied
            if not success:
                answer["error"] = "Claude non-success result or permission denial"
        else:
            events = [strict_json(line) for line in result.stdout.splitlines() if line.strip()]
            if not events or any(type(e) is not dict for e in events):
                raise ValueError("expected JSON event stream")
            success, failure, pieces = False, False, []
            for event in events:
                kind = event.get("type")
                if provider == "codex":
                    if kind == "thread.started":
                        answer["session_id"] = event.get("thread_id")
                    if kind in {"error", "turn.failed"}:
                        failure = True
                    if kind == "item.completed":
                        item = event.get("item", {})
                        if item.get("type") == "agent_message":
                            pieces.append(item.get("text", ""))
                        elif item.get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
                            answer["tool_calls"] = True
                            if item.get("status") == "failed":
                                failure = True
                    if kind == "turn.completed":
                        success = True
                elif provider == "dsh":
                    if kind == "session":
                        answer["session_id"] = event.get("session_id") or event.get("id")
                    elif kind == "text":
                        pieces.append(event.get("text", ""))
                    elif kind in {"tool_call", "tool_result"}:
                        answer["tool_calls"] = True
                    elif kind == "error":
                        failure = True
                    elif kind == "final":
                        # Installed headless event vocabulary; additional shapes must be verified.
                        success = event.get("reason") == "completed"
                        if not success:
                            failure = True
                else:
                    raise ValueError("unknown provider")
            answer["text"] = "\n".join(pieces)
            success = success and not failure
            if not success:
                answer["error"] = "missing terminal success or provider error"
        if result.returncode != 0:
            success = False
            answer["error"] = "provider exited with code %s" % result.returncode
        if type(answer["text"]) is not str or not answer["text"].strip():
            success = False
            answer["error"] = "missing final text"
        if success:
            answer["status"] = "completed"
    except (ValueError, TypeError, KeyError) as exc:
        answer["error"] = "invalid/truncated provider response: " + str(exc)
    answer["text"] = redact(answer["text"])
    answer["error"] = redact(answer["error"])
    return answer


class Providers:
    def __init__(self, config, process=execute):
        self.config, self.process = config, process

    def invoke(self, provider, prompt, workspace, action="read", classifier=False, session_id=None, pass_fds=()):
        conf = self.config["providers"][provider]
        argv, env = list(conf["argv"]), dict(conf["env"])
        with tempfile.TemporaryDirectory(prefix="wa-provider-") as temp:
            output = Path(temp) / "final.txt"
            if provider == "dsh":
                if not classifier or action != "read":
                    raise ValueError("DSH V1 is a read-only classifier")
                argv += ["--profile", "headless", "--json"]
                env["DSH_PERMISSION_MODE"] = "read-only"
                if session_id:
                    argv += ["--session-id", session_id]
                argv += ["-"]
            else:
                policy = execution_policy(provider, action)
                if provider == "codex":
                    argv += ["exec"]
                    if session_id:
                        # Resume parser has no -s/-C flags; use global config overrides.
                        argv += ["-c", 'sandbox_mode="%s"' % policy["permission_mode"], "resume"]
                    else:
                        argv += ["-C", str(workspace), "-s", policy["permission_mode"]]
                    argv += ["--json", "--skip-git-repo-check", "-o", str(output)]
                    effort = conf.get("planner_effort" if classifier else "effort", "low" if classifier else "medium")
                    argv += ["-c", 'model_reasoning_effort="%s"' % effort]
                    if classifier:
                        schema = Path(temp) / "route.schema.json"
                        schema.write_text(json.dumps(ROUTE_SCHEMA), encoding="utf-8")
                        argv += ["--output-schema", str(schema)]
                    if conf.get("model"):
                        argv += ["-m", conf["model"]]
                    if session_id:
                        argv += [session_id]
                    argv += ["-"]
                else:
                    argv += ["-p", "--output-format", "json", "--permission-mode", policy["permission_mode"],
                             "--tools", ",".join(policy["tools"]), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
                    if conf.get("model"):
                        argv += ["--model", conf["model"]]
                    if session_id:
                        argv += ["--resume", session_id]
            try:
                result = self.process(argv, prompt, cwd=workspace, env=env, timeout=conf["timeout"], pass_fds=pass_fds)
                parsed = parse(provider, result)
                # Final file is supplemental; terminal stream validation is still mandatory.
                if provider == "codex" and parsed["status"] == "completed" and output.exists():
                    if output.stat().st_size > 2 * 1024 * 1024:
                        parsed.update(status="outcome_unknown", error="final output exceeds limit")
                    else:
                        parsed["text"] = redact(output.read_text(encoding="utf-8"))
                for key, value in env.items():
                    if any(word in key.lower() for word in ("secret", "token", "password", "key")) and len(value) >= 8:
                        for field in ("text", "stderr", "error"):
                            parsed[field] = parsed[field].replace(value, "[REDACTED]")
                return parsed
            except OSError as exc:
                return {"status": "failed", "text": "", "session_id": None, "tool_calls": False,
                        "error": redact(str(exc)), "latency_ms": 0, "stderr": ""}
