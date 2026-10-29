"""Official lark-cli adapter; credentials stay in the CLI's selected securestore."""
import json
import os
import re
import shutil
import time
import urllib.parse
from pathlib import Path
from .feishu import Feishu, APIError, AmbiguousError, document_settings
from .process import execute, ProcessLaunchError
from .util import dumps


def validate(settings, base):
    document_settings(settings)
    kind = settings.get("transport", "http")
    if kind not in {"http", "lark_cli"}:
        raise ValueError("publisher transport must be http or lark_cli")
    if kind != "lark_cli":
        return
    if settings.get("mode") != "app":
        raise ValueError("lark_cli transport supports app mode only")
    argv = settings.get("cli_argv", ["lark-cli"])
    if type(argv) is not list or not argv or any(type(a) is not str or not a or "\x00" in a for a in argv):
        raise ValueError("publisher cli_argv must be a nonempty string array")
    argv = list(argv)
    def resolve(value):
        path = Path(value).expanduser()
        return str((Path(base) / path).resolve() if not path.is_absolute() else path.resolve())
    if "/" in argv[0] or argv[0].startswith("~"):
        argv[0] = resolve(argv[0])
    directory = settings.get("cli_config_dir")
    if type(directory) is not str or not directory or "\x00" in directory:
        raise ValueError("lark_cli requires cli_config_dir")
    app = settings.get("expected_app_id")
    if type(app) is not str or not re.fullmatch(r"cli_[A-Za-z0-9_-]+", app):
        raise ValueError("lark_cli requires a fixed expected_app_id")
    profile = settings.get("cli_profile")
    if profile is not None and (type(profile) is not str or not profile or "\x00" in profile):
        raise ValueError("invalid cli_profile")
    timeout = settings.get("cli_timeout", 30)
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise ValueError("cli_timeout must be between 0 and 300 seconds")
    settings.update(cli_argv=argv, cli_config_dir=resolve(directory), cli_timeout=timeout)


def isolated_environment(settings):
    # A complete allowlist prevents unknown future credential/provider overrides as
    # well as current LARK*, OPENCLAW*, HERMES* and FEISHU* ambient identity inputs.
    keys = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP",
            "SYSTEMROOT", "SystemRoot", "WINDIR", "USERPROFILE", "LOCALAPPDATA",
            "APPDATA", "LANG", "TZ", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}
    env = {k: v for k, v in os.environ.items() if k in keys or k.startswith("LC_")}
    env.update(LARKSUITE_CLI_CONFIG_DIR=settings["cli_config_dir"],
               LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
    return env


class LarkCLI(Feishu):
    def __init__(self, settings, process=None):
        settings = dict(settings)
        validate(settings, Path.cwd())
        super().__init__(settings)
        self.process = process or execute
        self.argv = list(settings["cli_argv"])
        if settings.get("cli_profile"):
            self.argv += ["--profile", settings["cli_profile"]]
        self.environment = isolated_environment(settings)
        self.authenticated = False

    def app_id(self):
        return self.settings["expected_app_id"]

    def _run(self, args, body=None):
        try:
            result = self.process(self.argv + args, dumps(body) if body is not None else "",
                cwd=self.settings["cli_config_dir"], env=self.environment,
                inherit_env=False, timeout=self.settings["cli_timeout"], max_bytes=4 * 1024 * 1024)
        except ProcessLaunchError:
            raise APIError("lark_cli could not start; no request was launched") from None
        except (OSError, ValueError):
            raise AmbiguousError("lark_cli interrupted; inspect remote state before retry") from None
        if result.timed_out or result.truncated or result.returncode < 0 or result.invalid_utf8:
            raise AmbiguousError("lark_cli timeout, interruption, output limit or invalid UTF-8; no automatic retry")
        return result

    @staticmethod
    def _json(raw):
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError("duplicate JSON key")
                value[key] = item
            return value
        def constant(_):
            raise ValueError("non-finite JSON constant")
        try:
            return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        except (ValueError, TypeError):
            raise AmbiguousError("lark_cli returned invalid JSON; inspect remote state") from None

    @staticmethod
    def _reject(result):
        # Only explicit, typed rejections establish that an operation was refused.
        envelope = LarkCLI._json(result.stderr)
        error = envelope.get("error") if type(envelope) is dict else None
        if (type(envelope) is dict and envelope.get("ok") is False and
                envelope.get("identity") in (None, "bot") and type(error) is dict):
            category, subtype = error.get("type"), error.get("subtype")
            definite = {"validation": {"invalid_argument", "failed_precondition", "command_unavailable"},
                        "authentication": {"token_missing", "token_invalid", "token_expired", "dpop_key_missing", "dpop_proof_failed"},
                        "authorization": {"missing_scope", "user_unauthorized", "app_scope_not_applied",
                                          "token_scope_insufficient", "app_unavailable", "app_disabled", "permission_denied"},
                        "config": {"invalid_client", "invalid_config", "not_configured"},
                        "api": {"invalid_parameters", "ownership_mismatch", "cross_tenant", "cross_brand"}}
            if not error.get("retryable") and subtype in definite.get(category, set()):
                # Never include server text, URLs, user identifiers, tokens or raw output.
                code = error.get("code")
                suffix = " code=%s" % code if type(code) is int else ""
                raise APIError("lark_cli rejected request: %s/%s%s" % (category, subtype, suffix))
        raise AmbiguousError("lark_cli request failed without a definite rejection; inspect remote state")

    def check_configuration(self):
        result = self._run(["config", "show"])
        if result.returncode != 0:
            self._reject(result)
        config = self._json(result.stdout)
        if type(config) is not dict or config.get("appId") != self.app_id() or config.get("brand") != "feishu":
            raise APIError("lark_cli selected App ID/brand does not match configured Feishu application")
        return True

    def authenticate(self):
        if self.authenticated:
            return
        try:
            self.check_configuration()
            self._api("GET", "/bot/v3/info")
        except AmbiguousError:
            raise APIError("lark_cli authentication preflight failed before any publication request; check doctor/config and bot permissions") from None
        self.authenticated = True

    def _api(self, method, path, body=None):
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
            raise APIError("invalid Feishu API path")
        args = ["api", method, "/open-apis" + parsed.path, "--as", "bot", "--format", "json"]
        if parsed.query:
            pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            if len(dict(pairs)) != len(pairs):
                raise APIError("duplicate API query keys")
            args += ["--params", dumps(dict(pairs))]
        if body is not None:
            args += ["--data", "-"]
        time.sleep(max(0, 0.4 - (time.monotonic() - self.last_request)))
        result = self._run(args, body)
        self.last_request = time.monotonic()
        if result.returncode != 0:
            self._reject(result)
        value = self._json(result.stdout)
        if (type(value) is not dict or value.get("ok") is not True or value.get("identity") != "bot" or
                value.get("dry_run", False) is not False or type(value.get("data")) is not dict):
            raise AmbiguousError("invalid lark_cli success envelope; inspect remote state")
        return value["data"]

    def request(self, method, path, body=None):
        self.authenticate()
        return self._api(method, path, body)

    def diagnose(self):
        installed = bool(shutil.which(self.argv[0]))
        result = {"transport": "lark_cli", "mode": "app", "installed": installed,
                  "app_id_matches": False, "configured": False, "live_tested": False,
                  "authentication": "not tested; config show only"}
        if installed:
            try:
                self.check_configuration()
                result.update(app_id_matches=True, configured=bool(self.env("receive_id_env", False)))
            except (APIError, AmbiguousError):
                result["diagnostic"] = "CLI configuration unavailable or App ID/brand mismatch; no API requests sent"
        return result
