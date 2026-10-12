"""The file format: which blocks are steps, what is ignored, and errors with file:line."""

import re
import subprocess
import sys

from conftest import REPO, pipeline, step

DOC = '''
# A research repo

Prose, a table and an image are documentation.

| a | b |
|---|---|

![diagram](diagram.png)

## Settings

```stepix
[params]
subset = "full"
```

```python
print("not read")
```

## Setup

### 1. First

```stepix
[steps.first]
run = "echo first >> ran.txt"
```

```bash
echo "not read either"
```

### 2. Second

```stepix
[steps.second]
run = "echo second-{params.subset} >> ran.txt"
requires = ["first"]
```

## Citation

Not a step.
'''


def test_only_stepix_blocks_are_read(project):
    project.write("README.md", DOC)
    assert "README.md: 2 steps, valid" in project.stepix("validate").out
    assert project.stepix("run").code == 0
    assert project.ran() == ["first", "second-full"]


def test_errors_point_at_file_and_line(project):
    project.write("README.md", """
        # P

        ```stepix
        [params]
        n = 1
        ```

        ```stepix
        [steps.a]
        run = "echo {params.missing}"
        needs = ["b"]
        ```

        ```stepix
        [steps.b]
        run = "echo b"
        retries = "lots"
        ```

        ```stepix
        [steps.c]
        run = "echo c
        ```

        ```stepix
        [steps.Not-A-Name]
        run = "echo d"
        ```

        ```stepix
        [steps.a]
        run = "echo again"
        ```

        ```stepix
        [steps.e]
        requires = []
        ```

        ```stepix f
        run = "echo f"
        ```

        ```stepix
        [step.g]
        run = "echo g"
        ```

        ```stepix
        [params]
        m = 2
        ```
        """)
    res = project.stepix("validate")
    assert res.code == 2
    found = {(int(line), heading or None)
             for line, heading in re.findall(r"README\.md:(\d+): (?:\[([\w.]+)\] )?", res.out)}
    assert found == {
        (11, "steps.a"),  # unknown field `needs` (it is `requires`)
        (10, "steps.a"),  # {params.missing}
        (17, "steps.b"),  # retries must be an integer
        (22, None),  # unterminated TOML string
        (26, None),  # not a step name
        (31, "steps.a"),  # duplicate step
        (36, "steps.e"),  # no `run`
        (40, None),  # the name goes inside the block now
        (45, None),  # [step.g] is not [steps.g]
        (50, "params"),  # a second [params]
    }


def test_references_cycles_and_envs(project):
    project.write("envs/unlocked/pyproject.toml", '[project]\nname = "x"\nversion = "0"\n')
    project.write("README.md", pipeline(
        "P", step("a", requires=("b",)), step("b", requires=("a",)),
        step("c", requires=("nope",), run="echo {steps.d.out}", extra='env = "envs/missing"'),
        step("d", extra='env = "envs/unlocked"')))
    out = project.stepix("validate").out
    for text in ("dependency cycle: ", "requires 'nope', which is not a step",
                 "{steps.d.out}: 'd' is not before 'c'", "'envs/missing' is not a uv project",
                 "fix: uv init --bare envs/missing"):
        assert text in out, text
    assert "envs/unlocked" not in out  # an env needs no uv.lock: uv makes one on first use


def test_any_markdown_file_and_clear_errors_without_one(project):
    missing = project.stepix("run")
    assert missing.code == 2 and "README.md not found" in missing.out
    project.write("README.md", "# Just a README\n\n```bash\necho hi\n```\n")
    assert "no steps here" in project.stepix("run").out
    project.write("docs/setup.md", pipeline("Setup", step("a", run="pwd > where.txt")))
    assert project.stepix("run", "docs/setup.md").code == 0
    assert (project.root / "docs" / "where.txt").read_text().strip().endswith("docs")
    assert (project.root / "docs" / ".stepix").is_dir()  # state lives next to the file
    project.write("SETUP.markdown", pipeline("Setup", step("b")))
    assert project.stepix("run", "SETUP.markdown").code == 0 and project.ran() == ["b"]


def test_the_published_version_is_the_one_stepix_reports():
    import stepix
    from stepix.spec import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == stepix.__version__


def test_the_readme_is_a_valid_pipeline():
    """The main README is the example: its stepix blocks must stay valid (CI also runs it)."""
    proc = subprocess.run([sys.executable, "-m", "stepix", "validate"], cwd=REPO,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "README.md: 2 steps, valid" in proc.stdout, proc.stdout
