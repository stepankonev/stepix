"""Pipeline files: any Markdown file. Its ```stepix blocks are TOML: a `[steps.NAME]` table
defines step NAME and the `[params]` table holds params, so each block's first line says what it
is, also on GitHub. Everything else is documentation."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .errors import INVALID, Problem, fail

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

STEP_ID = re.compile(r"[a-z0-9_]+")
REF = re.compile(r"\{params\.(\w+)\}|\{steps\.(\w+)\.(\w+)\}")
HEADER = re.compile(r"\s*\[\s*([^\[\]]+?)\s*\]\s*(#.*)?$")  # [params], [steps.download]
KEY = re.compile(r"\s*[\"']?([\w-]+)[\"']?\s*=")
ON_FAIL = ("stop", "continue", "warn")
FIELDS = {"run": str, "env": str, "requires": list, "cache": bool, "on_fail": str, "retries": int}


@dataclass
class Step:
    id: str
    line: int  # file line of its [steps.NAME] header
    run: str = ""
    env: str | None = None
    requires: list[str] = field(default_factory=list)
    cache: bool = False  # skipped once it succeeded only when its author says so
    on_fail: str = "stop"
    retries: int = 0
    lines: dict[str, int] = field(default_factory=dict)  # file line of each field

    def refs(self) -> list[tuple[str | None, str]]:
        """Placeholders in run: (None, param) or (step, output name)."""
        return [(None, m[1]) if m[1] else (m[2], m[3]) for m in REF.finditer(self.run)]


@dataclass
class Pipeline:
    path: Path  # the Markdown file
    params: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, Step] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def root(self) -> Path:
        """Commands run here, and paths are relative to it: the Markdown file's folder."""
        return self.path.parent

    def order(self) -> list[str]:
        """Steps in dependency order; ties keep file order."""
        done: list[str] = []
        left = list(self.steps)
        while left:
            ready = [s for s in left if all(n in done or n not in self.steps
                                            for n in self.steps[s].requires)]
            if not ready:  # a cycle: validation reports it
                return done + left
            done.append(ready[0])
            left.remove(ready[0])
        return done

    def ancestors(self, sid: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.steps[sid].requires) if sid in self.steps else []
        while stack:
            n = stack.pop()
            if n in self.steps and n not in seen:
                seen.add(n)
                stack.extend(self.steps[n].requires)
        return seen


def load(file: str | None) -> tuple[Pipeline, list[Problem]]:
    """Read and check a pipeline file (default: README.md here). Missing file: exit 2."""
    path = Path(file or "README.md")
    if not path.is_file():
        hint = "" if file else ": run stepix where your README.md is, or pass a .md file"
        raise fail(INVALID, "no_file", f"{path} not found{hint}")
    pipe, errors = parse(path.resolve(), str(path))
    return pipe, errors + validate(pipe, str(path))


def parse(path: Path, shown: str) -> tuple[Pipeline, list[Problem]]:
    pipe = Pipeline(path)
    errors: list[Problem] = []
    params_at: int | None = None

    def err(line: int, message: str, heading: str | None = None) -> None:
        errors.append(Problem("parse_error", message, file=shown, line=line, heading=heading))

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        err(1, f"not valid UTF-8 ({e.reason} at byte {e.start})")
        return pipe, errors
    for tok in MarkdownIt("commonmark").parse(text):
        words = tok.info.split() if tok.type == "fence" else []
        if words[:1] != ["stepix"]:
            continue
        start = tok.map[0] + 1
        if len(words) > 1:
            err(start, f"```stepix {' '.join(words[1:])}: the name goes inside the block, as "
                f"[steps.{words[1]}]")
            continue
        lines = _lines(tok)
        data = _toml(tok, shown, errors)
        if data is None:  # still note its steps, so steps that require them don't fail too
            data = {"steps": {t.split(".", 1)[1]: None for t in lines if t.startswith("steps.")}}
        for key in sorted(set(data) - {"params", "steps"}):
            at = next((s[""] for t, s in lines.items()
                       if (t == key or t.startswith(key + ".")) and "" in s), start)
            err(at, f"[{key}]: a ```stepix block holds [params] and [steps.NAME] tables")
        if "params" in data:
            at = lines.get("params", {}).get("", start)
            if params_at is None:
                params_at = at
                _parse_params(pipe, data["params"], at, shown, errors)
            else:
                err(at, f"[params] is already defined on line {params_at}", "params")
        steps = data.get("steps", {})
        if not isinstance(steps, dict):
            err(start, "steps hold tables: [steps.NAME]")
            steps = {}
        for sid, body in steps.items():
            section = lines.get(f"steps.{sid}", {})
            line = section.get("", start)
            if not STEP_ID.fullmatch(sid):
                err(line, f"[steps.{sid}]: a step name is lowercase letters, digits and _")
            elif sid in pipe.steps:
                err(line, f"duplicate step {sid!r}", f"steps.{sid}")
            elif body is None:
                pipe.steps[sid] = Step(sid, line)
            elif not isinstance(body, dict):
                err(line, f"steps.{sid} must be a table: [steps.{sid}]", f"steps.{sid}")
            else:
                pipe.steps[sid] = _parse_step(sid, line, body, section, shown, errors)
    if not pipe.steps and not errors:
        err(1, "no steps here: a step is a [steps.NAME] table in a ```stepix block")
    return pipe, errors


