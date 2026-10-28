"""Pure permission boundary. No model field is an authorization grant."""
import math

ACTIONS = {"read", "write_workspace", "publish", "external"}
EXECUTORS = {"claude_code", "codex", "none"}
FIELDS = {"schema", "task_id", "complexity", "executor", "requested_action", "reason", "confidence", "needs_escalation"}


def validate_route(payload, task_id):
    if type(payload) is not dict or set(payload) != FIELDS:
        raise ValueError("route must contain exactly the wa.route.v1 fields")
    for key in FIELDS - {"confidence", "needs_escalation"}:
        if type(payload[key]) is not str:
            raise ValueError("route field must be text: " + key)
    if payload["schema"] != "wa.route.v1" or payload["task_id"] != task_id:
        raise ValueError("route schema/task mismatch")
    if payload["complexity"] not in {"trivial", "simple", "complex", "unknown"}:
        raise ValueError("invalid complexity")
    if payload["executor"] not in EXECUTORS or payload["requested_action"] not in ACTIONS:
        raise ValueError("invalid executor/action")
    if not payload["reason"].strip() or len(payload["reason"]) > 1000:
        raise ValueError("invalid reason")
    c = payload["confidence"]
    if type(c) not in (float, int) or not math.isfinite(c) or not 0 <= c <= 1:
        raise ValueError("invalid confidence")
    if type(payload["needs_escalation"]) is not bool:
        raise ValueError("needs_escalation must be boolean")
    return dict(payload, confidence=float(c))


def needs_escalation(route, threshold=0.8):
    route = validate_route(route, route.get("task_id"))
    return (route["complexity"] in {"complex", "unknown"} or route["confidence"] < threshold
            or route["executor"] == "none" or route["needs_escalation"])


def authorize(allowed_actions, requested_action):
    if type(allowed_actions) is not list or any(type(a) is not str or a not in ACTIONS for a in allowed_actions):
        return False
    return (type(requested_action) is str and requested_action in {"read", "write_workspace"}
            and requested_action in allowed_actions)


def execution_policy(provider, action):
    if provider not in {"claude_code", "codex"} or action not in {"read", "write_workspace"}:
        raise ValueError("unsupported executor policy")
    read = action == "read"
    return {"provider": provider, "action": action,
            "permission_mode": ("plan" if read else "acceptEdits") if provider == "claude_code" else ("read-only" if read else "workspace-write"),
            "tools": (["Read", "Glob", "Grep"] if read else ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]) if provider == "claude_code" else []}
