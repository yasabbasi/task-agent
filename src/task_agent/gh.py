"""The gh CLI, acting as whoever is signed in."""

import subprocess


def gh(*args: str, input: str | None = None) -> str:
    """Run gh and return its output; a failure raises CalledProcessError."""
    done = subprocess.run(["gh", *args], input=input, capture_output=True, text=True, check=True)
    return done.stdout
