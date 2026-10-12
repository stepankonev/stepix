"""The command line: `stepix run` and `stepix validate`, on one Markdown file (default: README.md
here)."""

from __future__ import annotations

import functools
import signal
import sys
from collections.abc import Callable
from typing import Any, NoReturn

import click

from . import __version__, engine, ui
from .engine import RunOptions
from .errors import INTERRUPTED, INVALID, OK, STATUS, StepixError
from .spec import Pipeline, load
from .store import Store


def finish(code: int, data: dict[str, Any], render: Callable[[dict[str, Any]], None]) -> NoReturn:
    render({"status": STATUS[code], **data})
    sys.exit(code)


def pipeline(file: str | None) -> Pipeline:
    """Load the file; any error in it stops the command (exit 2)."""
    pipe, errors = load(file)
    if errors:
        raise StepixError(INVALID, errors)
    return pipe


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="stepix")
def cli() -> None:
    """Run the ```stepix steps of a Markdown file (default: README.md here).

    Exit codes: 0 ok, 1 a step failed, 2 it can't start (fix it first), 130 interrupted.
    """


def command(name: str) -> Callable:
    """A subcommand that reports what stops it (an invalid file, uv missing, ...) with exit 2."""
    def deco(fn: Callable) -> click.Command:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except StepixError as e:
                finish(e.code, {"errors": [p.to_dict() for p in e.problems]}, ui.render_problems)
        return cli.command(name)(wrapper)
    return deco


@command("run")
@click.argument("file", required=False, metavar="[FILE.md]")
@click.option("--step", metavar="NAME", help="Only this step.")
@click.option("--from", "start", metavar="NAME", help="This step and every step after it.")
@click.option("--no-cache", is_flag=True, help="Rerun steps even if they succeeded before.")
def run_cmd(file: str | None, step: str | None, start: str | None, no_cache: bool) -> None:
    """Run the steps of FILE.md (default: README.md here)."""
    if step and start:
        raise click.UsageError("use --step or --from, not both")
    pipe = pipeline(file)
    view = ui.LiveView() if sys.stdout.isatty() else ui.PlainView()
    opts = RunOptions(file, step, start, no_cache)
    result = engine.run(pipe, opts, Store(pipe.root), view)
    finish(result.code, result.to_dict(), ui.render_run)


@command("validate")
@click.argument("file", required=False, metavar="[FILE.md]")
def validate_cmd(file: str | None) -> None:
    """Check the file without running anything; errors point at file:line."""
    pipe, errors = load(file)
    finish(INVALID if errors else OK, {"pipeline": pipe.name, "steps": list(pipe.steps),
                                       "errors": [p.to_dict() for p in errors]}, ui.render_validate)


def _interrupt(*_: Any) -> None:
    raise KeyboardInterrupt


def main() -> None:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):  # all stop a run the same way
        signal.signal(sig, _interrupt)
    try:
        code = cli.main(prog_name="stepix", standalone_mode=False)
    except click.ClickException as e:
        e.show()
        sys.exit(INVALID)
    except (click.Abort, KeyboardInterrupt):
        print("interrupted", file=sys.stderr)
        sys.exit(INTERRUPTED)
    sys.exit(code or 0)
