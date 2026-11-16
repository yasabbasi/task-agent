"""Set up another repo for the loop, as whoever is signed in to gh; safe to run again."""

import json
import re
import subprocess
import tempfile
import textwrap
import unicodedata
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
VERSION = re.compile(r"v\d+\.\d+\.\d+")
ORIGIN = r"(https://github\.com/|git@github\.com:|ssh://git@github\.com/){}(\.git)?/?"
FREE = "warning: {} is private and its owner is on the free plan, so rulesets are not enforced"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run git with the caller's own configuration, so a commit is the caller's."""
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)
    return done.stdout if done.returncode == 0 else ""


def show(clone: Path, path: str, ref: str = "origin/main", check: bool = True) -> str:
    """A file as it is at ref; "" when it is missing and check is off."""
    return git(clone, "show", f"{ref}:{path}", check=check)


def pin(root: Path) -> tuple[str, str]:
    """Fetch origin with its tags; the commit every source is read from, and its name."""
    git(root, "fetch", "--tags", "origin")
    tags = git(root, "tag", "--merged", "origin/main", "--sort=-v:refname", "--list", "v*")
    found = [tag for tag in tags.split() if VERSION.fullmatch(tag)]
    sha = git(root, "rev-parse", f"{found[0] if found else 'origin/main'}^{{commit}}").strip()
    return sha, found[0] if found else sha[:7]


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


def managed(base: Config, target: Config, sha: str, tag: str) -> dict[str, str]:
    """Each managed path with the text it should have on the target's main."""
    files = {path: show(base.root, path, sha) for path in COPIED}
    top = git(target.clone, "ls-tree", "--name-only", "origin/main").splitlines()
    if not any(name.upper().startswith("LICEN") for name in top):
        files["LICENSE"] = show(base.root, "LICENSE", sha)
    values = {"TASK_AGENT_REPO": base.repo, "TASK_AGENT_BOT": base.bot}
    values |= {"TASK_AGENT_SHA": sha, "TASK_AGENT_TAG": tag}
    for name in WORKFLOWS:
        text = show(base.root, f".github/templates/{name}", sha)
        for key, value in values.items():
            text = text.replace(key, value)
        files[f".github/workflows/{name}"] = text
    step = config.table(show(target.clone, "pyproject.toml", check=False)).get("gate_step")
    if step:
        command = textwrap.indent(step.strip() + "\n", " " * 10)
        files[".github/workflows/gate.yml"] += f"      - run: |\n{command}"
    return files


def propose(target: Config, changed: dict[str, str]) -> str:
    """Bring the branch to the files on top of main, pushing only a change; the PR's link."""
    git(target.clone, "worktree", "prune")
    head = "".join(git(target.clone, "ls-remote", "origin", f"refs/heads/{BRANCH}").split()[:1])
    git(target.clone, "fetch", "--quiet", "origin", BRANCH, check=False)
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "init"
        git(target.clone, "worktree", "add", "--quiet", "-B", BRANCH, str(tree), "origin/main")
        try:
            for path, text in changed.items():
                if any((tree / up).is_symlink() for up in (Path(path), *Path(path).parents)):
                    raise SystemExit(f"{path}: it or a folder above it is a worktree symlink")
                (tree / path).parent.mkdir(parents=True, exist_ok=True)
                (tree / path).write_text(text)
            git(tree, "add", "--", *changed)
            new = [git(tree, "rev-parse", "origin/main").strip(), git(tree, "write-tree").strip()]
            old = git(tree, "rev-parse", f"{head}^", f"{head}^{{tree}}", check=False).split()
            if not head or old != new:
                git(tree, "commit", "--quiet", "-m", TITLE)
                git(tree, "push", "--quiet", "--force", "origin", BRANCH)
                head = git(tree, "rev-parse", "HEAD").strip()
        finally:
            git(target.clone, "worktree", "remove", "--force", str(tree))
    aimed = ["--repo", target.repo, "--head", BRANCH]
    body = "Files brought up to date by `task-agent init`:\n\n"
    body += "".join(f"- `{path}`\n" for path in changed)
    for pr in json.loads(gh("pr", "list", *aimed, "--json", "url,isCrossRepository,headRefOid")):
        if not pr["isCrossRepository"] and pr["headRefOid"] == head:
            gh("pr", "edit", pr["url"], "--body", body)
            return str(pr["url"])
    labeled = [*aimed, "--label", "human-authored"]
    return gh("pr", "create", *labeled, "--title", TITLE, "--body", body).strip()


def ruleset(repo: str, text: str) -> None:
    """Create the ruleset, or replace the one that already has its name."""
    name, path = json.loads(text)["name"], f"repos/{repo}/rulesets"
    ids = [found["id"] for found in json.loads(gh("api", path)) if found["name"] == name]
    method, path = ("PUT", f"{path}/{ids[0]}") if ids else ("POST", path)
    gh("api", "-X", method, path, "--input", "-", input=text)


def steps(base: Config, target: Config) -> None:
    repo = target.repo
    if repo.lower() == base.repo.lower():
        raise SystemExit(f"{repo} is task-agent itself; its workflows are written by hand")
    urls = [("get-url", "--all", "origin"), ("get-url", "--push", "--all", "origin")]
    lists = [git(target.clone, "remote", *url, check=False) for url in urls * target.clone.is_dir()]
    found = [url for text in lists for url in text.split()]
    wanted = re.compile(ORIGIN.format(re.escape(repo)), re.IGNORECASE | re.ASCII)
    if not found or not all(wanted.fullmatch(url) for url in found):
        raise SystemExit(f"origin of {target.clone} is not {repo} on github.com")
    sha, tag = pin(base.root)
    git(target.clone, "fetch", "--quiet", "origin", "main")
    files = managed(base, target, sha, tag)
    ways = {str(up) for path in files for up in (Path(path), *Path(path).parents)}
    above = {unicodedata.normalize("NFD", way).casefold() for way in ways}
    listed = git(target.clone, "ls-tree", "-r", "-t", "-z", "origin/main").split("\0")
    links = [e.partition("\t")[2] for e in listed if e.startswith("120000")]
    if any(unicodedata.normalize("NFD", link).casefold() in above for link in links):
        raise SystemExit(f"{repo}: a managed path or a folder above it is a symlink on main")
    settings(repo, show(base.root, ".github/labels.json", sha))
    changed = {p: text for p, text in files.items() if show(target.clone, p, check=False) != text}
    if changed:
        print(f"{repo}: merge {propose(target, changed)} and run init again for the ruleset")
        return
    ruleset(repo, show(base.root, ".github/ruleset.json", sha))
    print(f"{repo} is up to date")


def run(base: Config, target: Config) -> None:
    """Settings first, then a pull request for files that differ, else the ruleset."""
    try:
        steps(base, target)
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"{' '.join(error.cmd[:2])}: {error.stderr.strip()}") from None
