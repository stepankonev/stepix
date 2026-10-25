"""Running a step's command: inside its uv env, in its own process group, streaming its output."""

from __future__ import annotations

import functools
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .errors import INVALID, fail


@functools.cache
def find_uv() -> str:
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise fail(INVALID, "uv_missing", "uv is not installed",
                   fix="curl -LsSf https://astral.sh/uv/install.sh | sh")
    return uv


@functools.cache
def uv_version() -> str:
    out = subprocess.run([find_uv(), "--version"], capture_output=True, text=True).stdout
    return out.split()[1] if len(out.split()) > 1 else "unknown"


def command(env: str | None, cmd: str) -> list[str]:
    """`uv run --project ENV -- sh -c CMD`: uv builds the env from its lock on first use.
    Without an env, plain `uv run` finds the repo's own project, if there is one."""
    project = ["--project", env] if env else []
    return [find_uv(), "run", *project, "--", "sh", "-c", cmd]


def child_env(**extra: str) -> dict[str, str]:
    """The caller's environment (secrets included) minus VIRTUAL_ENV, which would confuse uv."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env.update(PYTHONUNBUFFERED="1", **extra)
    return env


def stream(argv: list[str], cwd: Path, env: dict[str, str], on_line: Callable[[str], None],
           stop: threading.Event | None = None) -> int:
    """Run argv with stdin closed and stderr merged into stdout, feeding each line to on_line.
    Background processes it leaves behind are stopped once it exits. When `stop` is set, it is
    interrupted and KeyboardInterrupt is raised, as on Ctrl+C."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", bufsize=1, start_new_session=True)
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    drain_until = None
    try:
        while True:
            if stop is not None and stop.is_set():
                raise KeyboardInterrupt
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is None:
                    continue
                drain_until = drain_until or time.monotonic() + 1  # a leftover may hold the pipe
                if time.monotonic() < drain_until:
                    continue
                break
            if line is None:
                break
            on_line(line.rstrip("\n"))
        return proc.wait()
    except BaseException:
        terminate(proc)
        raise
    finally:
        stop_group(proc.pid)


def terminate(proc: subprocess.Popen[str]) -> None:
    """Interrupt a running step: SIGINT to its process group, then SIGTERM/SIGKILL if needed."""
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=5)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        pass
    stop_group(proc.pid)


def stop_group(pgid: int, grace: float = 2) -> None:
    """SIGTERM a process group, then SIGKILL whatever is still there after `grace` seconds."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.05)
