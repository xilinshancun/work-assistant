"""Bounded subprocess execution; no shell, process group cleanup, no implicit retries."""
import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


class TerminationRequested(KeyboardInterrupt):
    """SIGTERM requests the same journal/child cleanup as a keyboard interrupt."""


class ProcessLaunchError(OSError):
    """Popen failed before returning a process; no worker was launched."""


@contextmanager
def termination_guard():
    if not hasattr(signal, "SIGTERM") or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        # Repeated termination must not interrupt the first signal's cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise TerminationRequested()
    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@dataclass
class ProcessResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False
    truncated: bool = False
    latency_ms: int = 0
    diagnostics: list = field(default_factory=list)
    invalid_utf8: bool = False


def subprocess_environment(env=None, inherit_env=True):
    """Drop only corrupt Paseo metadata; fail before launch on other bad entries."""
    overlay = os.environ.copy() if inherit_env else {}
    overlay.update(env or {})
    diagnostics = []
    for key, value in list(overlay.items()):
        if type(key) is not str or type(value) is not str or "\x00" in key or "=" in key or "\x00" in value:
            raise ProcessLaunchError("invalid environment entry; repair local environment/config before running")
        try:
            key.encode("utf-8", "strict")
            value.encode("utf-8", "strict")
        except UnicodeEncodeError:
            if key.startswith("PASEO_"):
                del overlay[key]
                safe_key = key.encode("ascii", "backslashreplace").decode("ascii")
                diagnostics.append("removed non-UTF8 Paseo metadata: " + safe_key)
            else:
                safe_key = key.encode("ascii", "backslashreplace").decode("ascii")
                raise ProcessLaunchError("invalid UTF-8 environment entry: " + safe_key + "; repair local environment/config")
    return overlay, diagnostics


def execute(argv, prompt, cwd, env=None, timeout=600, pass_fds=(), max_bytes=2 * 1024 * 1024, inherit_env=True):
    if type(argv) is not list or not argv or any(type(a) is not str for a in argv):
        raise ValueError("argv must be a string list")
    started = time.monotonic()
    overlay, diagnostics = subprocess_environment(env, inherit_env=inherit_env)
    kwargs = {"cwd": str(cwd), "env": overlay, "stdin": subprocess.PIPE,
              "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "shell": False}
    if os.name == "posix":
        kwargs.update(start_new_session=True, pass_fds=tuple(pass_fds))
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        raise ProcessLaunchError(str(exc)) from exc
    output = [bytearray(), bytearray()]
    overflow = threading.Event()

    def read(stream, index):
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            room = max_bytes - len(output[index])
            output[index].extend(chunk[:max(0, room)])
            if len(chunk) > room:
                overflow.set()
        stream.close()

    def feed():
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    threads = [threading.Thread(target=read, args=(proc.stdout, 0), daemon=True),
               threading.Thread(target=read, args=(proc.stderr, 1), daemon=True),
               threading.Thread(target=feed, daemon=True)]
    for thread in threads:
        thread.start()

    def kill():
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        # Also kill descendants left behind after their group leader exits.
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            elif proc.poll() is None:
                proc.kill()
        except ProcessLookupError:
            pass
        proc.wait()

    timed_out = False
    try:
        deadline = started + timeout
        while proc.poll() is None:
            if time.monotonic() >= deadline or overflow.is_set():
                timed_out = time.monotonic() >= deadline
                kill()
                break
            time.sleep(0.01)
        # A child could still hold stdout/stderr; bound the drain too.
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(t.is_alive() for t in threads):
            timed_out = True
            kill()
            for thread in threads:
                thread.join(timeout=1)
    except BaseException:
        kill()
        raise
    invalid_utf8 = False
    decoded = []
    for raw in output:
        try:
            decoded.append(raw.decode("utf-8", "strict"))
        except UnicodeDecodeError:
            invalid_utf8 = True
            decoded.append(raw.decode("utf-8", "replace"))
    return ProcessResult(decoded[0], decoded[1], proc.returncode, timed_out, overflow.is_set(),
                         round((time.monotonic() - started) * 1000), diagnostics, invalid_utf8)
