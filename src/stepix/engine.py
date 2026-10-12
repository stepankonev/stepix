"""Running a pipeline: which steps run and why, outputs between steps, state, receipts.

A step runs every time unless it has `cache = true`. Then it is skipped ("cached") once it has
succeeded, unless a param it uses changed, a step it requires ran, or `--no-cache` was given.
`--step` and `--from` only choose which steps the run covers.
"""

from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import __version__
from .envs import child_env, command, find_uv, stream, uv_version
from .errors import FAILED, INTERRUPTED, INVALID, OK, Problem, fail, stepix
from .spec import REF, Pipeline, Step
from .store import Store, now, write_json

OUTPUT = re.compile(r"::stepix::output\s+([A-Za-z_]\w*)=(.*)$")
EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Interrupt|Exit)\b")
ERROR_WORD = re.compile(r"(?i)error|fatal|traceback|exception|failed|×")
TAIL = 30  # log lines in a failure summary
HEADER = 3  # lines stepix writes at the top of every step log
NOT_DONE = {"failed": "failed last time", "warned": "failed last time (on_fail = warn)",
            "running": "was interrupted last time", "invalidated": "the last run stopped before it"}


@dataclass
class RunOptions:
    file: str | None = None  # the pipeline file as the user typed it; None = README.md
    step: str | None = None  # --step: only this step
    start: str | None = None  # --from: this step and every step after it
    no_cache: bool = False  # --no-cache: run them even if they succeeded before


class MissingOutput(Exception):
    def __init__(self, step: str, name: str) -> None:
        super().__init__(f"step {step!r} did not output {name!r}")
        self.step, self.name = step, name


# -------------------------------------------------------------------------------------- planning

@dataclass
class Plan:
    pipe: Pipeline
    opts: RunOptions
    params: dict[str, Any]
    selected: list[str]  # dependency order
    reasons: dict[str, str | None]  # why each step runs; None = cached
    pstate: dict[str, Any]

    def entry(self, sid: str) -> dict[str, Any]:
        return self.pstate["steps"].get(sid) or {}

    def resume(self) -> str:
        """The command that finishes this run: the same one without --no-cache, since cached
        steps that succeeded are skipped now."""
        args = ["run", *filter(None, [self.opts.file])]
        args += ["--step", self.opts.step] if self.opts.step else []
        args += ["--from", self.opts.start] if self.opts.start else []
        return stepix(*map(shlex.quote, args))


def select(pipe: Pipeline, opts: RunOptions) -> list[str]:
    """The steps of this run, in order: all of them, --step STEP alone, or --from STEP on."""
    name = opts.step or opts.start
    if name and name not in pipe.steps:
        raise fail(INVALID, "unknown_step", f"{name!r} is not a step of {pipe.name} (steps: "
                   f"{', '.join(pipe.steps)})")
    order = pipe.order()
    if opts.step:
        return [opts.step]
    return order[order.index(opts.start):] if opts.start else order


def used_params(step: Step, params: dict[str, Any]) -> dict[str, Any]:
    return {name: params[name] for sid, name in step.refs() if sid is None and name in params}


def interpolate(text: str, params: dict[str, Any], outputs: dict[str, dict[str, str]],
                quote: bool) -> str:
    def sub(m: re.Match[str]) -> str:
        if m[1]:
            v = params[m[1]]
            value = str(v).lower() if isinstance(v, bool) else str(v)
        elif m[3] in outputs.get(m[2], {}):
            value = str(outputs[m[2]][m[3]])
        else:
            raise MissingOutput(m[2], m[3])
        return shlex.quote(value) if quote else value

    return REF.sub(sub, text)


def why_run(step: Step, entry: dict[str, Any], no_cache: bool, used: dict[str, Any],
            upstream: list[str]) -> str | None:
    """Why the step runs this time, or None when it is skipped as cached."""
    if no_cache:
        return "--no-cache"
    if not step.cache:
        return "not cached"
    if entry.get("last_state") != "done":
        return NOT_DONE.get(entry.get("last_state") or "", "never ran")
    old = entry.get("params", {})
    changed = sorted(k for k in {*old, *used} if old.get(k) != used.get(k))
    if changed:
        return "changed: " + ", ".join(f"params.{k}" for k in changed)
    return f"{upstream[0]} reran" if upstream else None


def make_plan(pipe: Pipeline, opts: RunOptions, store: Store) -> Plan:
    selected = select(pipe, opts)
    params = pipe.params
    pstate = store.pipeline_state(pipe.name)
    for sid in selected:  # a step it requires that this run skips must have succeeded before
        for n in pipe.steps[sid].requires:
            last = (pstate["steps"].get(n) or {}).get("last_state")
            if n not in selected and last not in ("done", "warned"):
                raise fail(INVALID, "requires_unfinished", f"{sid!r} requires {n!r}, which hasn't "
                           "succeeded yet", fix=stepix("run", *filter(None, [opts.file])))
    reasons: dict[str, str | None] = {}
    for sid in selected:
        step = pipe.steps[sid]
        upstream = [n for n in step.requires if reasons.get(n)]
        reasons[sid] = why_run(step, pstate["steps"].get(sid) or {}, opts.no_cache,
                               used_params(step, params), upstream)
    return Plan(pipe, opts, params, selected, reasons, pstate)


