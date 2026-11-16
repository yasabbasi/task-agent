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

    def answer(
        visibility: str = "public", plan: str = "free", rulesets: str = "[]", prs: str = "[]"
    ) -> Any:
        answers = {"api repos/owner/repo/rulesets": rulesets, "pr create": LINK, "pr list": prs}
        answers |= {"api repos/owner/repo --jq": visibility, "api user": plan}
        (bin_dir / "gh-answers.json").write_text(json.dumps(answers))
        log = bin_dir / "gh-calls.json"
        return lambda: json.loads(log.read_text()) if log.exists() else []

    return answer


@pytest.fixture
def base(tmp_path: Path) -> config.Config:
    """A checkout of task-agent whose origin holds this working tree's .github at v1.2.0.

    Its own HEAD and working tree have moved on, so they must not be what init reads.
    """
    upstream, home = tmp_path / "upstream", tmp_path / "home"
    shutil.copytree(ROOT / ".github", upstream / ".github")
    shutil.copy(ROOT / "LICENSE", upstream / "LICENSE")
    run_git(upstream, "init", "--quiet", "-b", "main")
    run_git(upstream, "add", "-A")
    run_git(upstream, "commit", "--quiet", "-m", "base")
    run_git(upstream, "tag", "v1.2.0")
    run_git(tmp_path, "clone", "--quiet", str(upstream), "home")
    (home / "LICENSE").write_text("moved on\n")
    shutil.rmtree(home / ".github")
    run_git(home, "commit", "--quiet", "-am", "local")
    return replace(CFG, repo="owner/task-agent", root=home)


