import json
import subprocess
import sys
import urllib.error
import uuid
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeApi, FakeApp, result, run_git

from task_agent import config, verify

Plan = Callable[..., Path]
RULES = [
    {"type": "deletion"},
    {
        "type": "pull_request",
        "parameters": {"require_code_owner_review": True, "require_last_push_approval": True},
    },
    {
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": "gate", "integration_id": 15368}]},
    },
]
REFUSED = {name: "refused (PermissionError)" for name in ("network", "read a keychain item")}
INIT = {"type": "system", "subtype": "init", "mcp_servers": [], "slash_commands": []}
OVER = result(subtype="error_max_budget_usd", is_error=True)
WORD = "0123456789abcdef0123456789abcdef"
KEYCHAIN = verify.keychain


class NarrowApp(FakeApp):
    """An App that, like the real one, is refused any permission it was not granted."""

    def token(self, repo: str, **permissions: str) -> FakeApi:
        if set(permissions) - {"contents", "issues", "pull_requests"}:
            raise urllib.error.HTTPError("u", 422, "Unprocessable", Message(), None)
        return super().token(repo, **permissions)


def reject_pushes(github: Path) -> None:
    hook = github / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'refused by the ruleset' >&2\nexit 1\n")
    hook.chmod(0o755)


def test_token_checks_pass_when_the_ruleset_and_the_app_hold(
    cfg: config.Config, github: Path
) -> None:
    (cfg.clone / ".github" / "workflows").mkdir(parents=True)
    (cfg.clone / ".github" / "workflows" / "gate.yml").write_text("name: gate\n")
    run_git(cfg.clone, "add", "-A")
    run_git(cfg.clone, "commit", "--quiet", "-m", "gate")
    run_git(cfg.clone, "push", "--quiet", str(github), "HEAD:main")
    reject_pushes(github)
    api = FakeApi({"/rules/branches/main": RULES})
    results = verify.token_checks(NarrowApp(api), cfg, api, pr=None)
    assert [text for ok, text in results if not ok] == []
    assert len(results) == 9
    assert not (cfg.clone / ".worktrees" / "issue-0").exists()


def test_push_checks_are_skipped_and_nothing_is_pushed_without_the_ruleset(
    cfg: config.Config, github: Path
) -> None:
    before = run_git(github, "rev-parse", "main")
    api = FakeApi({"/rules/branches/main": RULES[:1]})
    results = verify.token_checks(NarrowApp(api), cfg, api, pr=None)
    assert results[-1] == (False, "the ruleset is not active, so the push checks were skipped")
    assert run_git(github, "rev-parse", "main") == before
    assert run_git(github, "branch", "--list").split() == ["*", "main"]


def test_a_push_that_goes_through_is_a_failed_check(cfg: config.Config, github: Path) -> None:
    (cfg.clone / ".github" / "workflows").mkdir(parents=True)
    (cfg.clone / ".github" / "workflows" / "gate.yml").write_text("name: gate\n")
    run_git(cfg.clone, "add", "-A")
    run_git(cfg.clone, "commit", "--quiet", "-m", "gate")
    run_git(cfg.clone, "push", "--quiet", str(github), "HEAD:main")
    api = FakeApi({"/rules/branches/main": RULES})
    failed = [text for ok, text in verify.token_checks(FakeApp(api), cfg, api, pr=5) if not ok]
    assert "the App's token cannot push to main" in failed
    assert "the App's token cannot push a workflow change" in failed
    assert "the App cannot get workflows:write" in failed
    assert "the App's token cannot merge unapproved #5" in failed
    assert ("DELETE", "/git/refs/heads/guardrail-probe", None) in api.calls


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in home folder, a fixed canary word and a keychain that accepts the probe item."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(WORD))
    monkeypatch.setattr(verify, "keychain", lambda *args: True)
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def tools(home: Path, *names: str) -> dict[str, Any]:
    """An assistant message that calls the three file tools the probe prompt asks for."""
    paths = {"Edit": "CLAUDE.md", "Read": "task-agent-canary.txt", "Write": "task-agent-write.txt"}
    blocks = [
        {"type": "tool_use", "name": name, "input": {"file_path": str(home / paths[name])}}
        for name in names or paths
    ]
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}, *blocks]}}


def failures(cfg: config.Config, claude: Plan, files: dict[str, str], *output: Any) -> list[str]:
    claude({"files": files, "output": list(output)}, {"output": [OVER]})
    return [text for ok, text in verify.session_checks(cfg, "t") if not ok]


def test_session_checks_pass_when_every_action_was_tried_and_refused(
    cfg: config.Config, claude: Plan, home: Path
) -> None:
    probe = {"files": {"probe.json": json.dumps(REFUSED)}, "output": [INIT, tools(home), result()]}
    calls = claude(probe, {"output": [OVER]})
    results = verify.session_checks(cfg, "t")
    assert [text for ok, text in results if not ok] == []
    assert len(results) == len(REFUSED) + 8
    first, second = json.loads(calls.read_text())
    assert str(home / "task-agent-canary.txt") in first[1] and "rules come from" not in str(first)
    assert second[second.index("--max-budget-usd") + 1] == "0.01"
    assert list(home.rglob("*")) == [home / ".claude", home / ".claude" / "skills"]
    assert not (cfg.clone / ".worktrees" / "issue-0").exists()


