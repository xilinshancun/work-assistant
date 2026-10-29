"""Adapters for installed CLI protocols. Unknown/missing terminal events never imply success."""
import json
import re
import tempfile
from pathlib import Path
from .policy import execution_policy
from .process import execute, ProcessLaunchError
from .util import strict_json, redact


ROUTE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "schema": {"type": "string", "enum": ["wa.route.v1"]}, "task_id": {"type": "string"},
    "complexity": {"type": "string", "enum": ["trivial", "simple", "complex", "unknown"]},
    "executor": {"type": "string", "enum": ["claude_code", "codex", "none"]},
    "requested_action": {"type": "string", "enum": ["read", "write_workspace", "publish", "external"]},
    "reason": {"type": "string"}, "confidence": {"type": "number"}, "needs_escalation": {"type": "boolean"}}}
ROUTE_SCHEMA["required"] = list(ROUTE_SCHEMA["properties"])

# Exact stdout diagnostic verified in the installed deepseek-account adapter.
# Do not skip arbitrary non-JSON lines: they may be a corrupted protocol event.
DSH_MODEL_LIST_DIAGNOSTIC = re.compile(
    r"\[llm-deepseek-account\] model listing from https://api\.deepseek\.com/models "
    r"loaded [0-9]+ model\(s\), skipped [0-9]+ row\(s\)")


def protocol_events(provider, stdout, diagnostics):
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        if provider == "dsh" and DSH_MODEL_LIST_DIAGNOSTIC.fullmatch(line.strip()):
            diagnostics.append(redact(line.strip()))
            continue
        events.append(strict_json(line))
    return events


def parse(provider, result, action="read"):
    answer = {"status": "failed", "text": "", "session_id": None, "tool_calls": False,
              "error": "", "latency_ms": result.latency_ms, "stderr": redact(result.stderr),
              "protocol_log": redact(result.stdout), "launched": True, "diagnostics": list(result.diagnostics)}
    if result.timed_out or result.truncated or result.returncode < 0:
        answer.update(status="outcome_unknown", protocol_status="outcome_unknown", error="timeout, killed process or output limit")
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
            events = protocol_events(provider, result.stdout, answer["diagnostics"])
            if not events or any(type(e) is not dict for e in events):
                raise ValueError("expected JSON event stream")
            success, failure, pieces = False, False, []
            final_seen = False
            for event in events:
                kind = event.get("type")
                if provider == "codex":
                    if kind == "thread.started":
                        answer["session_id"] = event.get("thread_id")
                    if kind in {"error", "turn.failed"}:
                        failure = True
                    if kind in {"item.started", "item.updated", "item.completed"}:
                        item = event.get("item", {})
                        if item.get("type") == "agent_message" and kind == "item.completed":
                            pieces.append(item.get("text", ""))
                        elif item.get("type") == "error":
                            answer["diagnostics"].append("item diagnostic: " + redact(str(item.get("message", "")))[:500])
                        elif item.get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
                            answer["tool_calls"] = True
                            if item.get("status") == "failed":
                                # Failed commands (e.g. rg misses or an initial test failure)
                                # can be recovered within a successful turn.
                                answer["diagnostics"].append("intermediate item failed: " + item["type"])
                    if kind == "turn.completed":
                        success = True
                elif provider == "dsh":
                    if event.get("truncated") is True:
                        raise ValueError("truncated DSH event")
                    if kind == "session":
                        answer["session_id"] = event.get("sessionId")
                    elif kind in {"tool_call", "tool_result"}:
                        answer["tool_calls"] = True
                    elif kind == "error":
                        failure = True
                    elif kind == "status" and event.get("phase") == "turn_end":
                        # Verified against installed dsh-agent-loop + headless projection.
                        # Synthetic fixtures reflect source, not successful live validation.
                        reason = event.get("reason")
                        if reason == {"kind": "completed"}:
                            success = True
                        else:
                            failure = True
                            if isinstance(reason, dict) and isinstance(reason.get("error"), dict):
                                code = reason["error"].get("code")
                                if type(code) is str:
                                    answer["diagnostics"].append(redact(code))
                    elif kind == "final":
                        if final_seen or not success or event is not events[-1]:
                            failure = True
                        final_seen = True
                        answer["text"] = event.get("text", "")
                else:
                    raise ValueError("unknown provider")
            if provider == "codex":
                answer["text"] = "\n".join(pieces)
            else:
                success = success and final_seen
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
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        answer["error"] = "invalid/truncated provider response: " + str(exc)
    answer["protocol_status"] = answer["status"]
    if action == "write_workspace" and answer["status"] == "failed":
        answer.update(status="outcome_unknown", error="write run did not complete; effects may already exist: " + answer["error"])
    if provider == "dsh" and answer["status"] != "completed" and answer["diagnostics"]:
        answer["error"] += ": " + "; ".join(answer["diagnostics"])
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
            cwd = workspace
            if classifier:
                cwd = Path(temp) / "classification"
                cwd.mkdir()
            if provider == "dsh":
                if not classifier or action != "read":
                    raise ValueError("DSH V1 is a read-only classifier")
                for path in conf.get("patches", []):
                    argv += ["--patch", path]
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
                        argv += ["-C", str(cwd), "-s", policy["permission_mode"]]
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
                result = self.process(argv, prompt, cwd=cwd, env=env, timeout=conf["timeout"], pass_fds=pass_fds)
                parsed = parse(provider, result, action)
                # Final file is supplemental; terminal stream validation is still mandatory.
                if provider == "codex" and parsed["status"] == "completed" and output.exists():
                    if output.stat().st_size > 2 * 1024 * 1024:
                        parsed.update(status="outcome_unknown", error="final output exceeds limit")
                    else:
                        parsed["text"] = redact(output.read_text(encoding="utf-8"))
                for key, value in env.items():
                    if any(word in key.lower() for word in ("secret", "token", "password", "key")) and len(value) >= 8:
                        for field in ("text", "stderr", "error", "protocol_log"):
                            parsed[field] = parsed[field].replace(value, "[REDACTED]")
                return parsed
            except OSError as exc:
                launched = not isinstance(exc, ProcessLaunchError)
                return {"status": "outcome_unknown" if launched else "failed", "text": "", "session_id": None, "tool_calls": False,
                        "error": redact(str(exc)), "latency_ms": 0, "stderr": "", "launched": launched}