@pytest.fixture
def cfg(cfg: config.Config) -> config.Config:
    """The clone's origin is spelled as GitHub spells it, which init checks."""
    run_git(cfg.clone, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    return cfg


def merge(github: Path) -> None:
    """Merge init's branch from a second clone, so the first sees it only by fetching."""
    run_git(github.parent, "clone", "--quiet", str(github), "second")
    branch = f"origin/{init.BRANCH}:refs/heads/main"
    run_git(github.parent / "second", "push", "--quiet", "origin", branch)


def later(base: config.Config, text: str = "later\n") -> str:
    """Add a commit to the source repo's current branch, and return it."""
    upstream = base.root.parent / "upstream"
    (upstream / "LICENSE").write_text(text)
    run_git(upstream, "commit", "--quiet", "-am", "later")
    return run_git(upstream, "rev-parse", "HEAD").strip()


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


def test_sources_are_read_from_the_newest_version_tag_on_main(
    gh: Gh, base: config.Config, cfg: config.Config
) -> None:
    upstream = base.root.parent / "upstream"
    sha = run_git(upstream, "rev-parse", "v1.2.0").strip()
    later(base)
    run_git(upstream, "tag", "v1")
    run_git(upstream, "checkout", "--quiet", "-b", "side")
    newer = later(base, "not merged\n")
    run_git(upstream, "tag", "v1.10.0")
    run_git(upstream, "checkout", "--quiet", "main")
    assert init.pin(base.root) == (sha, "v1.2.0")
    files = init.managed(base, cfg, sha, "v1.2.0")
    gate = files[".github/workflows/gate.yml"]
    assert f"repository: owner/task-agent\n          ref: {sha} # v1.2.0\n" in gate
    assert '--bot "task-agent-test[bot]"' in gate and "TASK_AGENT_" not in gate
    assert gate.endswith("      - run: make check\n")
    assert files["LICENSE"] == (ROOT / "LICENSE").read_text()
    run_git(upstream, "merge", "--quiet", "--no-edit", "side")
    assert init.pin(base.root) == (newer, "v1.10.0")


def test_without_a_version_tag_sources_are_read_from_main(
    gh: Gh, base: config.Config, cfg: config.Config
) -> None:
    for repo in (base.root, base.root.parent / "upstream"):
        run_git(repo, "tag", "-d", "v1.2.0")
    sha = later(base)
    assert init.pin(base.root) == (sha, sha[:7])
    assert init.managed(base, cfg, sha, sha[:7])["LICENSE"] == "later\n"


def test_init_stops_before_changing_anything_at_the_wrong_target(
    gh: Gh, base: config.Config, cfg: config.Config
) -> None:
    calls = gh()
    with pytest.raises(SystemExit, match="is task-agent itself"):
        init.run(base, replace(cfg, repo="Owner/Task-Agent"))
    with pytest.raises(SystemExit, match="is not owner/other"):
        init.run(base, replace(cfg, repo="owner/other"))
    with pytest.raises(SystemExit, match="is not wner/repo"):
        init.run(base, replace(cfg, repo="wner/repo"))
    assert calls() == []


@pytest.mark.parametrize("link", [".github", ".github/workflows", ".github/CODEOWNERS"])
def test_init_stops_at_a_symlink_on_the_way_to_a_managed_path(
    gh: Gh, base: config.Config, cfg: config.Config, link: str
) -> None:
    calls = gh()
    (cfg.clone / link).parent.mkdir(exist_ok=True)
    (cfg.clone / link).symlink_to("elsewhere")
    run_git(cfg.clone, "add", "-A")
    run_git(cfg.clone, "commit", "--quiet", "-m", "link")
    run_git(cfg.clone, "push", "--quiet", "origin", "main")
    with pytest.raises(SystemExit, match="is a symlink on main"):
        init.run(base, cfg)
    assert calls() == []


def test_an_ssh_origin_names_the_repo_too(
    gh: Gh,
    base: config.Config,
    cfg: config.Config,
    github: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh()
    ssh = "git@github.com:Owner/Repo.git"
    run_git(cfg.clone, "config", f"url.{github}.insteadOf", ssh)
    run_git(cfg.clone, "remote", "set-url", "origin", ssh)
    init.run(base, cfg)
    assert LINK in capsys.readouterr().out


def test_a_failed_step_ends_in_one_message_and_no_traceback(
    gh: Gh, base: config.Config, cfg: config.Config, tmp_path: Path
) -> None:
    gh()
    (tmp_path / "gh-bin" / "gh").write_text("#!/bin/sh\necho not signed in >&2\nexit 1\n")
    with pytest.raises(SystemExit) as stopped:
        init.run(base, cfg)
    assert stopped.value.code == "gh api: not signed in"


def test_a_leftover_worktree_for_the_branch_is_pruned(
    gh: Gh, base: config.Config, cfg: config.Config, tmp_path: Path, github: Path
) -> None:
    gh()
    run_git(cfg.clone, "worktree", "add", "--quiet", "-B", init.BRANCH, str(tmp_path / "left"))
    shutil.rmtree(tmp_path / "left")
    init.run(base, cfg)
    assert run_git(github, "diff", "--name-only", "main", init.BRANCH)


@pytest.mark.parametrize(("fork", "stale"), [(False, False), (True, False), (False, True)])
def test_an_open_pull_request_is_reused_only_when_it_is_init_s_own(
    gh: Gh,
    base: config.Config,
    cfg: config.Config,
    github: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fork: bool,
    stale: bool,
) -> None:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_DATE", "2026-01-01T00:00:00Z")
    calls = gh()
    init.run(base, cfg)
    head = "0" * 40 if stale else run_git(github, "rev-parse", init.BRANCH).strip()
    other = "https://github.com/owner/repo/pull/9"
    gh(prs=json.dumps([{"url": other, "isCrossRepository": fork, "headRefOid": head}]))
    init.run(base, cfg)
    made = [call[0] for call in calls() if call[0][:2] in (["pr", "create"], ["pr", "edit"])]
    body = made[0][-1]
    assert "- `LICENSE`\n" in body
    if fork or stale:
        assert made[1] == made[0]
        assert capsys.readouterr().out.count(LINK) == 2
    else:
        assert made[1:] == [["pr", "edit", other, "--body", body]]
        assert f"merge {other} and" in capsys.readouterr().out


def test_gate_ends_with_the_target_s_own_step(
    gh: Gh, base: config.Config, cfg: config.Config, github: Path
) -> None:
    (cfg.clone / "pyproject.toml").write_text('[tool.task-agent]\ngate_step = "make docs"\n')
    (cfg.clone / "LICENSE.md").write_text("theirs\n")
    run_git(cfg.clone, "commit", "--quiet", "-am", "step")
    run_git(cfg.clone, "add", "LICENSE.md")
    run_git(cfg.clone, "commit", "--quiet", "-m", "license")
    run_git(cfg.clone, "push", "--quiet", "origin", "main")
    files = init.managed(base, cfg, *init.pin(base.root))
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
    github: Path,
    capsys: pytest.CaptureFixture[str],
    rulesets: str,
    call: list[str],
) -> None:
    calls = gh(rulesets=rulesets)
    init.run(base, cfg)
    merge(github)
    init.run(base, cfg)
    assert capsys.readouterr().out.endswith("owner/repo is up to date\n")
    assert calls()[-1] == [[*call, "--input", "-"], (ROOT / ".github/ruleset.json").read_text()]
    assert sum(made[0][:2] == ["pr", "create"] for made in calls()) == 1


def test_init_module_stays_within_150_lines() -> None:
    assert len((ROOT / "src/task_agent/init.py").read_text().splitlines()) <= 150


def test_init_command_aims_main_s_settings_at_the_repo_and_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[str, str, Path]] = []
    text = (cli.ROOT / "pyproject.toml").read_text()
    monkeypatch.setattr(dispatch, "show_main", lambda root, path: text)
    monkeypatch.setattr(init, "run", lambda base, to: seen.append((base.repo, to.repo, to.clone)))
    assert cli.main(["init", "--repo", "o/new", "--clone", str(tmp_path)]) == 0
    assert seen == [("yasabbasi/task-agent", "o/new", tmp_path.resolve())]
