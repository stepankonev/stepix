"""Running pipelines: what reruns and why, --step, --from and --no-cache, outputs, failures."""

import io
import json
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import pipeline, step

import stepix

CHAIN = pipeline("Chain", step("a"), step("d"), step("b", requires=("a",)),
                 step("c", requires=("b",)))


def test_caching_and_reruns(project):
    project.write("README.md", CHAIN)
    first = project.stepix("run")
    assert first.code == 0 and project.ran() == ["a", "d", "b", "c"]
    assert re.search(r"\[stepix\] logs: \.stepix/runs/[\w-]+/logs/<step>\.log\n", first.out)
    again = project.stepix("run")
    assert again.code == 0 and project.ran() == [] and set(again.steps.values()) == {"cached"}
    assert all(s["last_run_at"] for s in again.receipt["steps"])
    assert again.out.count("cached (last ran ") == 4  # the run says when each one last ran


def test_step_from_and_no_cache(project):
    project.write("README.md", CHAIN)
    early = project.stepix("run", "--step", "b")
    assert early.code == 2 and "'b' requires 'a', which hasn't succeeded yet" in early.out
    assert project.stepix("run").code == 0 and project.ran() == ["a", "d", "b", "c"]
    only_b = project.stepix("run", "--step", "b")  # chooses b; b is cached, so nothing runs
    assert only_b.code == 0 and project.ran() == [] and only_b.steps == {"b": "cached"}
    assert project.stepix("run", "--step", "b", "--no-cache").code == 0 and project.ran() == ["b"]
    assert project.stepix("run", "--from", "d").code == 0 and project.ran() == []
    assert project.stepix("run", "--from", "d", "--no-cache").code == 0
    assert project.ran() == ["d", "b", "c"]
    fresh = project.stepix("run", "--no-cache")
    assert fresh.code == 0 and project.ran() == ["a", "d", "b", "c"]
    assert {s["reason"] for s in fresh.receipt["steps"]} == {"--no-cache"}
    assert "'nope' is not a step" in project.stepix("run", "--step", "nope").out
    assert project.stepix("run", "--step", "a", "--from", "b").code == 2


def test_changed_params_and_uncached_steps(project):
    readme = project.write("README.md", pipeline(
        "P", step("fetch", run="echo fetch-{params.url} >> ran.txt"),
        step("use", requires=("fetch",)), step("other", run="echo other-{params.mode} >> ran.txt"),
        step("dice", requires=("use",), cache=False),  # no `cache`: runs every time
        params='url = "a"\nmode = "m"'))
    assert project.stepix("run").code == 0
    assert project.ran() == ["fetch-a", "use", "other-m", "dice"]
    assert project.stepix("run").code == 0 and project.ran() == ["dice"]
    readme.write_text(readme.read_text().replace('url = "a"', 'url = "b"'))  # edited
    res = project.stepix("run")
    assert project.ran() == ["fetch-b", "use", "dice"]  # fetch and what comes after it
    assert res.receipt["steps"][0]["reason"] == "changed: params.url"


def test_outputs_pass_values_between_steps(project):
    project.write("README.md", pipeline(
        "P", step("make", run="echo ::stepix::output angle={params.angle}"),
        step("use", requires=("make",), run="echo use-{steps.make.angle} >> ran.txt",
             cache=False),
        step("ask", requires=("make",), run="echo {steps.make.nope}"),
        params="angle = 30"))
    res = project.stepix("run")
    assert res.code == 1 and project.ran() == ["use-30"]
    assert res.steps["ask"] == "failed" and "ask failed: missing output" in res.out
    assert "step 'make' did not output 'nope'" in res.out
    assert project.stepix("run").code == 1
    assert project.ran() == ["use-30"]  # make was cached: its output is kept with its state


def test_failure_summary_and_resume(project):
    flaky = "echo b >> ran.txt; if [ -e fail_b ]; then echo 'ValueError: shard 3' >&2; exit 2; fi"
    project.write("README.md", pipeline(
        "Chain", step("a"), step("d"), step("b", requires=("a",), run=flaky),
        step("c", requires=("b",))))
    project.write("fail_b", "x")
    res = project.stepix("run")
    assert res.code == 1 and project.ran() == ["a", "d", "b"]
    assert res.steps == {"a": "done", "d": "done", "b": "failed", "c": "skipped"}
    assert res.receipt["steps"][-1]["reason"] == "b failed"
    for line in ("✗ b failed (exit 2)", "error   ValueError: shard 3", "resume  stepix run\n"):
        assert line in res.out, line
    from_d = project.stepix("run", "--from", "d")  # d is cached; b failed last time, so it runs
    assert "resume  stepix run --from d\n" in from_d.out and project.ran() == ["b"]
    fresh = project.stepix("run", "--no-cache")  # resume without --no-cache: a and d are done
    assert "resume  stepix run\n" in fresh.out and project.ran() == ["a", "d", "b"]
    (project.root / "fail_b").unlink()
    assert project.stepix("run").code == 0 and project.ran() == ["b", "c"]


