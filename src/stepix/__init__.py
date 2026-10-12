"""stepix makes the setup instructions in your README exact and runnable.

Steps return values to later steps with `output()`. This file needs only the standard library:
stepix copies it into every step's PYTHONPATH, so steps can import it without installing stepix.
"""

__version__ = "0.1.0"


def output(**values: object) -> None:
    """Hand values to the steps after this one: `output(dir="data/prep")` makes
    `{steps.<this step>.dir}` available to them. It prints `::stepix::output dir=data/prep`."""
    for name, value in values.items():
        print(f"::stepix::output {name}={value}", flush=True)