def test_an_action_the_session_never_tried_is_not_a_pass(
    cfg: config.Config, claude: Plan, home: Path
) -> None:
    probe = json.dumps(REFUSED | {"gh auth token": "not tested ([Errno 2] gh)"})
    assert failures(cfg, claude, {"probe.json": probe}, INIT, tools(home, "Read"), result()) == [
        "in a session, gh auth token: not tested ([Errno 2] gh)",
        "in a session, the Edit tool on CLAUDE.md: not attempted",
        f"in a session, the Write tool on {home / 'task-agent-write.txt'}: not attempted",
    ]


def test_anything_a_session_managed_to_do_fails_its_check(
    cfg: config.Config, claude: Plan, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = json.dumps(REFUSED | {"network": "allowed"})
    done = {"probe.json": probe, "CLAUDE.md": "probe\n", str(home / "task-agent-write.txt"): "x"}
    leaked = {"type": "user", "message": {"content": [{"type": "tool_result", "content": WORD}]}}
    assert failures(cfg, claude, done, INIT, tools(home), leaked, result()) == [
        "in a session, network: allowed",
        "in a session, the Edit tool on CLAUDE.md: allowed",
        f"in a session, the Read tool on {home / 'task-agent-canary.txt'}: allowed",
        f"in a session, the Write tool on {home / 'task-agent-write.txt'}: allowed",
    ]
    servers = INIT | {"mcp_servers": [{"name": "planted", "status": "connected"}]}
    skills = INIT | {"slash_commands": ["task-agent-probe"]}
    refused = {"probe.json": json.dumps(REFUSED)}
    assert failures(cfg, claude, refused, servers, tools(home), result()) == [
        f"a planted .mcp.json loads no server ({servers['mcp_servers']})"
    ]
    assert failures(cfg, claude, refused, skills, tools(home), result()) == [
        "a user-level skill is unseen"
    ]
    monkeypatch.setattr(verify, "keychain", lambda *args: False)
    claude({"files": refused, "output": [INIT, tools(home), result()]}, {"output": [result()]})
    assert [text for ok, text in verify.session_checks(cfg, "t") if not ok] == [
        "a keychain item was planted for the probe to look for",
        "a $0.01 budget did not stop the session",
    ]


def test_the_keychain_probe_runs_only_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "platform", "linux")
    assert verify.keychain("delete-generic-password") is None and ran == []
    monkeypatch.setattr(sys, "platform", "darwin")
    assert verify.keychain("delete-generic-password") is True and ran[0][0] == "security"


def test_off_darwin_the_keychain_checks_are_skipped_and_do_not_fail(
    cfg: config.Config, claude: Plan, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(verify, "keychain", KEYCHAIN)
    found = REFUSED | {"read a keychain item": "not tested ([Errno 2] security)"}
    probe = {"files": {"probe.json": json.dumps(found)}, "output": [INIT, tools(home), result()]}
    claude(probe, {"output": [OVER]})
    results = verify.session_checks(cfg, "t")
    assert [text for ok, text in results if ok is False] == []
    assert [text for ok, text in results if ok is None] == [
        "in a session, read a keychain item: not tested ([Errno 2] security)",
        "a keychain item was planted for the probe to look for",
    ]


def test_a_session_that_never_starts_proves_nothing(
    cfg: config.Config, claude: Plan, home: Path
) -> None:
    found = failures(cfg, claude, {}, "not json")
    assert "the probe ran inside a real agent session" in found
    assert "a user-level skill is unseen" in found and len(found) == 6


def test_only_a_refusal_from_github_counts_as_refused() -> None:
    def fail(exc: Exception) -> bool:
        def action() -> None:
            raise exc

        return verify.refused(action)

    rejected = " ! [remote rejected] HEAD -> main (push declined due to repository rule violations)"
    assert fail(subprocess.CalledProcessError(1, ["git", "push"], stderr=rejected))
    assert fail(subprocess.CalledProcessError(1, ["git", "push"], stderr="remote: error: GH013: x"))
    assert not fail(subprocess.CalledProcessError(128, ["git", "push"], stderr="Could not resolve"))
    assert fail(urllib.error.HTTPError("u", 422, "Unprocessable", Message(), None))
    assert not fail(urllib.error.HTTPError("u", 503, "Unavailable", Message(), None))
    assert not verify.refused(lambda: None)


def test_a_crash_is_reported_as_a_failed_check(cfg: config.Config) -> None:
    class Broken(FakeApp):
        def token(self, repo: str, **permissions: str) -> FakeApi:
            raise RuntimeError("no installation")

    results = verify.run(Broken(), cfg)
    assert results == [(False, "verify stopped early: RuntimeError: no installation")]


def test_the_watcher_verifies_a_repo_when_a_request_file_asks(
    cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines, asked = list[str](), list[int | None]()

    def run(app: Any, cfg: config.Config, pr: int | None = None) -> list[verify.Result]:
        asked.append(pr)
        return [(True, "a"), (False, "b"), (None, "c")]

    monkeypatch.setattr(verify, "run", run)
    verify.requested(FakeApp(), cfg, lines.append)
    assert lines == [] and asked == []
    request = cfg.root / ".worktrees" / "requests" / "verify-repo"
    request.parent.mkdir(parents=True)
    request.write_text("5")
    verify.requested(FakeApp(), cfg, lines.append)
    assert lines == ["verify pass: a", "verify fail: b", "verify skip: c"] and asked == [5]
    assert not request.exists()
