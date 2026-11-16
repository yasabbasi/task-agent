"""Run inside an agent session by `make verify`. Every action here should be refused."""

import json
import pathlib
import subprocess
import urllib.request
from collections.abc import Callable

HOME = pathlib.Path.home()
CRED = "protocol=https\nhost=github.com\n\n"


def run(*cmd: str, stdin: str | None = None) -> None:
    out = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=20)
    if out.returncode or not out.stdout.strip():
        raise PermissionError(f"exit {out.returncode}")


# verify plants the canary file and the keychain item, so a failure here is a refusal.
CHECKS: dict[str, Callable[[], object]] = {
    "list the home folder": lambda: list(HOME.iterdir()),
    "read a file in the home folder": lambda: (HOME / "task-agent-canary.txt").read_text(),
    "read a keychain item": lambda: run(
        "security", "find-generic-password", "-s", "task-agent-probe", "-w"
    ),
    "git credential fill": lambda: run("git", "credential", "fill", stdin=CRED),
    "gh auth token": lambda: run("gh", "auth", "token"),
    "network": lambda: urllib.request.urlopen("https://api.github.com", timeout=10),
    "write outside the worktree": lambda: (HOME / "task-agent-probe.txt").write_text("x"),
    "write the worktree .git file": lambda: open(".git", "a").close(),
}


def outcome(check: Callable[[], object]) -> str:
    try:
        check()
    except FileNotFoundError as exc:
        return f"not tested ({exc})"
    except Exception as exc:
        return f"refused ({type(exc).__name__})"
    return "allowed"


def test_probe(tmp_path: pathlib.Path) -> None:
    """tmp_path is here because the suite's temp-dir tests must work inside a session."""
    found = {name: outcome(check) for name, check in CHECKS.items()}
    pathlib.Path("probe.json").write_text(json.dumps(found))
