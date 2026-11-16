"""Set up another repo for the loop, as whoever is signed in to gh; safe to run again."""

import json
import subprocess
import tempfile
import textwrap
from pathlib import Path

from task_agent import config
from task_agent.config import Config
from task_agent.gh import gh

BRANCH, TITLE = "chore/task-agent-init", "Set up task-agent"
COPIED = (
    ".github/CODEOWNERS",
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/task.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
)
WORKFLOWS = ("gate.yml", "readiness.yml", "close-out.yml")
FREE = "warning: {} is private and its owner is on the free plan, so rulesets are not enforced"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run git with the caller's own configuration, so a commit is the caller's."""
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)
    return done.stdout if done.returncode == 0 else ""


def show(clone: Path, path: str, check: bool = True) -> str:
    """A file as it is on origin/main; "" when it is missing and check is off."""
    return git(clone, "show", f"origin/main:{path}", check=check)


def settings(repo: str, labels: str) -> None:
    """Warn about a private repo on the free plan, then set labels and repo settings."""
    private = gh("api", f"repos/{repo}", "--jq", ".visibility").strip() == "private"
    if private and gh("api", "user", "--jq", ".plan.name").strip() == "free":
        print(FREE.format(repo))
    for label in json.loads(labels):
        colors = ["--color", label["color"], "--description", label["description"]]
        gh("label", "create", label["name"], "--repo", repo, *colors, "--force")
    merges = ["--enable-squash-merge", "--enable-merge-commit=false", "--enable-rebase-merge=false"]
    gh("repo", "edit", repo, *merges, "--delete-branch-on-merge")
    reviews = ["-F", "can_approve_pull_request_reviews=false"]
    path = f"repos/{repo}/actions/permissions/workflow"
    gh("api", "-X", "PUT", path, "-f", "default_workflow_permissions=read", *reviews)


def managed(base: Config, target: Config) -> dict[str, str]:
    """Each managed path with the text it should have on the target's main."""
    files = {path: show(base.root, path) for path in COPIED}
    top = git(target.clone, "ls-tree", "--name-only", "origin/main").splitlines()
    if not any(name.upper().startswith("LICEN") for name in top):
        files["LICENSE"] = show(base.root, "LICENSE")
    sha = git(base.root, "rev-parse", "origin/main").strip()
    tags = git(base.root, "tag", "--points-at", sha).split()
    values = {"TASK_AGENT_REPO": base.repo, "TASK_AGENT_BOT": base.bot}
    values |= {"TASK_AGENT_SHA": sha, "TASK_AGENT_TAG": tags[0] if tags else sha[:7]}
    for name in WORKFLOWS:
        text = show(base.root, f".github/templates/{name}")
        for key, value in values.items():
            text = text.replace(key, value)
        files[f".github/workflows/{name}"] = text
    step = config.table(show(target.clone, "pyproject.toml", check=False)).get("gate_step")
    if step:
        command = textwrap.indent(step.strip() + "\n", " " * 10)
        files[".github/workflows/gate.yml"] += f"      - run: |\n{command}"
    return files


def propose(target: Config, changed: dict[str, str]) -> str:
    """Commit the files on a branch in a temporary worktree, push it, and return the PR's link."""
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "init"
        git(target.clone, "worktree", "add", "--quiet", "-B", BRANCH, str(tree), "origin/main")
        try:
            for path, text in changed.items():
                (tree / path).parent.mkdir(parents=True, exist_ok=True)
                (tree / path).write_text(text)
            git(tree, "add", "--", *changed)
            git(tree, "commit", "--quiet", "-m", TITLE)
            git(tree, "push", "--quiet", "--force", "origin", BRANCH)
        finally:
            git(target.clone, "worktree", "remove", "--force", str(tree))
    aimed = ["--repo", target.repo, "--head", BRANCH]
    if url := gh("pr", "list", *aimed, "--json", "url", "--jq", ".[].url").strip():
        return url
    body = "Files brought up to date by `task-agent init`:\n\n"
    body += "".join(f"- `{path}`\n" for path in changed)
    labeled = [*aimed, "--label", "human-authored"]
    return gh("pr", "create", *labeled, "--title", TITLE, "--body", body).strip()


def ruleset(repo: str, text: str) -> None:
    """Create the ruleset, or replace the one that already has its name."""
    name, path = json.loads(text)["name"], f"repos/{repo}/rulesets"
    ids = [found["id"] for found in json.loads(gh("api", path)) if found["name"] == name]
    method, path = ("PUT", f"{path}/{ids[0]}") if ids else ("POST", path)
    gh("api", "-X", method, path, "--input", "-", input=text)


def run(base: Config, target: Config) -> None:
    """Settings first, then a pull request for files that differ, else the ruleset."""
    repo = target.repo
    settings(repo, show(base.root, ".github/labels.json"))
    git(target.clone, "fetch", "--quiet", "origin", "main")
    files = managed(base, target)
    changed = {p: text for p, text in files.items() if show(target.clone, p, check=False) != text}
    if changed:
        print(f"{repo}: merge {propose(target, changed)} and run init again for the ruleset")
        return
    ruleset(repo, show(base.root, ".github/ruleset.json"))
    print(f"{repo} is up to date")
