"""The `.stepix/` folder: plain files that humans, agents and future tools all read.

    .stepix/.gitignore                     "*": everything here is per machine
    .stepix/state.json                     per step: last state, when it ran, params, outputs
    .stepix/runs/<id>/manifest.json        the receipt of one run
    .stepix/runs/<id>/logs/<step>.log      each step's output
    .stepix/lib/stepix/__init__.py         `from stepix import output` for steps
    .stepix/locks/                         one run per pipeline at a time
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is out of scope for v0
    fcntl = None  # type: ignore[assignment]

STATE_SCHEMA = "stepix/state/v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seconds_since(stamp: str) -> float:
    then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - then).total_seconds()


def atomic_write(path: Path, text: str) -> None:
    """Write to a temp file in the same dir, then rename: readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, default=str) + "\n")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = root / ".stepix"

    def ensure(self) -> Path:
        if not (self.dir / ".gitignore").exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / ".gitignore").write_text("# stepix state, logs and receipts\n*\n")
        return self.dir

    @contextmanager
    def lock(self, name: str, wait: bool = True) -> Iterator[bool]:
        """Exclusive flock on .stepix/locks/<name>.lock. Yields False if busy and wait=False."""
        path = self.ensure() / "locks" / f"{name}.lock"
        path.parent.mkdir(exist_ok=True)
        with open(path, "a") as f:
            if fcntl is None:
                yield True
                return
            try:
                fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def state(self) -> dict[str, Any]:
        data = read_json(self.dir / "state.json")
        if isinstance(data, dict) and isinstance(data.get("pipelines"), dict):
            return data
        return {"schema": STATE_SCHEMA, "pipelines": {}}

    def pipeline_state(self, name: str) -> dict[str, Any]:
        found = self.state()["pipelines"].get(name) or {}
        return {"steps": found.get("steps") or {}}

    def save_pipeline_state(self, name: str, pstate: dict[str, Any]) -> None:
        with self.lock("state"):
            data = self.state()
            data["pipelines"][name] = pstate
            write_json(self.dir / "state.json", data)

    def helper(self) -> Path:
        """A folder with a copy of stepix/__init__.py (standard library only, it defines
        `output()`), put on PYTHONPATH so `from stepix import output` works in every step."""
        target = self.ensure() / "lib" / "stepix" / "__init__.py"
        source = Path(__file__).with_name("__init__.py").read_text()
        if not target.is_file() or target.read_text() != source:
            atomic_write(target, source)
        return target.parent.parent

    def new_run(self) -> tuple[str, Path]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]  # sorts by time
        run_id = f"{stamp}-{secrets.token_hex(3)}"
        run_dir = self.ensure() / "runs" / run_id
        (run_dir / "logs").mkdir(parents=True)
        return run_id, run_dir
