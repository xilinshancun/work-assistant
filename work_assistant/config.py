import copy
import json
import os
import shutil
import sys
from pathlib import Path
from .util import atomic_write

DSH_APP = "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
DEFAULT = {
    "state_dir": ".wa", "providers": {
        "dsh": {"argv": ["dsh"], "env": {}, "timeout": 25, "patches": []},
        "codex": {"argv": ["codex"], "env": {}, "timeout": 600, "planner_effort": "low", "effort": "medium"},
        "claude_code": {"argv": ["claude"], "env": {}, "timeout": 600}},
    "publisher": {"mode": "app", "transport": "http", "app_id_env": "FEISHU_APP_ID", "app_secret_env": "FEISHU_APP_SECRET",
                  "document_layout": "per_period", "timeline_title": "Daily Sync", "timeline_max_blocks": 3000,
                  "receive_id_env": "FEISHU_RECEIVE_ID", "receive_id_type": "chat_id", "webhook_env": "FEISHU_WEBHOOK",
                  "folder_token_env": "FEISHU_FOLDER_TOKEN", "grants": []},
    "ingest_paths": []}


def resolve_path(value, base):
    path = Path(value).expanduser()
    return (Path(base) / path).resolve() if not path.is_absolute() else path.resolve()


def load(path="config.local.json"):
    path = Path(path).expanduser().resolve()
    data = copy.deepcopy(DEFAULT)
    if path.exists():
        given = json.loads(path.read_text(encoding="utf-8"))
        for key, value in given.items():
            if key == "providers":
                for provider, conf in value.items():
                    if provider not in data["providers"]:
                        raise ValueError("unknown provider: " + provider)
                    data["providers"][provider].update(conf)
            elif key == "publisher":
                data[key].update(value)
            else:
                data[key] = value
    data["config_path"] = str(path)
    data["state_dir"] = str(resolve_path(data["state_dir"], path.parent))
    data["ingest_paths"] = [str(resolve_path(p, path.parent)) for p in data["ingest_paths"]]
    for name, conf in data["providers"].items():
        if name == "dsh":
            if type(conf["patches"]) is not list or any(type(p) is not str or not p for p in conf["patches"]):
                raise ValueError("DSH patches must be a list of local paths")
            conf["patches"] = [str(resolve_path(p, path.parent)) for p in conf["patches"]]
        argv = conf["argv"]
        if type(argv) is not list or not argv or any(type(a) is not str or not a for a in argv):
            raise ValueError("provider argv must be a nonempty string array")
        if "/" in argv[0] or argv[0].startswith("~"):
            argv[0] = str(resolve_path(argv[0], path.parent))
        if name == "dsh" and argv == ["dsh"] and not shutil.which("dsh") and Path(DSH_APP).is_file():
            argv[0] = DSH_APP
        if type(conf["env"]) is not dict or any(type(k) is not str or type(v) is not str for k, v in conf["env"].items()):
            raise ValueError("provider env must map strings to strings")
        # Explicit home overlays stay local and use the same path base as config.
        for key in ("HOME", "CODEX_HOME", "DSH_HOME", "CLAUDE_CONFIG_DIR"):
            if key in conf["env"]:
                conf["env"][key] = str(resolve_path(conf["env"][key], path.parent))
        if type(conf["timeout"]) not in (int, float) or not 0 < conf["timeout"] <= 86400:
            raise ValueError("invalid timeout")
        for field in ("effort", "planner_effort"):
            if field in conf and conf[field] not in {"low", "medium", "high", "xhigh"}:
                raise ValueError("invalid effort")
    from .lark_cli import validate
    validate(data["publisher"], path.parent)
    return data


