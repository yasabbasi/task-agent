import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import ROOT, FakeApp, run_git

from task_agent import config


@pytest.fixture
def doctor() -> ModuleType:
    spec = importlib.util.spec_from_file_location("doctor", ROOT / "scripts" / "doctor.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_machine_checks_name_a_fix_for_each_tool(
    doctor: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {
        "gh": (True, "Token scopes: 'gist', 'repo'"),
        "claude": (True, ""),
        "op": (False, ""),
    }
    monkeypatch.setattr(doctor, "run", lambda *cmd, cwd=None: answers[cmd[0]])
    checks = doctor.machine()
    assert [ok for ok, _, _ in checks] == [False, True, False]
    assert checks[0][2] == "gh auth refresh -s repo,workflow"
    assert "1Password" in checks[2][2]


def test_clone_checks_want_an_https_origin_and_a_tracked_main(
    doctor: ModuleType, cfg: config.Config, tmp_path: Path
) -> None:
    missing = doctor.clone(cfg.target("owner/repo", str(tmp_path / "nowhere")))
    assert missing[0][0] is False and missing[0][2].startswith("gh repo clone owner/repo")
    found = doctor.clone(cfg)
    assert [ok for ok, _, _ in found] == [False, True]
    assert found[0][2] == "git remote set-url origin https://github.com/owner/repo.git"
    run_git(cfg.clone, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    assert [ok for ok, _, _ in doctor.clone(cfg)] == [True, True]


def test_the_app_must_match_the_config_and_be_installed_on_every_repo(
    doctor: ModuleType, cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def call(method: str, path: str, token: str) -> dict[str, Any]:
        if path == "/app":
            return {"slug": "task-agent-test"}
        raise OSError("HTTP Error 404: Not Found")

    monkeypatch.setattr(doctor, "call", call)
    monkeypatch.setattr(FakeApp, "jwt", lambda self: "jwt")
    checks = doctor.installed(cfg, FakeApp())
    assert [ok for ok, _, _ in checks] == [True, False]
    assert checks[1][2] == "install it at https://github.com/apps/task-agent-test/installations/new"