def _lines(tok: Token) -> dict[str, dict[str, int]]:
    """File lines in a block, per table ("params", "steps.NAME"): its header ("") and keys."""
    found: dict[str, dict[str, int]] = {}
    table = ""
    for n, text in enumerate(tok.content.split("\n")):
        line = tok.map[0] + 2 + n
        if m := HEADER.match(text):
            table = re.sub(r"[\s\"']", "", m[1])
            found.setdefault(table, {})[""] = line
        elif m := KEY.match(text):
            found.setdefault(table, {}).setdefault(m[1], line)
    return found


def _toml(tok: Token, shown: str, errors: list[Problem]) -> dict[str, Any] | None:
    try:
        return tomllib.loads(tok.content)
    except tomllib.TOMLDecodeError as e:
        m = re.search(r"at line (\d+)", str(e))
        line = tok.map[0] + 1 + int(m.group(1)) if m else tok.map[1] - 1
        msg = re.sub(r"\s*\((at line \d+, column \d+|at end of document)\)", "", str(e))
        errors.append(Problem("parse_error", f"invalid TOML: {msg}", file=shown, line=line))
        return None


def _parse_params(pipe: Pipeline, params: Any, line: int, shown: str,
                  errors: list[Problem]) -> None:
    if isinstance(params, dict) and all(isinstance(v, (str, int, float, bool))
                                        for v in params.values()):
        pipe.params = params
    else:
        errors.append(Problem("invalid_field", "[params] maps names to strings, numbers or "
                              "booleans", file=shown, line=line, heading="params"))


def _parse_step(sid: str, line: int, data: dict[str, Any], section: dict[str, int], shown: str,
                errors: list[Problem]) -> Step:
    step = Step(sid, line, lines={k: v for k, v in section.items() if k})

    def bad(key: str, message: str) -> None:
        errors.append(Problem("invalid_field", message, file=shown, line=step.lines.get(key, line),
                              heading=f"steps.{sid}"))

    for key in sorted(set(data) - set(FIELDS)):
        bad(key, f"unknown step field {key!r} (fields: {', '.join(FIELDS)})")
    for key, kind in FIELDS.items():
        if key not in data:
            continue
        value = data[key]
        ok = isinstance(value, kind) and not (kind is int and isinstance(value, bool))
        if ok and kind is list:
            ok = all(isinstance(x, str) for x in value)
        if ok:
            setattr(step, key, value)
        else:
            bad(key, f"{key} must be " + {str: "a string", list: "a list of strings",
                                          int: "an integer", bool: "true or false"}[kind])
    if not step.run.strip():
        bad("run", "a step needs `run`, the command it runs")
    if step.on_fail not in ON_FAIL:
        bad("on_fail", f"on_fail must be one of: {', '.join(ON_FAIL)}")
    if step.retries < 0:
        bad("retries", "retries must be >= 0")
    return step


def validate(pipe: Pipeline, shown: str) -> list[Problem]:
    """Checks across steps: requires, cycles, placeholders, env folders."""
    errors: list[Problem] = []

    def report(step: Step, kind: str, message: str, line: int | None = None,
               fix: str | None = None) -> None:
        errors.append(Problem(kind, message, fix=fix, file=shown, line=line or step.line,
                              heading=f"steps.{step.id}"))

    graph = {s.id: list(dict.fromkeys(n for n in s.requires if n in pipe.steps))
             for s in pipe.steps.values()}
    order = list(pipe.steps)
    while True:  # every cycle: report one, break it, look again
        try:
            tuple(TopologicalSorter(graph).static_order())
            break
        except CycleError as e:
            loop = e.args[1][::-1][:-1]  # graphlib lists a required step first: flip it
            first = min(range(len(loop)), key=lambda i: order.index(loop[i]))
            loop = loop[first:] + loop[:first]  # start at the step that comes first in the file
            pairs = list(zip(loop, loop[1:] + loop[:1]))
            report(pipe.steps[loop[0]], "cycle", "dependency cycle: " + ", ".join(
                f"{a} requires {'itself' if a == b else b}" for a, b in pairs),
                pipe.steps[loop[0]].lines.get("requires"))
            graph[loop[0]].remove(pairs[0][1])
    for step in pipe.steps.values():
        for n in step.requires:
            if n not in pipe.steps:
                report(step, "unknown_step", f"requires {n!r}, which is not a step in this file",
                       step.lines.get("requires"))
        upstream = pipe.ancestors(step.id)
        for sid, name in step.refs():
            if sid is None and name not in pipe.params:
                report(step, "unknown_param", f"{{params.{name}}} is not declared in [params]",
                       step.lines.get("run"))
            elif sid is not None and sid not in upstream:
                report(step, "unknown_step", f"{{steps.{sid}.{name}}}: {sid!r} is not before "
                       f"{step.id!r} (add it to requires)", step.lines.get("run"))
        if step.env and not (pipe.root / step.env / "pyproject.toml").is_file():
            report(step, "missing_env", f"env {step.env!r} is not a uv project (no pyproject.toml)",
                   step.lines.get("env"), fix=f"uv init --bare {step.env}")
    return errors
