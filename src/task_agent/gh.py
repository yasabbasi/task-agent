"""The gh CLI, acting as whoever is signed in."""

import json
import subprocess
from typing import Any


def gh(*args: str, input: str | None = None) -> str:
    """Run gh and return its output; a failure raises CalledProcessError."""
    done = subprocess.run(["gh", *args], input=input, capture_output=True, text=True, check=True)
    return done.stdout


def gh_json(*args: str) -> Any:
    """Run gh and parse its JSON output; empty output is None."""
    out = gh(*args)
    return json.loads(out) if out.strip() else None
