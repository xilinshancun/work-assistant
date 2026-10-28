import copy
import unittest
from work_assistant.policy import validate_route, needs_escalation, authorize, execution_policy
from work_assistant.util import strict_json


def route(task_id="t_test", **updates):
    value = {"schema": "wa.route.v1", "task_id": task_id, "complexity": "simple", "executor": "codex",
             "requested_action": "read", "reason": "synthetic", "confidence": 0.9, "needs_escalation": False}
    value.update(updates)
    return value


class PolicyTests(unittest.TestCase):
    def test_exact_shape_and_primitives(self):
        self.assertEqual(validate_route(route(), "t_test")["confidence"], 0.9)
        for key in route():
            invalid = route()
            del invalid[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                validate_route(invalid, "t_test")
        cases = [route(extra="write"), route(allowed_actions=["write_workspace"]), route(task_id="other"),
                 route(confidence=True), route(confidence=float("nan")), route(confidence=float("inf")),
                 route(confidence=-0.1), route(confidence=1.1), route(needs_escalation=0),
                 route(executor="arbitrary"), route(reason=""), route(reason="x" * 1001), route(requested_action="shell"),
                 route(complexity=[]), route(executor=False)]
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_route(invalid, "t_test")

    def test_no_fences_nan_duplicate_keys(self):
        for text in ('```json\n{}\n```', '{"confidence":NaN}', '{"a":1,"a":2}'):
            with self.assertRaises(ValueError):
                strict_json(text)

    def test_escalation(self):
        self.assertFalse(needs_escalation(route()))
        for changes in ({"complexity": "complex"}, {"complexity": "unknown"}, {"confidence": 0.4},
                        {"executor": "none"}, {"needs_escalation": True}):
            self.assertTrue(needs_escalation(route(**changes)))

    def test_grants_never_widen(self):
        self.assertTrue(authorize(["read", "write_workspace"], "write_workspace"))
        self.assertFalse(authorize(["write_workspace"], "read"))
        for grants in (["publish"], ["external"], ["read", "oops"], ["read", {}], "read", None, {"read": True}):
            for requested in ("read", "write_workspace", "publish", "external", None, []):
                self.assertFalse(authorize(grants, requested))

    def test_executor_permission_modes(self):
        self.assertEqual(execution_policy("claude_code", "read")["tools"], ["Read", "Glob", "Grep"])
        self.assertEqual(execution_policy("codex", "write_workspace")["permission_mode"], "workspace-write")
        for provider, action in (("dsh", "read"), ("codex", "publish"), ("claude_code", "external")):
            with self.assertRaises(ValueError):
                execution_policy(provider, action)
