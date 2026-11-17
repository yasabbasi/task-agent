"""Check that this machine can run agents. Every failure names its fix."""

import os
import subprocess
import sys
from pathlib import Path

from task_agent.app_token import App, call, read_key
from task_agent.config import Config, load

Check = tuple[bool, str, str]


def run(*cmd: str, cwd: Path | None = None) -> tuple[bool, str]:
    env = os.environ | {"GIT_CONFIG_GLOBAL": "/dev/null"} if cmd[0] == "git" else None
    try:
        done = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return done.returncode == 0, (done.stdout + done.stderr).strip()


def machine() -> list[Check]:
    """The tools: gh's scopes, the claude sign-in, and the key file or 1Password's CLI."""
    _, scopes = run("gh", "auth", "status")
    if path := os.environ.get("TASK_AGENT_KEY_FILE"):
        fix = "point TASK_AGENT_KEY_FILE at a file that holds the App's private key"
        key = (os.access(path, os.R_OK) and Path(path).is_file(), f"{path} can be read", fix)
    else:
        fix = "turn on 1Password's CLI integration, and on macOS 27 allow your terminal under"
        fix += " Files & Folders > Data shared by 1Password"
        key = (run("op", "vault", "list")[0], "op can list vaults", fix)
    return [
        ("'repo'" in scopes and "'workflow'" in scopes, "gh has the repo and workflow scopes",
         "gh auth refresh -s repo,workflow"),
        (run("claude", "auth", "status")[0], "claude is signed in", "run `claude` and sign in"),
        key,
    ]  # fmt: skip


def clone(cfg: Config) -> list[Check]:
    """One repo's local clone: an HTTPS origin, and main tracking origin/main."""
    if not (cfg.clone / ".git").exists():
        return [
            (False, f"{cfg.repo}: clone at {cfg.clone}", f"gh repo clone {cfg.repo} {cfg.clone}")
        ]
    url = f"https://github.com/{cfg.repo}"
    origin = run("git", "config", "--get", "remote.origin.url", cwd=cfg.clone)[1].removesuffix(
        ".git"
    )
    checks = [
        (origin == url, f"{cfg.repo}: origin is HTTPS", f"git remote set-url origin {url}.git")
    ]
    if run("git", "rev-parse", "--verify", "--quiet", "main", cwd=cfg.clone)[0]:
        tracks = run("git", "rev-parse", "--abbrev-ref", "main@{upstream}", cwd=cfg.clone)[1]
        fix = "git branch --set-upstream-to=origin/main main"
        checks.append((tracks == "origin/main", f"{cfg.repo}: main tracks origin/main", fix))
    return checks


def installed(cfg: Config, app: App) -> list[Check]:
    """The App behind the key is the configured one, and it is installed on every repo."""
    found = run_call("/app", app)
    checks = [(found == cfg.app_slug, f"the key belongs to the App {cfg.app_slug}",
               "check client_id, key_title and key_vault in pyproject.toml")]  # fmt: skip
    fix = f"install it at https://github.com/apps/{cfg.app_slug}/installations/new"
    for repo in cfg.repos:
        on = run_call(f"/repos/{repo}/installation", app) == cfg.app_slug
        checks.append((on, f"{repo}: the App is installed", fix))
    return checks


def run_call(path: str, app: App) -> str:
    try:
        answer = call("GET", path, app.jwt())
    except OSError as exc:
        return str(exc)
    return str(answer.get("slug") or answer.get("app_slug"))


def main() -> int:
    """Print every check as pass or fail, with the fix after a failure."""
    cfg = load()
    checks = machine()
    if all(ok for ok, _, _ in checks):
        checks += installed(cfg, App(cfg.client_id, read_key(cfg.key_title, cfg.key_vault)))
    for repo in cfg.repos:
        checks += clone(cfg.target(repo))
    for ok, text, fix in checks:
        print(f"{'pass' if ok else 'fail'}: {text}" + ("" if ok else f". Fix: {fix}"))
    return 0 if all(ok for ok, _, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
