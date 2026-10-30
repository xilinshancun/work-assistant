"""Create the bundled synthetic exercise, without loading config or invoking agents.

Canonical files live in resources/first_project inside this package. The checkout's
examples/first-project is an audited byte-identical mirror, enforced by tests.
There is deliberately no source-checkout fallback for missing installed resources.
"""
import hashlib
from importlib import resources
from pathlib import Path
import sys

FILES = ("README.md", "count_records.py", "records.txt")


def create_example(output):
    source = resources.files("work_assistant").joinpath("resources").joinpath("first_project")
    try:
        payloads = {name: source.joinpath(name).read_bytes() for name in FILES}
    except (OSError, FileNotFoundError):
        raise ValueError("Bundled first-project resources are unavailable; use a complete Work Assistant installation.") from None
    target = Path(output).expanduser().absolute()
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        raise ValueError("Example output must be a new directory; existing paths and symlinks are never overwritten.") from None
    for name, raw in payloads.items():
        with (target / name).open("xb") as stream:
            stream.write(raw)
    workspace = str(target.resolve())
    return {"schema": "wa.example.v1", "status": "created", "workspace": workspace,
            "files": list(FILES), "file_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()},
            "scope": "synthetic read-only practice; intentional README/implementation mismatch; no model invoked",
            "next_steps": [{"action": "register_read_only_project", "argv": [sys.executable, "-m", "work_assistant", "project", "add",
                "first-project", "--workspace", workspace, "--allow", "read"],
                "note": "Choose a new project name if first-project already exists. Inspect the exercise read-only with your own authenticated executor."}]}
