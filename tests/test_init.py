import json
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import CFG, ROOT, run_git

from task_agent import cli, config, dispatch, init

FAKE_GH = """#!{python}
import json, pathlib, sys
here, args = pathlib.Path(__file__).parent, sys.argv[1:]
log = here / "gh-calls.json"
seen = json.loads(log.read_text()) if log.exists() else []
log.write_text(json.dumps(seen + [[args, sys.stdin.read() if "-" in args else ""]]))
for start, answer in json.loads((here / "gh-answers.json").read_text()).items():
    if " ".join(args).startswith(start):
        print(answer)
        break
"""
LINK = "https://github.com/owner/repo/pull/3"
Gh = Callable[..., Callable[[], list[Any]]]


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Gh:
    """Put a fake `gh` first on PATH and name the caller, with no global git config."""
    bin_dir = tmp_path / "gh-bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH.format(python=sys.executable))
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Caller")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "caller@example.com")

    def answer(visibility: str = "public", plan: str = "free", rulesets: str = "[]") -> Any:
        answers = {"api repos/owner/repo/rulesets": rulesets, "pr create": LINK}
        answers |= {"api repos/owner/repo --jq": visibility, "api user": plan}
        (bin_dir / "gh-answers.json").write_text(json.dumps(answers))
        return lambda: json.loads((bin_dir / "gh-calls.json").read_text())

    return answer


@pytest.fixture
def base(tmp_path: Path) -> config.Config:
    """A checkout of task-agent whose origin/main holds this working tree's .github."""
    home = tmp_path / "home"
    shutil.copytree(ROOT / ".github", home / ".github")
    shutil.copy(ROOT / "LICENSE", home / "LICENSE")
    run_git(home, "init", "--quiet", "-b", "main")
    run_git(home, "add", "-A")
    run_git(home, "commit", "--quiet", "-m", "base")
    run_git(home, "update-ref", "refs/remotes/origin/main", "HEAD")
    return replace(CFG, repo="owner/task-agent", root=home)


def merge(clone: Path) -> None:
    run_git(clone, "push", "--quiet", "origin", f"{init.BRANCH}:main")