# ------------------------------------------------------------------------------------- execution

class View(Protocol):
    def start(self, plan: Plan, logs: str) -> None: ...
    def update(self, sid: str, info: dict[str, Any]) -> None: ...
    def line(self, sid: str, text: str) -> None: ...
    def stop(self) -> None: ...


@dataclass
class RunResult:
    code: int
    plan: Plan
    run_id: str | None = None
    run_dir: Path | None = None
    states: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        plan, steps = self.plan, []
        for sid in plan.selected:
            info, planned = self.states.get(sid, {}), plan.reasons[sid]
            item = {"step": sid, "state": info.get("state") or ("pending" if planned else "cached")}
            item.update({k: info[k] for k in ("duration_s", "exit_code") if k in info})
            reason = info.get("reason", planned)  # a skipped step says which step failed
            if reason:
                item["reason"] = reason
            if item["state"] == "cached" and plan.entry(sid).get("last_run_at"):
                item["last_run_at"] = plan.entry(sid)["last_run_at"]
            steps.append(item)
        out: dict[str, Any] = {"pipeline": plan.pipe.name, "steps": steps}
        if self.run_id and self.run_dir:
            out.update(run_id=self.run_id, run_dir=_rel(plan.pipe.root, self.run_dir))
        if self.failures:
            out["failures"] = self.failures
        return out


def run(pipe: Pipeline, opts: RunOptions, store: Store, view: View) -> RunResult:
    find_uv()
    with store.lock(f"run-{pipe.name}", wait=False) as free:
        if not free:
            raise fail(INVALID, "run_in_progress", f"{pipe.name} is already running")
        return Execution(make_plan(pipe, opts, store), store, view).run()


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