@pytest.mark.parametrize("mode", ["continue", "warn", "stop"])
def test_on_fail(project, mode):
    project.write("README.md", pipeline(
        "P", step("a"), step("b", requires=("a",), extra=f'on_fail = "{mode}"'),
        step("c", requires=("b",)), step("d", requires=("a",))))
    project.write("fail_b", "x")
    res = project.stepix("run")
    if mode == "continue":
        assert res.code == 1 and project.ran() == ["a", "b", "d"]
        assert res.steps == {"a": "done", "b": "failed", "c": "skipped", "d": "done"}
    elif mode == "warn":
        assert res.code == 0 and project.ran() == ["a", "b", "c", "d"]
        assert res.steps["b"] == "warned"
    else:
        assert res.code == 1 and project.ran() == ["a", "b"]
        assert res.steps == {"a": "done", "b": "failed", "c": "skipped", "d": "pending"}


def test_retries(project):
    flaky = "echo try >> ran.txt; test $(wc -l < ran.txt) -ge 3"
    project.write("README.md", pipeline("P", step("a", run=flaky, extra="retries = 2")))
    assert project.stepix("run").code == 0 and project.ran() == ["try", "try", "try"]


def test_the_receipt_is_written(project):
    project.write("README.md", pipeline("P", step("a"), step("b", requires=("a",))))
    res = project.stepix("run")
    assert res.code == 0 and res.steps == {"a": "done", "b": "done"}
    assert res.receipt["status"] == "ok" and res.receipt["versions"]["stepix"] == stepix.__version__
    logs = sorted(p.name for p in project.root.glob(".stepix/runs/*/logs/*.log"))
    assert logs == ["a.log", "b.log"]


def test_one_run_at_a_time_and_its_files_show_progress(project):
    wait = ("echo waiting; touch started; i=0; "
            "while [ ! -e release ] && [ $i -lt 600 ]; do sleep 0.1; i=$((i+1)); done")
    project.write("README.md", pipeline("P", step("slow", run=wait)))
    proc = subprocess.Popen([sys.executable, "-m", "stepix", "run"], cwd=project.root,
                            env=project.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(150):
            if (project.root / "started").exists():
                break
            time.sleep(0.2)
        second = project.stepix("run")
        assert second.code == 2 and "README.md is already running" in second.out
        (manifest,) = project.receipts()  # what an agent reads while the run goes on
        assert json.loads(manifest.read_text())["steps"][0]["state"] == "running"
        assert (manifest.parent / "logs" / "slow.log").read_text().endswith("waiting\n")
    finally:
        (project.root / "release").touch()
        proc.communicate(timeout=60)
    assert proc.returncode == 0


def test_a_signal_stops_the_step_and_finalizes_the_run(project):
    project.write("README.md", pipeline(
        "P", step("slow", run="touch started; sleep 4; touch survived")))
    proc = subprocess.Popen([sys.executable, "-m", "stepix", "run"], cwd=project.root,
                            env=project.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if (project.root / "started").exists():
            break
        time.sleep(0.1)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    time.sleep(5)
    assert not (project.root / "survived").exists()
    (manifest,) = project.root.glob(".stepix/runs/*/manifest.json")
    receipt = json.loads(manifest.read_text())
    assert receipt["status"] == "interrupted" and receipt["steps"][0]["state"] == "failed"


def test_background_leftovers_neither_hang_nor_survive(project):
    project.write("README.md", pipeline("P", step("bg", run="(sleep 5; touch leftover) &")))
    t0 = time.monotonic()
    assert project.stepix("run").code == 0 and time.monotonic() - t0 < 5
    time.sleep(5.5)
    assert not (project.root / "leftover").exists()


def test_steps_without_env_use_the_repos_own_project(project):
    project.write("pyproject.toml", '[project]\nname = "repo"\nversion = "0"\n'
                  'requires-python = ">=3.10"\n')
    project.write("docs/setup.md", pipeline("P", step("a", run='echo "$VIRTUAL_ENV" > venv.txt')))
    assert project.stepix("run", "docs/setup.md").code == 0
    venv = Path((project.root / "docs" / "venv.txt").read_text().strip())
    assert venv.resolve() == (project.root / ".venv").resolve()


def test_output_helper_works_in_any_step(project):
    helper = "python3 -c 'from stepix import output; output(angle=30, name=\"x y\")'"
    project.write("README.md", pipeline(
        "P", step("make", run=helper),
        step("use", requires=("make",),
             run="echo {steps.make.angle}-{steps.make.name} >> ran.txt")))
    assert project.stepix("run").code == 0 and project.ran() == ["30-x", "y"]


def test_the_terminal_view_draws_boxes_and_the_values_between_them(project):
    from rich.console import Console

    from stepix.flow import Flow
    from stepix.spec import load
    from stepix.store import now

    readme = project.write("README.md", pipeline(
        "P", step("a", run="echo ::stepix::output x=1"),
        step("b", requires=("a",), run="echo {steps.a.x}"), step("c", requires=("a",))))
    pipe, _ = load(str(readme))
    items = {"a": {"state": "done", "duration_s": 42},
             "b": {"state": "running", "started_at": now()}, "c": {"state": "pending"}}
    console = Console(record=True, width=90, height=100, file=io.StringIO())
    console.print(Flow(pipe, ["a", "b", "c"], items))
    lines = console.export_text().splitlines()
    text = "\n".join(lines)
    for part in ("✓ a", "done 0:42", "$ echo ::stepix::output x=1", "● x", "to b",  # a hands x on
                 "running 0:0", "▶ x", "from a", "$ echo {steps.a.x}",  # b uses it
                 "· c", "waiting", "repo env", "✓ 1 done", "· 1 waiting"):
        assert part in text, part
    top_of_c = next(line for line in lines if "· c" in line)
    assert "▶╭" in top_of_c  # c only requires a: the line goes box to box