def init(path):
    path = Path(path).expanduser().resolve()
    if not path.exists():
        atomic_write(path, json.dumps(DEFAULT, ensure_ascii=False, indent=2) + "\n")
        path.chmod(0o600)
    conf = load(path)
    Path(conf["state_dir"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    return conf


def doctor(conf, process=None):
    from .process import execute
    from .executors import codex_mcp_preflight, MCPPreflightError
    process = process or execute
    result = {}
    for name, item in conf["providers"].items():
        executable = item["argv"][0]
        found = shutil.which(executable, path=item["env"].get("PATH"))
        capabilities = {}
        mcp_error = None
        help_error = None
        options = {"codex": ["--json", "--sandbox", "--output-schema", "--output-last-message"],
                   "claude_code": ["--output-format", "--permission-mode", "--tools", "--resume"],
                   "dsh": ["--profile"]}[name]
        if found:
            args = list(item["argv"]) + (["exec", "--help"] if name == "codex" else ["--help"])
            try:
                response = process(args, "", cwd=Path(conf["config_path"]).parent, env=item["env"], timeout=10, max_bytes=128 * 1024)
                capabilities = {option: option in response.stdout for option in options}
                capabilities["help_success"] = (response.returncode == 0 and not response.timed_out
                                                and not response.truncated and not response.invalid_utf8)
                if not capabilities["help_success"]:
                    help_error = "CLI help probe failed, timed out or returned incomplete/invalid output"
            except (OSError, ValueError):
                capabilities["help_success"] = False
                help_error = "CLI was found but could not run its help probe; check argv and executable permissions"
            if name == "codex" and help_error is None:
                try:
                    _, metadata = codex_mcp_preflight(item["argv"], item["env"], Path(conf["config_path"]).parent,
                                                      process=process)
                    capabilities["mcp_isolation_ready"] = True
                except MCPPreflightError as exc:
                    capabilities["mcp_isolation_ready"] = False
                    mcp_error = "cannot safely start Codex: " + str(exc)
            elif name == "codex":
                capabilities["mcp_isolation_ready"] = False
        missing = {"dsh": "optional: classification failures escalate once to the configured Codex planner",
                   "codex": "needed for GPT escalation and Codex execution; configure argv or install/login separately",
                   "claude_code": "optional executor; independent opposite-provider review needs both executors"}
        missing_options = [option for option in options if not capabilities.get(option)] if found and not help_error else []
        if not found:
            readiness, diagnostic = "not_installed", missing[name]
            next_steps = ["Install the CLI or set providers.%s.argv and providers.%s.env.PATH in this config." % (name, name)]
        elif help_error:
            readiness, diagnostic = "probe_failed", help_error
            next_steps = ["Repair providers.%s.argv/environment and check that its help command can run, then rerun doctor." % name]
        elif mcp_error:
            readiness, diagnostic = "blocked", mcp_error
            next_steps = ["Repair the Codex CLI/MCP configuration until both mcp list --json checks succeed, then rerun doctor."]
        elif missing_options:
            readiness = "incompatible"
            diagnostic = "CLI help is missing required options: " + ", ".join(missing_options)
            next_steps = ["Use a compatible CLI in providers.%s.argv with the listed options, then rerun doctor." % name]
        else:
            readiness, diagnostic = "local_checks_passed", "local CLI checks passed; authentication and model access untested"
            next_steps = ["Authenticate this provider separately before running a real task; doctor does not test model access."]
        if name == "dsh":
            next_steps.append("DSH is optional; classification failures escalate to Codex, which must be installed and authenticated for that fallback.")
        elif name == "claude_code":
            next_steps.append("Claude Code is optional for --provider codex tasks; independent opposite-provider review requires both executors.")
        result[name] = {"installed": bool(found), "configured_env_keys": sorted(item["env"]),
                        "authentication": "not tested", "capabilities": capabilities,
                        "diagnostic": diagnostic, "readiness": readiness, "next_steps": next_steps}
    pub = conf["publisher"]
    if pub.get("transport", "http") == "lark_cli":
        from .lark_cli import LarkCLI
        result["feishu"] = LarkCLI(pub, process=process).diagnose()
        result["feishu"]["document_layout"] = pub.get("document_layout", "per_period")
    else:
        result["feishu"] = {"mode": pub["mode"], "configured": all(os.environ.get(pub[k]) for k in
            (("webhook_env",) if pub["mode"] == "webhook" else ("app_id_env", "app_secret_env", "receive_id_env"))),
            "live_tested": False, "document_layout": pub.get("document_layout", "per_period")}
    local = {name: result.get(name, {}).get("readiness") == "local_checks_passed" for name in DEFAULT["providers"]}
    paths = []
    if local["codex"]:
        paths.append({"provider": "codex", "routing": "DSH optional; Codex handles fallback", "coverage": "planner_and_executor",
                      "task_option": ["--provider", "codex"]})
    if local["claude_code"] and (local["dsh"] or local["codex"]):
        paths.append({"provider": "claude_code", "routing": "Codex planner fallback available; DSH optional" if local["codex"] else
                      "DSH direct route; GPT escalation additionally needs Codex",
                      "coverage": "planner_and_executor" if local["codex"] else "direct_classification_only",
                      "task_option": ["--provider", "claude_code"]})
    result["workflows"] = {
        "offline": {"readiness": "available", "authentication": "not required",
            "features": ["demo", "project/task records", "evidence/claims", "report", "publish --dry-run"],
            "next_steps": [{"argv": [sys.executable, "-m", "work_assistant", "demo", "--output", "out/demo"]}]},
        "ask": {"readiness": "local_checks_passed" if local["codex"] else "limited_direct_route" if paths else "needs_providers",
            "authentication": "not tested", "gpt_fallback": "local_checks_passed" if local["codex"] else "unavailable",
            "paths": paths, "next_steps": ["Authenticate the listed provider CLIs separately; local compatibility does not test model access." if local["codex"] else
                "DSH+Claude only supports direct classification here; install/configure and authenticate Codex for complex/failed classification fallback." if paths else
                "Install/configure and authenticate Codex for the minimal ask path; DSH is optional. See the provider diagnostics above."]},
        "independent_review": {"optional": True, "readiness": "local_checks_passed" if local["codex"] and local["claude_code"] else "needs_providers",
            "authentication": "not tested", "next_steps": ["Independent opposite-provider review requires both Codex and Claude Code; omit --review to use a single executor."]},
        "feishu_live": {"optional": True, "readiness": "configured_auth_untested" if result["feishu"].get("configured") else "not_configured",
            "live_tested": False, "next_steps": ["Configure your own destination/app or webhook for live publishing; reports and publish --dry-run work without Feishu."]}}
    return result
