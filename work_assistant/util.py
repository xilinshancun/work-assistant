import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex[:20]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    data = value if isinstance(value, bytes) else dumps(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def redact(text):
    text = str(text).replace(str(Path.home()), "[HOME]")
    for key, value in os.environ.items():
        if re.search(r"(?i)(secret|token|password|api_?key|webhook)", key) and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"/(?:Users|home)/[^/\s\"<>]+", "[HOME]", text)
    text = re.sub(r"https://[^\s\"<>]*/(?:bot/v2/hook|webhook)/[^\s\"<>]+", "[WEBHOOK]", text)
    text = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|password)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{15,})", "[REDACTED]", text)
    return text


def outbound_guard(text):
    if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", text):
        raise ValueError("outbound material contains an obvious credential; remove it from the source")


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()
