"""Explicit user message transport; OAuth credentials remain in official securestore."""
import re
import time
from urllib.parse import parse_qsl, urlsplit

from .feishu import APIError, AmbiguousError
from .lark_cli import LarkCLI
from .util import dumps


class UserLarkCLI(LarkCLI):
    def __init__(self, settings, process=None):
        owner = settings.get("message_user_open_id")
        if (settings.get("transport") != "lark_cli" or type(owner) is not str
                or not re.fullmatch(r"ou_[A-Za-z0-9_-]{1,197}", owner)):
            raise APIError("user messages require lark_cli and a trusted message_user_open_id")
        super().__init__(settings, process=process)
        self.owner = owner

    def authenticate(self):
        if self.authenticated:
            return
        try:
            self.check_configuration()
            result = self._run(["auth", "status", "--json", "--verify"])
            if result.returncode != 0:
                raise APIError("user authentication verification failed before send")
            status = self._json(result.stdout)
            identities = status.get("identities") if type(status) is dict else None
            user = identities.get("user") if type(identities) is dict else None
            if (type(status) is not dict or status.get("appId") != self.app_id()
                    or status.get("brand") != "feishu" or status.get("identity") != "user"
                    or status.get("verified") is not True or type(user) is not dict
                    or user.get("status") != "ready" or user.get("tokenStatus") != "valid"
                    or user.get("openId") != self.owner or type(user.get("scope")) is not str
                    or not {"im:message", "im:message.send_as_user"} <= set(user["scope"].split())):
                raise APIError("user authentication, owner or required message scopes do not match")
        except AmbiguousError:
            raise APIError("user authentication preflight unavailable; no send request started") from None
        self.authenticated = True

    def _api(self, method, path, body=None):
        parsed = urlsplit(path)
        if (parsed.scheme or parsed.netloc or parsed.fragment
                or not ((method == "POST" and parsed.path == "/im/v1/messages")
                        or (method == "GET" and re.fullmatch(r"/im/v1/messages/[A-Za-z0-9_-]{1,200}", parsed.path)))):
            raise APIError("user transport supports message send and known-message readback only")
        args = ["api", method, "/open-apis" + parsed.path, "--as", "user", "--format", "json"]
        if parsed.query:
            pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            if len(dict(pairs)) != len(pairs):
                raise APIError("duplicate API query keys")
            args += ["--params", dumps(dict(pairs))]
        if body is not None:
            args += ["--data", "-"]
        time.sleep(max(0, 0.4 - (time.monotonic() - self.last_request)))
        result = self._run(args, body)
        self.last_request = time.monotonic()
        # No raw CLI output is included in diagnostics or persisted receipts.
        if result.returncode != 0:
            raise AmbiguousError("user message request failed; inspect known state, never automatically resend")
        value = self._json(result.stdout)
        if (type(value) is not dict or value.get("ok") is not True or value.get("identity") != "user"
                or value.get("dry_run", False) is not False or type(value.get("data")) is not dict):
            raise AmbiguousError("invalid user CLI success envelope; inspect known state")
        return value["data"]
