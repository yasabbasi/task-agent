import io
import subprocess
from pathlib import Path

import pytest

from task_agent import cli, dispatch
from task_agent.app_token import App
from task_agent.config import Config


def test_version_flag_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--version"])
    assert exit_.value.code == 0
    assert capsys.readouterr().out.strip()[0].isdigit()


def test_unknown_command_fails() -> None:
    with pytest.raises(SystemExit) as exit_:
        cli.main(["nope"])
    assert exit_.value.code == 2


def test_console_script_runs() -> None:
    result = subprocess.run(["task-agent", "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "usage: task-agent" in result.stdout


BOT = "yasabbasi-task-agent[bot]"


def pr_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: list[str], **env: str) -> int:
    listing = tmp_path / "files.txt"
    listing.write_text("\n".join(files) + "\n")
    values = {"PR_AUTHOR": BOT, "PR_TITLE": "Add a file", "PR_BODY": "Closes #7\n"}
    values |= {"PR_BRANCH": "agent/issue-7", "PR_CHANGED_FILES": str(len(files))} | env
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return cli.main(["pr-check", "--files", str(listing)])


def test_pr_check_passes_a_clean_app_pull_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pr_check(monkeypatch, tmp_path, ["src/a.py", "tests/test_a.py"]) == 0
    assert capsys.readouterr().out == "pr-check: 0 problem(s)\n"


def test_pr_check_fails_an_app_pull_request_that_breaks_a_rule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pr_check(monkeypatch, tmp_path, ["src/a.py", "Makefile"], PR_BODY="Fixes #8") == 1
    out = capsys.readouterr().out
    assert "::error::agent pull requests may not change: Makefile" in out
    assert "::error::the description needs exactly one closing line, `Closes #7`" in out
    assert out.endswith("pr-check: 3 problem(s)\n")


def test_pr_check_sees_the_old_name_of_a_renamed_protected_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert (
        pr_check(monkeypatch, tmp_path, ["docs/rules.md", "CLAUDE.md"], PR_CHANGED_FILES="1") == 1
    )


@pytest.mark.parametrize("count", ["3", "3000"])
def test_pr_check_fails_when_it_cannot_see_every_changed_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], count: str
) -> None:
    assert pr_check(monkeypatch, tmp_path, ["a.py", "b.py"], PR_CHANGED_FILES=count) == 1
    assert f"could not list all {count} changed files" in capsys.readouterr().out


def test_pr_check_leaves_other_authors_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pr_check(monkeypatch, tmp_path, ["Makefile"], PR_AUTHOR="yasabbasi", PR_BRANCH="x") == 0
    assert "not an App pull request" in capsys.readouterr().out


def test_pr_check_covers_an_agent_branch_whoever_opened_the_pull_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert pr_check(monkeypatch, tmp_path, ["a.py"], PR_AUTHOR="stranger", PR_BODY="Closes #9") == 1


def test_verify_request_leaves_a_file_for_the_watcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    (tmp_path / ".worktrees" / "requests").mkdir(parents=True)
    assert cli.main(["verify", "--request", "--repo", "yasabbasi/data-pipeline", "--pr", "5"]) == 0
    assert cli.main(["verify", "--request"]) == 0
    requests = tmp_path / ".worktrees" / "requests"
    assert (requests / "verify-data-pipeline").read_text() == "5"
    assert (requests / "verify-task-agent").read_text() == ""
    with pytest.raises(SystemExit, match="o/typo is not under"):
        cli.main(["verify", "--request", "--repo", "o/typo"])


def test_dispatch_all_covers_every_configured_repo_with_main_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[str, Path, Path]] = []
    text = (cli.ROOT / "pyproject.toml").read_text().replace("budget_usd = 3.0", "budget_usd = 2.0")
    monkeypatch.setattr(dispatch, "show_main", lambda root, path: text)
    monkeypatch.setattr("sys.stdin", io.StringIO("the key"))

    def run_pass(app: App, base: Config, targets: list[Config], **kwargs: object) -> None:
        assert app._key == "the key" and base.budget_usd == 2.0 and "each" in kwargs
        seen.extend((cfg.repo, cfg.clone, cfg.root) for cfg in targets)

    monkeypatch.setattr(dispatch, "run_pass", run_pass)
    assert cli.main(["dispatch-all", "--key-stdin"]) == 0
    code = cli.ROOT.parent
    assert seen == [
        ("yasabbasi/task-agent", cli.ROOT, cli.ROOT),
        ("yasabbasi/data-pipeline", code / "data-pipeline", cli.ROOT),
        ("yasabbasi/mcp-server", code / "mcp-server", cli.ROOT),
    ]