def test_labels_and_settings_come_before_anything_else(
    gh: Gh, base: config.Config, cfg: config.Config, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = gh(visibility="private")
    init.run(base, cfg)
    args = [call[0] for call in calls()]
    labels = json.loads((ROOT / ".github" / "labels.json").read_text())
    assert args[:2] == [["api", "repos/owner/repo", "--jq", ".visibility"]] + [
        ["api", "user", "--jq", ".plan.name"]
    ]
    assert args[2] == ["label", "create", "status:triage", "--repo", "owner/repo"] + [
        *["--color", "d4c5f9", "--description", "New request, not yet reviewed", "--force"]
    ]
    assert [a[2] for a in args[2 : 2 + len(labels)]] == [label["name"] for label in labels]
    assert args[2 + len(labels) :][:2] == [
        ["repo", "edit", "owner/repo", "--enable-squash-merge", "--enable-merge-commit=false"]
        + ["--enable-rebase-merge=false", "--delete-branch-on-merge"],
        ["api", "-X", "PUT", "repos/owner/repo/actions/permissions/workflow"]
        + ["-f", "default_workflow_permissions=read"]
        + ["-F", "can_approve_pull_request_reviews=false"],
    ]
    assert "rulesets are not enforced" in capsys.readouterr().out.splitlines()[0]


@pytest.mark.parametrize(("visibility", "plan"), [("public", "free"), ("private", "pro")])
def test_no_warning_when_rulesets_are_enforced(
    gh: Gh,
    base: config.Config,
    cfg: config.Config,
    capsys: pytest.CaptureFixture[str],
    visibility: str,
    plan: str,
) -> None:
    gh(visibility=visibility, plan=plan)
    init.run(base, cfg)
    assert "warning" not in capsys.readouterr().out


def test_differing_files_become_the_callers_pull_request_and_the_ruleset_waits(
    gh: Gh,
    base: config.Config,
    cfg: config.Config,
    github: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = gh()
    init.run(base, cfg)
    assert LINK in capsys.readouterr().out
    create = next(call[0] for call in calls() if call[0][:2] == ["pr", "create"])
    assert create[2:8] == [
        "--repo",
        "owner/repo",
        "--head",
        init.BRANCH,
        "--label",
        "human-authored",
    ]
    names = run_git(github, "diff", "--name-only", "main", init.BRANCH).split()
    assert sorted(names) == sorted(
        [*init.COPIED, "LICENSE", *(f".github/workflows/{name}" for name in init.WORKFLOWS)]
    )
    assert all(f"- `{name}`\n" in create[-1] for name in names)
    assert run_git(github, "log", "-1", "--format=%an <%ae>", init.BRANCH).strip() == (
        "Caller <caller@example.com>"
    )
    assert not any("rulesets" in " ".join(call[0]) for call in calls())
    assert run_git(cfg.clone, "worktree", "list").count("\n") == 1


def test_workflows_are_made_from_the_templates_at_main_s_commit(
    gh: Gh, base: config.Config, cfg: config.Config
) -> None:
    sha = run_git(base.root, "rev-parse", "origin/main").strip()
    gate = init.managed(base, cfg)[".github/workflows/gate.yml"]
    assert f"repository: owner/task-agent\n          ref: {sha} # {sha[:7]}\n" in gate
    assert '--bot "task-agent-test[bot]"' in gate and "TASK_AGENT_" not in gate
    assert gate.endswith("      - run: make check\n")
    run_git(base.root, "tag", "v1.2.0")
    assert f"ref: {sha} # v1.2.0\n" in init.managed(base, cfg)[".github/workflows/close-out.yml"]


def test_gate_ends_with_the_target_s_own_step(
    gh: Gh, base: config.Config, cfg: config.Config, github: Path
) -> None:
    (cfg.clone / "pyproject.toml").write_text('[tool.task-agent]\ngate_step = "make docs"\n')
    (cfg.clone / "LICENSE.md").write_text("theirs\n")
    run_git(cfg.clone, "commit", "--quiet", "-am", "step")
    run_git(cfg.clone, "add", "LICENSE.md")
    run_git(cfg.clone, "commit", "--quiet", "-m", "license")
    run_git(cfg.clone, "push", "--quiet", "origin", "main")
    files = init.managed(base, cfg)
    assert files[".github/workflows/gate.yml"].endswith(
        "      - run: make check\n      - run: |\n          make docs\n"
    )
    assert "LICENSE" not in files


@pytest.mark.parametrize(
    ("rulesets", "call"),
    [
        ('[{"id": 4, "name": "other"}]', ["api", "-X", "POST", "repos/owner/repo/rulesets"]),
        ('[{"id": 7, "name": "main"}]', ["api", "-X", "PUT", "repos/owner/repo/rulesets/7"]),
    ],
)
def test_an_up_to_date_repo_gets_the_ruleset(
    gh: Gh,
    base: config.Config,
    cfg: config.Config,
    capsys: pytest.CaptureFixture[str],
    rulesets: str,
    call: list[str],
) -> None:
    calls = gh(rulesets=rulesets)
    init.run(base, cfg)
    merge(cfg.clone)
    init.run(base, cfg)
    assert capsys.readouterr().out.endswith("owner/repo is up to date\n")
    assert calls()[-1] == [[*call, "--input", "-"], (ROOT / ".github/ruleset.json").read_text()]
    assert sum(made[0][:2] == ["pr", "create"] for made in calls()) == 1


def test_init_command_aims_main_s_settings_at_the_repo_and_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[str, str, Path]] = []
    text = (cli.ROOT / "pyproject.toml").read_text()
    monkeypatch.setattr(dispatch, "show_main", lambda root, path: text)
    monkeypatch.setattr(init, "run", lambda base, to: seen.append((base.repo, to.repo, to.clone)))
    assert cli.main(["init", "--repo", "o/new", "--clone", str(tmp_path)]) == 0
    assert seen == [("yasabbasi/task-agent", "o/new", tmp_path.resolve())]
