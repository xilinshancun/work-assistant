"""Bounded subprocess execution; no shell, process group cleanup, no implicit retries."""
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass
class ProcessResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False
    truncated: bool = False
    latency_ms: int = 0


def execute(argv, prompt, cwd, env=None, timeout=600, pass_fds=(), max_bytes=2 * 1024 * 1024):
    if type(argv) is not list or not argv or any(type(a) is not str for a in argv):
        raise ValueError("argv must be a string list")
    started = time.monotonic()
    overlay = os.environ.copy()
    overlay.update(env or {})
    kwargs = {"cwd": str(cwd), "env": overlay, "stdin": subprocess.PIPE,
              "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "shell": False}
    if os.name == "posix":
        kwargs.update(start_new_session=True, pass_fds=tuple(pass_fds))
    proc = subprocess.Popen(argv, **kwargs)
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
    return ProcessResult(output[0].decode("utf-8", "replace"), output[1].decode("utf-8", "replace"),
                         proc.returncode, timed_out, overflow.is_set(), round((time.monotonic() - started) * 1000))
