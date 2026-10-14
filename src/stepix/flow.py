"""The live view of a run in a terminal: each step is a box, and a line runs from each value a
step outputs (●) to every step that uses it (▶), or from a step to the steps that require it."""

from __future__ import annotations

import time
from typing import Any

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text

from .spec import Pipeline, Step
from .store import seconds_since

COLOR = {"pending": "grey46", "running": "deep_sky_blue1", "done": "green3", "cached": "green4",
         "failed": "red3", "warned": "yellow3", "skipped": "grey46"}
ICON = {"pending": "·", "done": "✓", "cached": "✓", "failed": "✗", "warned": "!", "skipped": "-"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
GLYPH = {"lr": "─", "du": "│", "dr": "╭", "ru": "╰", "dl": "╮", "lu": "╯", "dlr": "┬", "lru": "┴",
         "dru": "├", "dlu": "┤", "dlru": "┼", "l": "─", "r": "─", "u": "│", "d": "│"}  # by joints


def clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ago(stamp: str) -> str:
    seconds = seconds_since(stamp)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def status(info: dict[str, Any]) -> str:
    state = info["state"]
    if state == "running" and info.get("started_at"):
        return "running " + clock(seconds_since(info["started_at"]))
    if state == "cached" and info.get("last_run_at"):
        return "cached " + ago(info["last_run_at"])
    if state in ("done", "failed", "warned") and info.get("duration_s") is not None:
        return f"{state} {clock(info['duration_s'])}"
    return "waiting" if state == "pending" else state


def spinner() -> str:
    return SPINNER[int(time.time() * 10) % len(SPINNER)]


class Canvas:
    """A character grid. Lines are drawn as connections, so crossings and joints pick the right
    box-drawing character; text is written over them."""

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.cells = [[(" ", "")] * w for _ in range(h)]
        self.links: list[list[tuple[set[str], str]]] = [[(set(), "")] * w for _ in range(h)]

    def text(self, x: int, y: int, s: str, style: str = "") -> None:
        for i, ch in enumerate(s):
            if 0 <= x + i < self.w and 0 <= y < self.h:
                self.cells[y][x + i] = (ch, style)

    def _link(self, x: int, y: int, dirs: str, style: str) -> None:
        self.links[y][x] = (self.links[y][x][0] | set(dirs), style)

    def hline(self, x0: int, x1: int, y: int, style: str) -> None:
        for x in range(x0, x1 + 1):
            self._link(x, y, ("l" if x > x0 else "") + ("r" if x < x1 else ""), style)

    def vline(self, x: int, y0: int, y1: int, style: str) -> None:
        for y in range(y0, y1 + 1):
            self._link(x, y, ("u" if y > y0 else "") + ("d" if y < y1 else ""), style)

    def lines(self) -> list[Text]:
        out = []
        for y in range(self.h):
            t = Text()
            for x in range(self.w):
                ch, style = self.cells[y][x]
                dirs, line_style = self.links[y][x]
                if ch == " " and dirs:
                    t.append(GLYPH["".join(sorted(dirs, key="dlru".index))], line_style)
                else:
                    t.append(ch, style)
            t.rstrip()
            out.append(t)
        return out


def lanes(spans: dict[Any, tuple[int, int]]) -> dict[Any, int]:
    """A lane per span, never shared by overlapping spans; short spans nearest the boxes."""
    used: list[list[tuple[int, int]]] = []
    lane = {}
    for key, (a, b) in sorted(spans.items(), key=lambda kv: kv[1][1] - kv[1][0]):
        k = next((k for k, taken in enumerate(used)
                  if all(b < t0 or a > t1 for t0, t1 in taken)), len(used))
        if k == len(used):
            used.append([])
        used[k].append((a, b))
        lane[key] = k
    return lane


class Flow:
    """The steps of a run as boxes with their inputs, command, outputs, environment and state.
    Rendered fresh on every refresh, so spinners and clocks move."""

    def __init__(self, pipe: Pipeline, selected: list[str], items: dict[str, dict[str, Any]]):
        self.pipe, self.selected, self.items = pipe, selected, items

    def state(self, sid: str) -> str:
        return self.items.get(sid, {}).get("state", "pending")

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lines, focus = self.draw(options.max_width)
        room = max(10, console.size.height - 8)  # leave room above for the step's output
        if len(lines) > room:  # too tall for the screen: keep the step at work in sight
            start = min(max(focus - 2, 0), len(lines) - room + 2)
            end = start + room - 2
            lines = ([Text("  ⋮", "grey50")] if start else []) + lines[start:end] + (
                [Text("  ⋮", "grey50")] if end < len(lines) else [])
        yield from lines
        yield Text("")
        yield self.summary()

    def draw(self, width: int) -> tuple[list[Text], int]:
        """The drawing, and the row to keep in sight: the box at work, else the next one."""
        steps = [self.pipe.steps[s] for s in self.selected]
        shown = set(self.selected)
        ins = {s.id: list(dict.fromkeys((name, src) for src, name in s.refs() if src))
               for s in steps}
        outs: dict[str, list[str]] = {s.id: [] for s in steps}
        users: dict[tuple[str, str], list[str]] = {}
        for t in steps:
            for value, src in ins[t.id]:
                if src in shown:
                    outs[src] += [value] if value not in outs[src] else []
                    users.setdefault((src, value), []).append(t.id)
        values = [(src, value, t.id) for t in steps for value, src in ins[t.id] if src in shown]
        plain = [(r, t.id) for t in steps for r in t.requires
                 if r in shown and not any(v[0] == r and v[2] == t.id for v in values)]
        top, bottom, row = {}, {}, {}
        y = 0
        for s in steps:
            top[s.id] = y
            y += 1
            for value, src in ins[s.id]:
                row[(s.id, "in", value, src)] = y
                y += 1
            y += 1  # the command
            for value in outs[s.id]:
                row[(s.id, "out", value)] = y
                y += 1
            bottom[s.id] = y
            y += 2
        spans: dict[Any, tuple[int, int]] = {
            v: (row[(v[0], "out", v[1])], row[(v[2], "in", v[1], v[0])]) for v in values}
        spans.update({p: (bottom[p[0]], top[p[1]]) for p in plain})
        lane = lanes(spans)
        bx = 2 * (max(lane.values()) + 1) + 2 if lane else 0
        names = max(len(s.id) for s in steps)
        bw = max(names + 26, min(72, width - bx - 1))
        c = Canvas(bx + bw, y - 1)
        for key, (ya, yb) in spans.items():
            x, color = bx - 3 - 2 * lane[key], COLOR[self.state(key[0])]
            c.hline(x, bx - 1, ya, color)  # from an output's ● or the bottom of the box
            c.vline(x, ya, yb, color)
            if len(key) == 3:  # into an input's ▶
                c.hline(x, bx - 1, yb, color)
            else:  # into the top of the box
                c.hline(x, bx - 2, yb, color)
                c.text(bx - 1, yb, "▶", color)
        for s in steps:
            self.box(c, s, bx, bw, top[s.id], bottom[s.id], ins[s.id], outs[s.id], users)
        at_work = [s for s in self.selected if self.state(s) == "running"] or [
            s for s in self.selected if self.state(s) == "pending"] or self.selected[-1:]
        return c.lines(), top[at_work[0]]

    def box(self, c: Canvas, s: Step, x: int, w: int, y0: int, y1: int,
            ins: list[tuple[str, str]], outs: list[str],
            users: dict[tuple[str, str], list[str]]) -> None:
        info = self.items.get(s.id) or {"state": "pending"}
        state = info["state"]
        color = COLOR[state]
        border = color + (" bold" if state == "running" else "")
        icon = spinner() if state == "running" else ICON[state]
        title, right = f"─ {icon} {s.id} ", f" {status(info)} ─"
        if len(title) + len(right) + 3 > w:
            right = " ─"
        c.text(x, y0, "╭" + title + "─" * (w - 2 - len(title) - len(right)) + right + "╮", border)
        c.text(x + 3, y0, icon, "bold " + color)
        quiet = state in ("pending", "skipped")
        c.text(x + 5, y0, s.id, "bold " + ("grey62" if quiet else "grey93"))
        c.text(x + w - len(right), y0, right[1:-2], color)
        for yy in range(y0 + 1, y1):
            c.text(x, yy, "│" + " " * (w - 2) + "│", border)

        def port(yy: int, mark: str, mark_style: str, value: str, hint: str) -> None:
            c.text(x, yy, mark, mark_style)
            c.text(x + 2, yy, value, "grey93")
            if len(value) + len(hint) + 6 <= w:
                c.text(x + w - 3 - len(hint), yy, hint, "grey46")

        yy = y0 + 1
        for value, src in ins:
            port(yy, "▶", COLOR[self.state(src)] if src in self.selected else "grey46", value,
                 f"from {src}")
            yy += 1
        command = "$ " + " ".join(s.run.split())
        c.text(x + 2, yy, command if len(command) <= w - 4 else command[:w - 5] + "…", "grey62")
        yy += 1
        for value in outs:
            port(yy, "●", color, value, "to " + ", ".join(users[(s.id, value)]))
            yy += 1
        env = f" {s.env or 'repo env'} "
        c.text(x, y1, "╰─" + env + "─" * (w - 3 - len(env)) + "╯", border)
        c.text(x + 2, y1, env, "grey50")

    def summary(self) -> Text:
        """One line under the boxes: what runs now, then counts."""
        t = Text()
        states = [self.state(s) for s in self.selected]
        for sid in self.selected:
            if self.state(sid) == "running":
                t.append(f"{spinner()} running ", COLOR["running"])
                took = status(self.items[sid]).split()[-1]
                t.append(f"{sid} {took}   ", "bold " + COLOR["running"])
        done, cached = states.count("done") + states.count("cached"), states.count("cached")
        if done:
            t.append(f"✓ {done} done" + (f" ({cached} cached)" if cached else "") + "   ",
                     COLOR["done"])
        for state, word in (("failed", "failed"), ("warned", "warned"), ("skipped", "skipped"),
                            ("pending", "waiting")):
            if states.count(state):
                t.append(f"{ICON[state]} {states.count(state)} {word}   ", COLOR[state])
        t.rstrip()
        return t
