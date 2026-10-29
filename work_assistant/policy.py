"""Pure validation and executor policy; model output never supplies authority.

Callers must obtain grants from explicit user actions or trusted project config.
These functions cannot establish the provenance of a list of grants themselves.
"""

import math


_ACTIONS = frozenset(("read", "write_workspace", "publish", "external"))
ACTIONS = _ACTIONS
_EXECUTORS = frozenset(("claude_code", "codex", "none"))
_COMPLEXITIES = frozenset(("trivial", "simple", "complex", "unknown"))
_ROUTE_FIELDS = frozenset(
    (
        "schema", "task_id", "complexity", "executor", "requested_action",
        "reason", "confidence", "needs_escalation",
    )
)


def _nonempty_string(value):
    return type(value) is str and bool(value.strip())


def _unit_number(value, name):
    # bool is an int subclass. Only JSON's actual number primitives belong here.
    if type(value) not in (int, float):
        raise ValueError("{} must be a finite number in [0, 1]".format(name))
    # Check bounds before float conversion, including arbitrarily large integers.
    if not 0 <= value <= 1 or not math.isfinite(value):
        raise ValueError("{} must be a finite number in [0, 1]".format(name))
    return float(value)


def validate_route(payload: dict, task_id: str) -> dict:
    """Validate the complete model schema and return a fresh normalized dict.

    Invalid data raises ValueError without echoing potentially sensitive input.
    Only confidence (to float) and surrounding reason whitespace are normalized.
    No permission, path, command, or model settings may be added to the schema.
    """
    if type(payload) is not dict:
        raise ValueError("route must be a dict")
    if any(type(key) is not str for key in payload) or set(payload) != _ROUTE_FIELDS:
        raise ValueError("route fields do not match wa.route.v1")
    if not _nonempty_string(task_id):
        raise ValueError("expected task_id must be a nonempty string")
    if type(payload["schema"]) is not str or payload["schema"] != "wa.route.v1":
        raise ValueError("unsupported route schema")
    if type(payload["task_id"]) is not str or payload["task_id"] != task_id:
        raise ValueError("route task_id does not match")
    for key, values in (
        ("complexity", _COMPLEXITIES),
        ("executor", _EXECUTORS),
        ("requested_action", _ACTIONS),
    ):
        if type(payload[key]) is not str or payload[key] not in values:
            raise ValueError("invalid route {}".format(key))
    reason = payload["reason"]
    if not _nonempty_string(reason) or len(reason) > 1000:
        raise ValueError("route reason must contain text and be at most 1000 characters")
    confidence = _unit_number(payload["confidence"], "confidence")
    if type(payload["needs_escalation"]) is not bool:
        raise ValueError("route needs_escalation must be a bool")
    normalized = dict(payload)
    normalized["confidence"] = confidence
    normalized["reason"] = reason.strip()
    return normalized


def needs_escalation(route: dict, threshold: float = 0.8) -> bool:
    """Decide first-stage escalation, rejecting malformed routes/thresholds.

    DSH failures, timeouts, and tool use are handled by the routing caller. This
    is a first-stage decision, not a veto on a successful complex planner result;
    the caller must bound planner attempts and preserve the user's write intent.
    """
    if type(route) is not dict:
        raise ValueError("route must be a dict")
    checked = validate_route(route, route.get("task_id"))
    minimum = _unit_number(threshold, "threshold")
    return (
        checked["complexity"] in ("complex", "unknown")
        or checked["confidence"] < minimum
        or checked["executor"] == "none"
        or checked["needs_escalation"]
    )


def authorize(allowed_actions: list, requested_action: str) -> bool:
    """Fail closed on malformed grants; allow only exact executor actions.

    Validate the *whole* trusted grant list before checking membership. A write
    grant never implies read or publication, and publication/external authority
    must be checked on a separate path outside this coding-worker boundary.
    """
    if type(allowed_actions) is not list:
        return False
    if type(requested_action) is not str or requested_action not in _ACTIONS:
        return False
    if any(type(grant) is not str or grant not in _ACTIONS for grant in allowed_actions):
        return False
    return requested_action in ("read", "write_workspace") and requested_action in allowed_actions


def execution_policy(provider: str, action: str) -> dict:
    """Return trusted policy data, never CLI flags or a permission bypass.

    Claude writes retain normal approval handling and a bounded file tool set.
    Codex tools are governed by its sandbox mode; its empty tools list does not
    specify an additional CLI tool filter. Adapters must map these fields using
    capabilities verified against the installed CLI.
    """
    if type(provider) is not str or provider not in ("claude_code", "codex"):
        raise ValueError("unsupported execution provider")
    if type(action) is not str or action not in ("read", "write_workspace"):
        raise ValueError("unsupported executor action")
    if provider == "claude_code":
        mode = "plan" if action == "read" else "acceptEdits"
        tool_names = ["Read", "Glob", "Grep"]
        if action == "write_workspace":
            tool_names.extend(("Edit", "Write"))
    else:
        mode = "read-only" if action == "read" else "workspace-write"
        tool_names = []
    return {
        "provider": provider,
        "action": action,
        "permission_mode": mode,
        "tools": tool_names,
    }
