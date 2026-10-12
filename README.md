# stepix

**stepix makes the setup instructions in your README exact and runnable.**

## The problem

Research repos often ship their setup as a README ritual: install this, download that,
preprocess, train. Those instructions are written for a person, not a computer. Steps are
implied, paths and versions are left to the reader, and nothing says when a step is done or what
it needs first. Every reader, human or agent, interprets them a little differently, so the same
README gives different setups.

## What stepix does

You keep the README. Under each instruction you add a small `stepix` block that defines the step
precisely: its command, environment, inputs, outputs and what it depends on. `uvx stepix run`
then executes the README as written, the same way every time. All you need is
[uv](https://docs.astral.sh/uv/) on Linux or macOS; there is nothing to install.

- **Exact steps.** Every step is fully defined; nothing is left to interpretation. Ensuring reproducibility and saving your time.
- **Separate environments where needed.** By default a step runs in the repo's environment. A
  step that needs different packages gets its own: for example, one step can use numpy<2 while
  the rest of the repo uses numpy>=2.
- **Caching where you ask for it.** A step with `cache = true` is skipped once it has
  succeeded, so a long download or training doesn't run twice. Every other step runs each time,
  and `--no-cache` runs them all.

## Try it: this README is a pipeline

```bash
git clone https://github.com/stepankonev/stepix && cd stepix
uvx stepix run ./README.md  # downloads an image, then rotates it by a random angle
uvx stepix run ./README.md  # the download is cached; the image gets a new angle
```

It runs the two steps below; their code is in
[example/](https://github.com/stepankonev/stepix/tree/main/example).

```stepix
[params]
url = "https://raw.githubusercontent.com/scikit-image/scikit-image/v0.24.0/skimage/data/chelsea.png"
output = "out/rotated.png"
```

### 1. Download the image

`example/download.py` saves the image and ends with `output(image_path=target)`, which is how the
next step gets `{steps.download.image_path}`. With `cache = true` it downloads once, and later
runs skip it.

```stepix
[steps.download]
run = "python example/download.py {params.url} --out data"
cache = true
retries = 2
```

### 2. Rotate it

This step needs OpenCV, so it runs in its own environment, `example/cv`, pinned by its
`uv.lock`. It isn't cached, so it runs every time, with a new angle.

```stepix
[steps.rotate]
env = "example/cv"
run = "python example/rotate.py --in {steps.download.image_path} --out {params.output}"
requires = ["download"]
```

## Steps and params

A ```` ```stepix ```` block is TOML, and its first line says what it holds: `[steps.NAME]` starts
step NAME, and `[params]` holds values that commands use as `{params.NAME}` (one `[params]` per
file).

| Field | Written as | Required | Default | Meaning |
|---|---|---|---|---|
| Name | `[steps.NAME]` | yes | — | the step's name: lowercase letters, digits, `_` |
| Command | `run = "..."` | yes | — | a shell command, run from the README's folder |
| Environment | `env = "envs/x"` | no | the repo's own | a uv project folder (`pyproject.toml`; a `uv.lock` pins its versions) |
| Requires | `requires = ["a"]` | no | `[]` | steps that must finish first |
| Cache | `cache = true` | no | `false` | `true`: skip it once it has succeeded |
| On failure | `on_fail = "continue"` | no | `"stop"` | `continue`: skip what depends on it; `warn`: carry on |
| Retries | `retries = 2` | no | `0` | how many more times to try a failed command |

In `run`, `{steps.STEP.NAME}` is a value from a step it requires. A step outputs one with
`output(NAME=value)` in Python (`from stepix import output`), or from any language by printing
`::stepix::output NAME=value`. A step with `cache = true` is skipped when its last run
succeeded, unless a param it uses changed, a step it requires ran, or you pass `--no-cache`.
Every other step runs each time.

## Commands

```bash
uvx stepix run                   # run README.md here (or: uvx stepix run path/to/file.md)
uvx stepix run --no-cache        # run cached steps too
uvx stepix run --step rotate     # only this step
uvx stepix run --from download   # this step and every step after it
uvx stepix validate              # check the README without running anything
```

`--step` and `--from` only choose steps; a step that is cached still skips unless you add
`--no-cache` (`uvx stepix run --step rotate --no-cache`). Commands run from the file's folder,
and state goes to `.stepix/` next to it. Exit code `0` ok, `1` a step failed, `2` it can't start
(the output says why), `130` interrupted.

## With agents

stepix never prompts, so an agent runs it the way a person does. When a run starts, it prints
where the logs go, `.stepix/runs/<run>/logs/<step>.log`; each log fills while its step runs, and
`.stepix/runs/<run>/manifest.json` holds every step's state. To follow a long run, an agent
reads those files.