class Execution:
    """One run: executes the plan step by step, keeping state.json and the receipt current."""

    def __init__(self, plan: Plan, store: Store, view: View) -> None:
        self.plan, self.store, self.view = plan, store, view
        self.root = plan.pipe.root
        self.lib = store.helper()  # `from stepix import output` works in every step
        self.run_id, self.run_dir = store.new_run()
        self.live: dict[str, dict[str, Any]] = {s: {"state": "pending"} for s in plan.selected}
        self.details: dict[str, dict[str, Any]] = {}
        self.failures: list[dict[str, Any]] = []
        for sid in plan.selected:  # if this run stops early, these still run next time
            entry = plan.pstate["steps"].get(sid) or {}
            if plan.reasons[sid] and entry.get("last_state") == "done":
                entry["last_state"] = "invalidated"
        self.manifest: dict[str, Any] = {
            "run_id": self.run_id, "pipeline": plan.pipe.name, "params": plan.params,
            "status": "running", "exit_code": None, "started_at": now(), "ended_at": None,
            "git": git_info(self.root),
            "versions": {"stepix": __version__, "uv": uv_version(),
                         "python": platform.python_version(), "os": platform.platform()},
            "steps": []}

    def save(self) -> None:
        self.store.save_pipeline_state(self.plan.pipe.name, self.plan.pstate)
        self.manifest["steps"] = [self.details.get(s) or {"step": s, **self.live[s]}
                                  for s in self.plan.selected]
        write_json(self.run_dir / "manifest.json", self.manifest)

    def set_state(self, sid: str, state: str, **info: Any) -> None:
        self.live[sid].update(state=state, **info)
        self.view.update(sid, self.live[sid])
        self.save()

    def run(self) -> RunResult:
        plan, code = self.plan, OK
        self.save()
        self.view.start(plan, _rel(self.root, self.run_dir / "logs" / "<step>.log"))
        try:
            stopped = False
            for sid in plan.selected:
                step = plan.pipe.steps[sid]
                broken = [n for n in step.requires  # steps outside this run succeeded before
                          if self.live.get(n, {}).get("state") in ("failed", "skipped")]
                if broken:
                    self.set_state(sid, "skipped", reason=f"{broken[0]} failed")
                elif stopped:
                    continue  # on_fail = "stop": steps that don't depend on it stay pending
                elif plan.reasons[sid] is None:
                    done = {k: plan.entry(sid).get(k) for k in ("last_run_at", "last_run_id")}
                    self.details[sid] = {"step": sid, "state": "cached", **done}
                    self.set_state(sid, "cached", last_run_at=done["last_run_at"])
                else:
                    stopped = self.run_step(step) == "stopped"
        except KeyboardInterrupt:
            code = INTERRUPTED
        finally:
            if code != INTERRUPTED:
                code = FAILED if any(i["state"] == "failed" for i in self.live.values()) else OK
            status = {OK: "ok", FAILED: "failed", INTERRUPTED: "interrupted"}[code]
            self.manifest.update(status=status, exit_code=code, ended_at=now())
            self.save()
            self.view.stop()
        return RunResult(code, plan, self.run_id, self.run_dir, self.live, self.failures)

    def run_step(self, step: Step) -> str | None:
        """Run one step's command. Returns the on_fail outcome, if it failed."""
        sid, plan, root = step.id, self.plan, self.root
        entry = plan.pstate["steps"].setdefault(sid, {})
        entry["last_state"] = "running"
        started, t0 = now(), time.time()
        self.set_state(sid, "running", started_at=started, reason=plan.reasons[sid])
        outputs = {n: plan.entry(n).get("outputs") or {} for n in plan.pipe.ancestors(sid)}
        log_path = self.run_dir / "logs" / f"{sid}.log"
        emitted: dict[str, str] = {}
        problems: list[Problem] = []
        rc: int | None = None
        error: str | None = "interrupted"
        cmd, attempts = "", 0
        with open(log_path, "w", encoding="utf-8") as log:
            def emit(text: str) -> None:
                log.write(text + "\n")
                log.flush()
                self.view.line(sid, text)

            def on_line(text: str) -> None:
                if m := OUTPUT.match(text.strip()):
                    emitted[m[1]] = m[2].strip()
                emit(text)

            try:
                try:
                    cmd = interpolate(step.run, plan.params, outputs, quote=True)
                except MissingOutput as e:
                    fix = f"make step {e.step!r} print ::stepix::output {e.name}=..."
                    problems = [Problem("missing_output", str(e), fix=fix)]
                shown = (cmd or step.run).replace("\n", " ")
                log.write(f"# stepix {__version__} | run {self.run_id} | step {sid}\n"
                          f"# command: {shown}\n# env: {step.env or '(none)'}\n")
                for p in problems:
                    emit(f"[stepix] {p.message}" + (f"  (fix: {p.fix})" if p.fix else ""))
                if problems:
                    error = problems[0].kind  # missing_output
                else:  # error stays "interrupted" until the command finishes
                    argv = command(step.env, cmd)
                    pythonpath = [str(self.lib), *filter(None, [os.environ.get("PYTHONPATH")])]
                    env = child_env(STEPIX="1", STEPIX_STEP=sid, STEPIX_RUN_ID=self.run_id,
                                    PYTHONPATH=os.pathsep.join(pythonpath))
                    for attempts in range(1, step.retries + 2):
                        if attempts > 1:
                            emit(f"[stepix] retry {attempts - 1} of {step.retries}")
                            emitted.clear()
                        rc = stream(argv, root, env, on_line)
                        if rc == 0:
                            break
                    error = "exit_code" if rc else None
            finally:
                ended = now()
                warn = step.on_fail == "warn" and error not in (None, "interrupted")
                state = "done" if error is None else "warned" if warn else "failed"
                entry.update(last_state=state, last_run_at=ended, last_run_id=self.run_id)
                if state != "failed":
                    entry["outputs"] = emitted
                if state == "done":
                    entry["params"] = used_params(step, plan.params)
                outcome = None if error is None else "stopped" if error == "interrupted" else {
                    "stop": "stopped", "continue": "continued", "warn": "warned"}[step.on_fail]
                timing = {"started_at": started, "ended_at": ended,
                          "duration_s": round(time.time() - t0, 3), "exit_code": rc}
                self.details[sid] = {"step": sid, "state": state, "reason": plan.reasons[sid],
                                     "command": cmd, "env": step.env, **timing,
                                     "attempts": attempts, "outputs": emitted,
                                     "log": _rel(self.run_dir, log_path)}
                if error:
                    self.failures.append(self.summary(step, state, rc, error, cmd, problems,
                                                      log_path))
                self.set_state(sid, state, **timing)
        return outcome

    def summary(self, step: Step, state: str, rc: int | None, error: str, cmd: str,
                problems: list[Problem], log_path: Path) -> dict[str, Any]:
        """What failed and how to continue, without opening the log."""
        out: dict[str, Any] = {"step": step.id, "state": state, "reason": error,
                               "log": _rel(self.root, log_path), "resume": self.plan.resume()}
        if problems:
            out["problems"] = [p.to_dict() for p in problems]
            return out
        lines = log_path.read_text(encoding="utf-8", errors="replace").split("\n")[HEADER:]
        lines = lines[:-1] if lines and not lines[-1] else lines
        out.update(command=cmd, exit_code=rc, error=error_line(lines), log_tail=lines[-TAIL:])
        return out


def error_line(lines: list[str]) -> str | None:
    """The last exception line (`ValueError: ...`), else the last line that looks like an error."""
    for pattern in (EXCEPTION_LINE, ERROR_WORD):
        for line in reversed(lines):
            if pattern.search(line.strip()):
                return line.strip()
    return None


def git_info(root: Path) -> dict[str, Any]:
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30)

    try:
        head = git("rev-parse", "HEAD")
        if head.returncode != 0:
            return {"commit": None, "dirty": None}
        dirty = bool(git("status", "--porcelain", "--untracked-files=no").stdout.strip())
        return {"commit": head.stdout.strip(), "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}
