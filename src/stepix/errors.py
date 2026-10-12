"""Exit codes, and the one shape for problems: errors in a pipeline file, missing outputs."""

from __future__ import annotations

import functools
import os
import re
import sys
from dataclasses import asdict, dataclass
from typing import Any

OK, FAILED, INVALID, INTERRUPTED = 0, 1, 2, 130
STATUS = {OK: "ok", FAILED: "failed", INVALID: "invalid", INTERRUPTED: "interrupted"}


@dataclass
class Problem:
    """Something wrong. `fix` is the exact command (or action) that resolves it, when known."""

    kind: str
    message: str
    fix: str | None = None
    file: str | None = None
    line: int | None = None
    heading: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


class StepixError(Exception):
    """Stops a command before it does anything, with an exit code and the reasons."""

    def __init__(self, code: int, problems: list[Problem]) -> None:
        super().__init__("; ".join(p.message for p in problems))
        self.code = code
        self.problems = problems


def fail(code: int, kind: str, message: str, **fields: Any) -> StepixError:
    return StepixError(code, [Problem(kind, message, **fields)])


@functools.cache
def _invocation() -> str:
    """How the user started stepix, so printed commands can be pasted as they are."""
    if os.environ.get("STEPIX_CMD"):
        return os.environ["STEPIX_CMD"]
    if re.search(r"[/\\]archive-v\d+[/\\]", sys.prefix):  # uvx's throwaway env
        return "uvx stepix"
    return "uv run stepix" if os.path.basename(sys.prefix) == ".venv" else "stepix"


def stepix(*args: str) -> str:
    """A stepix command line, e.g. `uvx stepix run --from train`."""
    return " ".join([_invocation(), *args])
