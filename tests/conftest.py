"""Test helpers: a throwaway project and a `stepix` runner that behaves like an agent's shell
(no terminal, stdin closed, hard timeout)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent


@dataclass
class Result:
    code: int
    out: str  # what the user sees: stdout, then stderr
    receipt: dict[str, Any] | None  # the run's manifest.json, if a run started

    @property
    def steps(self) -> dict[str, str]:
        """Each step's state in this run."""
        return {s["step"]: s["state"] for s in (self.receipt or {}).get("steps", [])}


class Project:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        self.env.update(STEPIX_CMD="stepix", PYTHONIOENCODING="utf-8")

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"))
        return path

    def ran(self) -> list[str]:
        """Steps that really executed, sorted: test steps append their name to ran.txt, and
        steps that don't depend on each other run at the same time, in no fixed order."""
        path = self.root / "ran.txt"
        lines = path.read_text().split() if path.exists() else []
        path.unlink(missing_ok=True)
        return sorted(lines)

    def receipts(self) -> set[Path]:
        return set(self.root.glob("**/.stepix/runs/*/manifest.json"))

    def stepix(self, *args: str, env: dict[str, str] | None = None, timeout: float = 120) -> Result:
        before = self.receipts()
        proc = subprocess.run([sys.executable, "-m", "stepix", *args], cwd=self.root,
                              stdin=subprocess.DEVNULL, text=True, capture_output=True,
                              timeout=timeout, env={**self.env, **(env or {})})
        new = sorted(self.receipts() - before)
        receipt = json.loads(new[-1].read_text()) if new else None
        return Result(proc.returncode, proc.stdout + proc.stderr, receipt)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    if not shutil.which("uv"):
        pytest.skip("uv is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    return Project(root)


def step(name: str, *, requires: tuple[str, ...] = (), run: str | None = None,
         cache: bool = True, extra: str = "") -> str:
    """A ```stepix block (under a prose heading) for step NAME, which records that it ran
    (ran.txt) and fails while a file fail_<name> exists. Cached unless cache=False."""
    cmd = run or f"echo {name} >> ran.txt && test ! -e fail_{name}"
    lines = [f"### Step {name}", "", "```stepix", f"[steps.{name}]", f"run = {json.dumps(cmd)}"]
    if cache:
        lines.append("cache = true")
    if requires:
        lines.append(f"requires = {json.dumps(list(requires))}")
    if extra:
        lines.append(textwrap.dedent(extra).strip())
    return "\n".join([*lines, "```", ""])


def pipeline(title: str, *steps: str, params: str = "") -> str:
    """A README.md: a title, prose, an optional params block, then the steps."""
    head = f"# {title}\n\nSome prose.\n\n## Install\n\nNot a step.\n\n"
    if params:
        head += "```stepix\n[params]\n" + textwrap.dedent(params).strip() + "\n```\n\n"
    return head + "\n".join(steps)
