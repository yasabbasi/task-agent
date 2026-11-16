import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from task_agent import config, dispatch
from task_agent.app_token import Api, App

ROOT = Path(__file__).resolve().parent.parent
BOT = "task-agent-test[bot]"
CFG = config.Config(
    repo="owner/repo", app_slug="task-agent-test", client_id="x", key_title="k", key_vault="v"
)
SHARED = ("prompts/agent.md", "prompts/rules.md", "prompts/probe.py", ".claude/agent-settings.json")
FAKE_CLAUDE = """#!{python}
import json, pathlib, sys
here = pathlib.Path(__file__).parent
plan = json.loads((here / "plan.json").read_text())
calls = here / "calls.json"
seen = json.loads(calls.read_text()) if calls.exists() else []
calls.write_text(json.dumps(seen + [sys.argv[1:]]))
step = plan[min(len(seen), len(plan) - 1)]
for name, text in step.get("files", {{}}).items():
    pathlib.Path(name).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(name).write_text(text)
print("\\n".join(json.dumps(line) for line in step["output"]))
"""


class FakeApi(Api):
    """Answers GETs from a dict of path prefix to response, and records every call."""

    def __init__(self, routes: dict[str, Any] | None = None, issues: dict[int, Any] | None = None):
        self.routes, self.issues = routes or {}, issues or {}
        self.calls: list[tuple[str, str, Any]] = []
        self.token, self.repo = "t", "owner/repo"

    def __call__(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if method == "GET":
            for prefix, response in self.routes.items():
                if path.startswith(prefix):
                    return response(path) if callable(response) else response
            return []
        if method == "POST" and path.endswith("/labels"):
            return [*self.routes.get(path, []), *({"name": name} for name in body["labels"])]
        return {"id": 99, "html_url": "https://example/pull/1"}

    def graphql(self, query: str, **variables: Any) -> Any:
        return {"repository": {"issue": self.issues[variables["number"]]}}

    def posted(self, path: str) -> list[Any]:
        return [body for method, called, body in self.calls if (method, called) == ("POST", path)]


class FakeApp(App):
    def __init__(self, api: FakeApi | None = None) -> None:
        self.api, self.asked = api or FakeApi(), list[tuple[str, dict[str, str]]]()

    def token(self, repo: str, **permissions: str) -> FakeApi:
        self.asked.append((repo, permissions))
        return self.api


def run_git(cwd: Path, *args: str) -> str:
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    env = os.environ | dispatch.GIT_ENV
    done = subprocess.run(["git", *ident, *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def approval(title: str = "t", body: str = "", who: str = "owner", **extra: Any) -> dict[str, Any]:
    """An issue as GraphQL returns it, marked ready by `who` at 12:00."""
    label: dict[str, Any] = {"__typename": "LabeledEvent", "createdAt": "2026-01-01T12:00:00Z"}
    label |= {"actor": {"login": who}, "label": {"name": "status:ready"}}
    issue: dict[str, Any] = {"title": title, "body": body, "lastEditedAt": None}
    return issue | {"timelineItems": {"nodes": [label]}} | extra


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calls outside a repo, such as looking up the bot's user id, get a canned answer."""
    monkeypatch.setattr(dispatch, "call", lambda method, path, token, body=None: {"id": 42})


@pytest.fixture
def github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bare repo standing in for https://github.com/owner/repo.git, with main's shared files."""
    seed, bare = tmp_path / "seed", tmp_path / "github.git"
    seed.mkdir()
    run_git(seed, "init", "--quiet", "-b", "main")
    for name in SHARED:
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text((ROOT / name).read_text())
    (seed / "pyproject.toml").write_text('[tool.task-agent]\nagent_commands = ["make sample"]\n')
    (seed / ".gitignore").write_text(".worktrees/\n.uv-cache/\n")
    (seed / "CLAUDE.md").write_text("# rules\n")
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "--quiet", "-m", "base")
    run_git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(bare))
    # Like GitHub's Linux runner: git must not guess an identity from the machine.
    settings = {
        "user.useConfigOnly": "true",
        f"url.{bare}.insteadOf": "https://github.com/owner/repo.git",
    }
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(settings)))
    for index, (key, value) in enumerate(settings.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    return bare


@pytest.fixture
def cfg(tmp_path: Path, github: Path) -> config.Config:
    """Settings for a clone of the fake GitHub repo, which is also the dispatcher's root."""
    run_git(tmp_path, "clone", "--quiet", str(github), "clone")
    clone = tmp_path / "clone"
    return replace(CFG, root=clone, clone=clone, repos={"owner/repo": "."})


@pytest.fixture
def claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """Put a fake `claude` first on PATH; each call plays the next step of the plan given."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def plan(*steps: dict[str, Any]) -> Path:
        (bin_dir / "plan.json").write_text(json.dumps(steps))
        (bin_dir / "calls.json").unlink(missing_ok=True)
        return bin_dir / "calls.json"

    return plan


def result(body: str = "## Why\n\nBecause.", **extra: Any) -> dict[str, Any]:
    """A successful session result with its two text fields."""
    output = {"pr_title": "Add a file", "pr_body": body}
    base = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3}
    return (
        base
        | {"total_cost_usd": 0.4234, "duration_ms": 61_400, "structured_output": output}
        | extra
    )
