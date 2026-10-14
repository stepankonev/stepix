"""What people see: a run as it happens, then each command's result."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.text import Text

from .flow import Flow, clock

console = Console(highlight=False, soft_wrap=True)


def when(stamp: str | None) -> str:
    """A UTC timestamp from state.json as local time, e.g. 2026-10-07 15:06."""
    if not stamp:
        return ""
    local = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    return local.strftime("%Y-%m-%d %H:%M")


class PlainView:
    """No terminal: one line per state change. Step output stays in the logs."""

    def __init__(self) -> None:
        self.seen: dict[str, str] = {}

    def start(self, plan: Any, logs: str) -> None:
        print(f"[stepix] logs: {logs}", flush=True)  # where to follow a run, e.g. for agents
    def line(self, sid: str, text: str) -> None: ...
    def stop(self) -> None: ...

    def update(self, sid: str, info: dict[str, Any]) -> None:
        state = info["state"]
        if self.seen.get(sid) != state:
            self.seen[sid] = state
            took = f" {clock(info['duration_s'])}" if info.get("duration_s") is not None else ""
            why = info.get("reason") if state in ("running", "skipped") else None
            if state == "cached" and info.get("last_run_at"):
                why = f"last ran {when(info['last_run_at'])}"
            print(f"[stepix] {sid} {state}{took}" + (f" ({why})" if why else ""), flush=True)


class LiveView:
    """A terminal: the steps as boxes with lines between them, live, and the running step's
    output streaming above."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.live: Live | None = None

    def start(self, plan: Any, logs: str) -> None:
        console.print(Text(f"logs: {logs}", style="grey50"))
        self.items = {s: {"step": s, "state": "pending"} for s in plan.selected}
        self.live = Live(Flow(plan.pipe, plan.selected, self.items), console=console,
                         refresh_per_second=8)
        self.live.start()

    def update(self, sid: str, info: dict[str, Any]) -> None:
        self.items[sid].update(info)

    def line(self, sid: str, text: str) -> None:
        console.print(Text(f"{sid} │ ", style="grey50") + Text.from_ansi(text))

    def stop(self) -> None:
        if self.live:
            self.live.stop()


# ------------------------------------------------------------------------------------ renderers

def problem(p: dict[str, Any], mark: str = "✗", style: str = "red") -> None:
    where = f"{p['file']}:{p['line']}: " if p.get("file") and p.get("line") else ""
    heading = f"[{p['heading']}] " if p.get("heading") else ""
    console.print(Text(f"  {mark} ", style=style) + Text(f"{where}{heading}{p['message']}"))
    if p.get("fix"):
        console.print(Text("      fix: ", style="grey50") + Text(p["fix"], style="cyan"))


def render_problems(doc: dict[str, Any]) -> None:
    for p in doc.get("errors", []):
        problem(p)


def render_failure(f: dict[str, Any]) -> None:
    title = f"{'!' if f['state'] == 'warned' else '✗'} {f['step']} {f['state']}"
    if f.get("problems"):  # found before its command ran: a missing output
        console.print(Text(f"{title}: {f['reason'].replace('_', ' ')}", style="bold red"))
        for p in f["problems"]:
            problem(p)
    else:
        console.print(Text(f"{title} (exit {f.get('exit_code')})", style="bold red"))
        for key in ("command", "error", "log"):
            if f.get(key):
                console.print(Text(f"  {key:<8}", style="grey50") + Text(str(f[key])))
        if f.get("log_tail"):
            console.print(Text(f"  ── last {len(f['log_tail'])} log lines ──", style="grey50"))
            for line in f["log_tail"]:
                console.print(Text("  ") + Text.from_ansi(line))
    console.print(Text("  resume  ", style="grey50") + Text(f["resume"], style="cyan"))


def render_run(doc: dict[str, Any]) -> None:
    for f in doc.get("failures", []):
        render_failure(f)
    render_problems(doc)
    if doc.get("run_id"):
        counts: dict[str, int] = {}
        for s in doc["steps"]:
            counts[s["state"]] = counts.get(s["state"], 0) + 1
        summary = ", ".join(f"{n} {state}" for state, n in counts.items())
        style = "bold green" if doc["status"] == "ok" else "bold red"
        console.print(Text(f"{doc['status']}: {doc['pipeline']} ({summary})", style=style)
                      + Text(f"  log: {doc['run_dir']}", style="grey50"))


def render_validate(doc: dict[str, Any]) -> None:
    for p in doc["errors"]:
        problem(p)
    if not doc["errors"]:
        n = len(doc["steps"])
        console.print(Text(f"✓ {doc['pipeline']}: {n} step{'s' * (n != 1)}, valid", style="green"))
