"""Take ready tasks, run one sandboxed Claude Code session for each, open the pull requests."""

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from task_agent import config, pr_format, readiness
from task_agent.app_token import Api, App, call
from task_agent.config import Config

Json = dict[str, Any]
Out = Callable[[str], None]
CLAIM_RE = re.compile(r"<!-- task-agent:claim run=(\S+) expires=(\S+) -->\n")
CLAIM_TTL = timedelta(minutes=90)
REFUSE = (".claude/settings.json", ".claude/settings.local.json", ".mcp.json", ".venv", "uv.toml")
SECRET_RE = re.compile(r"gh[opsur]_\w{20,}|github_pat_\w{20,}|-----BEGIN [A-Z ]*PRIVATE KEY")
KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR")
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
SIGN_IN = '!f() { echo username=x-access-token; echo "password=$TASK_AGENT_TOKEN"; }; f'
GIT_LOCK = threading.Lock()  # fetch, worktree add and worktree remove take git's own locks
CANCEL = threading.Event()
SESSIONS: set[subprocess.Popen[str]] = set()
SESSIONS_LOCK = threading.Lock()
PINNED = ("pyproject.toml", "uv.lock", ".python-version")
REWORK = "<!-- task-agent:rework until={} -->\n"
REWORK_RE = re.compile(r"<!-- task-agent:rework(?: until=(\S+))? -->\n")
NOT_TAKEN = "#{}: not taken, the label isn't the owner's, or the issue changed after it."
NOT_TAKEN += " The owner adds `status:ready` again"
RUN_RE = re.compile(r"<!-- task-agent:run cost=(\d+\.\d+) turns=(\d+) secs=(\d+) -->")
FIELDS = ("pr_title", "pr_body")
SCHEMA = {
    "type": "object",
    "properties": {name: {"type": "string"} for name in FIELDS},
    "required": list(FIELDS),
    "additionalProperties": False,
}
APPROVAL = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) {
    title body lastEditedAt
    timelineItems(itemTypes: [LABELED_EVENT, RENAMED_TITLE_EVENT], last: 100) { nodes {
      __typename
      ... on LabeledEvent { createdAt actor { login } label { name } }
      ... on RenamedTitleEvent { createdAt }
    } }
  } }
}"""
REPAIR = """The pull request description below fails these checks:

{problems}

{size} Change no files. Return the same pr_title, and the whole corrected description as pr_body.

<description>
{body}
</description>"""


class Blocked(Exception):
    """Stop this task and hand it back to the owner."""

    def __init__(self, reason: str, result: Json | None = None) -> None:
        super().__init__(reason)
        self.result = result or {}


class Claimed(Exception):
    """Another dispatcher holds the oldest live claim; the message is when it expires."""


def now() -> datetime:
    return datetime.now(UTC)


def git(cwd: Path, *args: str, token: str = "") -> str:
    """Run git without the owner's global config, hooks or signing; a token signs in."""
    sign_in = ["-c", "credential.helper=", "-c", f"credential.helper={SIGN_IN}"] if token else []
    cmd = [*GIT, "-c", "commit.gpgsign=false", *sign_in, *args]
    env = os.environ | GIT_ENV | {"TASK_AGENT_TOKEN": token}
    done = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=300
    )
    return done.stdout


def fetch_main(clone: Path, repo: str, token: str) -> None:
    """Update origin/main from the repo's HTTPS URL, whatever the origin remote says."""
    refspec = "+refs/heads/main:refs/remotes/origin/main"
    git(clone, "fetch", "--quiet", f"https://github.com/{repo}.git", refspec, token=token)


def show_main(clone: Path, path: str) -> str:
    """A file as it is on origin/main, never as it is in the working tree."""
    return git(clone, "show", f"origin/main:{path}")


def approved(api: Api, number: int, cfg: Config) -> Json | None:
    """The issue as the owner marked it ready, or None when that approval no longer holds."""
    # The latest ready label must be the owner's, or the App's when it puts a cancelled task
    # back, and the title and body must be unchanged since the owner's own label.
    issue: Json = api.graphql(APPROVAL, number=number)["repository"]["issue"]
    events = issue["timelineItems"]["nodes"]
    ready = [
        ((e["actor"] or {}).get("login"), e["createdAt"])
        for e in events
        if e["__typename"] == "LabeledEvent" and e["label"]["name"] == "status:ready"
    ]
    owners = [at for who, at in ready if who == cfg.owner]
    if not owners or ready[-1][0] not in (cfg.owner, cfg.app_slug, cfg.bot):
        return None
    edits = [e["createdAt"] for e in events if e["__typename"] == "RenamedTitleEvent"]
    return None if any(at and at >= owners[-1] for at in [*edits, issue["lastEditedAt"]]) else issue


def pages(api: Api, path: str, page: int = 1) -> list[Json]:
    """Every item of a list, read through each page of 100."""
    found: list[Json] = api("GET", f"{path}?per_page=100&page={page}")
    return found + (pages(api, path, page + 1) if len(found) == 100 else [])


def live_claims(api: Api, number: int, bot: str) -> list[tuple[int, str, str]]:
    """(comment id, run id, expiry) of unexpired claims posted by the App, oldest first."""
    claims = []
    for comment in pages(api, f"/issues/{number}/comments"):
        match = CLAIM_RE.match(comment["body"] or "")
        if match and comment["user"]["login"] == bot and datetime.fromisoformat(match[2]) > now():
            claims.append((int(comment["id"]), match[1], match[2]))
    return sorted(claims)


def labeled(api: Api, label: str) -> list[Json]:
    """Open issues with this label, without pull requests."""
    found = api("GET", f"/issues?state=open&labels={quote(label)}&per_page=100")
    return [issue for issue in found if "pull_request" not in issue]


def priority(issue: Json) -> tuple[str, int]:
    labels = sorted(lb["name"] for lb in issue["labels"] if lb["name"].startswith("priority:"))
    return (labels[0] if labels else "priority:p9", issue["number"])


def select(api: Api, cfg: Config, waits: list[str]) -> list[Json]:
    """Ready issues, and claimed ones whose claim expired; refusals are described in waits."""
    picks = []
    for label in ("status:ready", "status:claimed"):
        for issue in labeled(api, label):
            number = issue["number"]
            if label == "status:claimed" and live_claims(api, number, cfg.bot):
                continue
            if text := approved(api, number, cfg):
                picks.append(issue | {"title": text["title"], "body": text["body"]})
            else:
                waits.append(NOT_TAKEN.format(number))
    return sorted(picks, key=priority)


def pull_for(api: Api, cfg: Config, number: int) -> Json | None:
    """The open pull request the App wrote for an issue from a branch in this repo, if any."""
    branch = f"agent/issue-{number}"
    for pull in api("GET", f"/pulls?state=open&head={quote(f'{cfg.owner}:{branch}')}"):
        head = pull["head"]
        ours = (head["repo"] or {}).get("full_name") == cfg.repo and head["ref"] == branch
        if ours and pull["user"]["login"] == cfg.bot:
            return dict(pull)
    return None


def feedback(api: Api, cfg: Config, pull: Json) -> tuple[str, list[str]]:
    """What the owner wrote on a pull request after its last push, or after the newest comment
    the last rework was given, oldest first; and the time of the newest of them."""
    number = pull["number"]
    notes = pages(api, f"/issues/{number}/comments")
    reviews = pages(api, f"/pulls/{number}/reviews")
    approving = {r["id"] for r in reviews if r["state"] == "APPROVED"}
    inline = [
        c | {"body": f"{c['path']}: {c['body']}"}
        for c in pages(api, f"/pulls/{number}/comments")
        if c["pull_request_review_id"] not in approving
    ]
    ours = [(c, REWORK_RE.search(c["body"] or "")) for c in notes if c["user"]["login"] == cfg.bot]
    marks = [m[1] or c["created_at"] for c, m in ours if m]
    pushed = api("GET", f"/commits/{pull['head']['sha']}")["commit"]["committer"]["date"]
    since = marks[-1] if marks else pushed
    found = [*notes, *(r for r in reviews if r["id"] not in approving), *inline]
    dated = [(c.get("submitted_at") or c.get("created_at") or "", c["body"] or "") for c in found]
    mine = [c["user"]["login"] == cfg.owner for c in found]
    new = [said for said, ok in sorted(zip(dated, mine, strict=True)) if ok and said[0] > since]
    return max([since, *(at for at, _ in new)]), [text for _, text in new]


def conflicted(api: Api, number: int) -> bool | None:
    """Whether a pull request conflicts with main; None while GitHub still doesn't know."""
    for attempt in range(6):
        time.sleep(2 if attempt else 0)
        mergeable = api("GET", f"/pulls/{number}")["mergeable"]
        if mergeable is not None:
            return not mergeable
    return None


def reworks(api: Api, cfg: Config, out: Out) -> list[Json]:
    """Issues in review whose pull request the owner commented on, or that conflicts with main."""
    picks = []
    for issue in labeled(api, "status:review"):
        number = issue["number"]
        try:
            pull = pull_for(api, cfg, number)
            due = pull and (feedback(api, cfg, pull)[1] or conflicted(api, pull["number"]))
            text = approved(api, number, cfg) if due else None
        except Exception as exc:  # one failed lookup must not stop the pass
            out(f"#{number}: rework check failed ({failure(exc).splitlines()[0]})")
            continue
        if text:
            picks.append(issue | {"title": text["title"], "body": text["body"]})
        elif due:
            out(NOT_TAKEN.format(number))
        elif pull and due is None:
            out(f"#{number}: conflict check pending")
    return picks


def blockers(app: App, api: Api, issue: Json) -> list[str]:
    """Each dependency of a task that is not closed as completed, with why it still holds."""
    held = []
    for repo, number in readiness.depends_on(readiness.parse_body(issue["body"]), api.repo):
        try:
            other = api if repo == api.repo else app.token(repo, issues="read")
            dep: Json = other("GET", f"/issues/{number}")
        except Exception as exc:  # one failed lookup holds only this task
            code = getattr(exc, "code", None)
            held_by = f"HTTP {code}" if code else type(exc).__name__
            dep = {} if code in (403, 404) else {"state": held_by}
        if dep.get("state") == "open":
            stuck = any(lb["name"] == "status:blocked" for lb in dep["labels"])
            why = "blocked" if stuck else "open"
        else:
            merged = (dep.get("pull_request") or {}).get("merged_at") and "completed"
            why = merged or dep.get("state_reason") or dep.get("state", "not found")
            why = why.replace("_", " ")
        if why != "completed":
            held.append(f"{'' if repo == api.repo else repo}#{number} ({why})")
    return held


def set_status(api: Api, number: int, status: str) -> None:
    """Add the new status label first, so a failed call never leaves an issue with none."""
    for label in api("POST", f"/issues/{number}/labels", {"labels": [status]}):
        if label["name"].startswith("status:") and label["name"] != status:
            api("DELETE", f"/issues/{number}/labels/{quote(label['name'])}")


def claim(api: Api, number: int, bot: str) -> int:
    """Post a claim and return its comment id; raise Claimed when an older live one exists."""
    run = uuid.uuid4().hex[:12]
    expires = (now() + CLAIM_TTL).isoformat(timespec="seconds")
    body = f"<!-- task-agent:claim run={run} expires={expires} -->\nClaimed by the dispatcher."
    mine = api("POST", f"/issues/{number}/comments", {"body": body})
    claims = live_claims(api, number, bot)
    if not claims or claims[0][1] != run:
        api("DELETE", f"/issues/comments/{mine['id']}")
        raise Claimed(claims[0][2] if claims else "unknown")
    set_status(api, number, "status:claimed")
    return int(mine["id"])


def identity(token: str, bot: str) -> list[str]:
    """git -c flags for the App's name and email, so a commit needs no git config."""
    bot_id = call("GET", "/users/" + quote(bot), token)["id"]
    return ["-c", f"user.name={bot}", "-c", f"user.email={bot_id}+{bot}@users.noreply.github.com"]


def discard(cfg: Config, worktree: Path) -> None:
    """Remove a worktree and its registration. Callers hold GIT_LOCK."""
    if worktree.exists():
        try:
            git(cfg.clone, "worktree", "remove", "--force", str(worktree))
        except subprocess.SubprocessError:
            shutil.rmtree(worktree, ignore_errors=True)
    git(cfg.clone, "worktree", "prune")


def prepare(cfg: Config, number: int, token: str) -> tuple[Path, str]:
    """A fresh worktree of origin/main for one issue, and its .git file to compare later."""
    worktree, branch = cfg.clone / ".worktrees" / f"issue-{number}", f"agent/issue-{number}"
    with GIT_LOCK:
        fetch_main(cfg.clone, cfg.repo, token)
        discard(cfg, worktree)
        git(cfg.clone, "worktree", "add", "--quiet", "-B", branch, str(worktree), "origin/main")
    for name in REFUSE:
        if (worktree / name).exists():
            raise Blocked(f"refused to run: the checkout contains {name}")
    if (worktree / "uv.lock").exists():
        sync = ["uv", "sync", "--locked", "--quiet"]
        subprocess.run(sync, cwd=worktree, check=True, capture_output=True, text=True)
    return worktree, (worktree / ".git").read_text()


def checkout(cfg: Config, worktree: Path, gitfile: str, api: Api, pull: Json) -> str:
    """Put the pull request's branch in a prepared worktree with origin/main merged in,
    conflicts left as markers; return the owner's comments as the prompt's review text."""
    ref = f"origin/{pull['head']['ref']}"
    refspec, url = f"+refs/heads/{pull['head']['ref']}:refs/remotes/{ref}", f"{cfg.repo}.git"
    with GIT_LOCK:
        git(worktree, "fetch", "--quiet", f"https://github.com/{url}", refspec, token=api.token)
    git(worktree, "reset", "--quiet", "--hard", ref)
    try:
        git(worktree, *identity(api.token, cfg.bot), "merge", "--no-commit", "origin/main")
    except subprocess.CalledProcessError:
        if not git(worktree, "ls-files", "--unmerged"):
            raise
    recheck(worktree, gitfile, {}, "origin/main")
    if changed := git(worktree, "diff", "--name-only", "origin/main", "--", *PINNED).split():
        raise Blocked(f"refused a rework: the branch changes {', '.join(changed)}")
    names = git(worktree, "diff", "--name-only", "--diff-filter=U", "-z").split("\0")[:-1]
    pull["unmerged"] = {n: (worktree / n).read_bytes() for n in names if (worktree / n).is_file()}
    pull["until"], notes = feedback(api, cfg, pull)
    text = "\n\n".join(notes).replace("</review>", "&lt;/review>")
    how = "`main` is merged in: search for `<<<<<<<` and resolve every conflict you find."
    how += f" Git left these files unmerged: {', '.join(names) or 'none'}."
    return f"\nThis reworks the open pull request. {how}\n\n<review>\n{text}\n</review>\n"


def render(template: str, values: dict[str, str]) -> str:
    """Fill {name} placeholders in one pass, so issue text can't add more of them."""
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m[1], m[0]), template)


def prompt_for(cfg: Config, issue: Json, review: str = "") -> str:
    """The task prompt from main's template, with the issue's text wrapped as data."""
    values = {"number": str(issue["number"]), "repo": cfg.repo, "review": review}
    for key in ("title", "body"):
        values[key] = (issue[key] or "").replace("</issue>", "&lt;/issue>")
    return render(show_main(cfg.root, "prompts/agent.md"), values)


def agent_settings(cfg: Config, worktree: Path) -> str:
    """Main's session settings with this task's paths, plus the repo's own agent commands."""
    text = show_main(cfg.root, ".claude/agent-settings.json")
    paths = {"$WORKTREE": worktree, "$GITDIR": cfg.clone / ".git", "$CLONE": cfg.clone}
    for name, path in paths.items():
        text = text.replace(name, str(path))
    settings: Json = json.loads(text)
    extra = config.table(show_main(cfg.clone, "pyproject.toml")).get("agent_commands", [])
    settings["permissions"]["allow"] += [f"Bash({command})" for command in extra]
    return json.dumps(settings)


def session(cmd: list[str], cfg: Config, worktree: Path) -> tuple[str, str, int]:
    """Run claude in its own process group, so Ctrl-C reaches only the dispatcher."""
    env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
    env |= {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "UV_NO_CONFIG": "1", "UV_NO_SYNC": "1"}
    env |= {"UV_OFFLINE": "1", "UV_CACHE_DIR": str(worktree / ".uv-cache")}
    with SESSIONS_LOCK:
        if CANCEL.is_set():
            raise Blocked("cancelled")
        proc = subprocess.Popen(
            cmd, cwd=worktree, env=env, text=True, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )  # fmt: skip
        SESSIONS.add(proc)
    try:
        stdout, stderr = proc.communicate(timeout=cfg.timeout_min * 60)
    except BaseException as exc:  # a timeout, or Ctrl-C while verify runs on the main thread
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        if isinstance(exc, subprocess.TimeoutExpired):
            raise Blocked(f"timed out after {cfg.timeout_min} minutes") from exc
        raise
    finally:
        with SESSIONS_LOCK:
            SESSIONS.discard(proc)
    if CANCEL.is_set():
        raise Blocked("cancelled")
    return stdout, stderr, proc.returncode


def run_agent(
    cfg: Config, worktree: Path, number: int, prompt: str, stream: bool = False, rules: str = ""
) -> Json:
    """Run claude -p in the worktree, boxed in; return its result message."""
    cmd = [
        "claude", "-p", prompt,
        "--restricted", "--tools", "Bash,Read,Edit,Write,Glob,Grep", "--strict-mcp-config",
        "--disable-slash-commands", "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--max-turns", str(cfg.max_turns), "--max-budget-usd", str(cfg.budget_usd),
        *(["--model", cfg.model] if cfg.model else []),
        "--json-schema", json.dumps(SCHEMA), "--no-session-persistence",
        "--settings", agent_settings(cfg, worktree),
        "--append-system-prompt", rules or show_main(cfg.root, "prompts/rules.md"),
        "--output-format", *(["stream-json", "--verbose"] if stream else ["json"]),
    ]  # fmt: skip
    stdout, stderr, code = session(cmd, cfg, worktree)
    logs = cfg.root / ".worktrees" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    name = f"{cfg.name}-issue-{number}-{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
    (logs / name).write_text(stdout or stderr)
    found: list[Json] = []
    for text in [stdout, *stdout.splitlines()]:  # one result, a list, or one message per line
        with contextlib.suppress(json.JSONDecodeError):
            item = json.loads(text)
            found += [
                m for m in (item if isinstance(item, list) else [item]) if isinstance(m, dict)
            ]
    result: Json | None = next((m for m in reversed(found) if m.get("type") == "result"), None)
    if result is None:
        raise Blocked(f"claude exited {code}: {stderr.strip()[-300:]}")
    init = next((m for m in found if m.get("subtype") == "init"), {})
    result |= {"init": init, "transcript": stdout}
    if result.get("is_error") or result.get("subtype") != "success":
        said = f"{result.get('subtype')}: {str(result.get('result') or '')[:300]}"
        raise Blocked(f"session ended with {said.removesuffix(': ')}", result)
    if not isinstance(result.get("structured_output"), dict):
        raise Blocked("session returned no pull request text", result)
    return result


def run_line(result: Json) -> str:
    cost, turns = result.get("total_cost_usd", 0) or 0, result.get("num_turns", 0)
    secs = round((result.get("duration_ms", 0) or 0) / 1000)
    return f"<!-- task-agent:run cost={cost:.2f} turns={turns} secs={secs} -->"


def summed(result: Json, old: str) -> Json:
    """The result with the totals from the last line of an old description added."""
    line = RUN_RE.fullmatch((old.splitlines() or [""])[-1])
    more = (float(line[1]), int(line[2]), int(line[3]) * 1000) if line else (0, 0, 0)
    keys = ("total_cost_usd", "num_turns", "duration_ms")
    return result | {key: (result.get(key) or 0) + n for key, n in zip(keys, more, strict=True)}


def pr_body(text: str, number: int, result: Json) -> str:
    """The description with its closing line first and the run's cost line last."""
    text = text.replace("\r\n", "\n")
    if f"#{number}" not in pr_format.CLOSING_LINE.findall(text):
        text = f"Closes #{number}\n\n{text}"
    return f"{text.rstrip()}\n\n{run_line(result)}\n"


def one_line(text: str) -> str:
    """A title: one line of at most 72 characters."""
    return " ".join(text.split())[:72].rstrip() or "Update"


def push(worktree: Path, token: str, repo: str, branch: str, force: bool = True) -> None:
    """Push HEAD to a branch at the repo's HTTPS URL, never to a named remote."""
    url, ref = f"https://github.com/{repo}.git", f"HEAD:refs/heads/{branch}"
    git(worktree, "push", "--quiet", *(["--force"] if force else []), url, ref, token=token)


def commit(
    cfg: Config, worktree: Path, api: Api, title: str, result: Json, left: dict[str, bytes]
) -> list[str]:
    """Commit the session's work as the App; return the changed paths, or raise Blocked."""
    # left holds each file git left unmerged, as it was before the session.
    for name in git(worktree, "diff", "--name-only", "--diff-filter=U", "-z").split("\0"):
        path = worktree / name
        if path.is_file() and re.search("^<<<<<<<", path.read_text(errors="replace"), re.M):
            raise Blocked(f"nothing was pushed: {name} still has a conflict marker", result)
        if path.is_file() and path.read_bytes() == left.get(name):
            raise Blocked(f"nothing was pushed: {name} is still unmerged", result)
    merging = worktree / git(worktree, "rev-parse", "--git-path", "MERGE_HEAD").strip()
    git(worktree, "add", "-A")
    if merging.exists() or git(worktree, "diff", "--cached", "--name-only").strip():
        # The message is the checked title: a squash merge may use it instead of the title.
        git(worktree, *identity(api.token, cfg.bot), "commit", "-q", "-m", title)
    if not git(worktree, "rev-list", "origin/main..HEAD").strip():
        text = result["structured_output"]["pr_body"]
        raise Blocked(f"the agent made no changes:\n\n{text}", result)
    if SECRET_RE.search(git(worktree, "diff", "origin/main...HEAD")):
        raise Blocked("the diff looks like it holds a secret", result)
    names = git(worktree, "diff", "--name-only", "--no-renames", "-z", "origin/main...HEAD")
    return [name for name in names.split("\0") if name]


def recheck(worktree: Path, gitfile: str, result: Json, rev: str = "HEAD") -> None:
    """Before a follow-up session: block if the .git file changed or a refused file appeared."""
    if (worktree / ".git").read_text() != gitfile:
        raise Blocked("the worktree's .git file was changed", result)
    made = ".venv" if git(worktree, "ls-tree", rev, "uv.lock") else ""
    for name in REFUSE:
        if name != made and (worktree / name).exists():
            raise Blocked(f"refused a follow-up: the checkout contains {name}", result)


def repair(cfg: Config, worktree: Path, number: int, text: str, problems: str, result: Json) -> str:
    """A short follow-up session rewrites a failing description; its cost is added to result."""
    body = pr_body(text, number, result)
    size = f"It has {pr_format.word_count(body)} words, not counting code blocks; aim for 200."
    if len(body) > pr_format.MAX_CHARS:
        size = "It is too long: shorten the code blocks."
    values, began = {"problems": problems, "size": size, "body": text}, time.monotonic()
    try:
        short = replace(cfg, max_turns=5, budget_usd=0.5, timeout_min=5)
        more = run_agent(short, worktree, number, render(REPAIR, values))
        text = more["structured_output"].get("pr_body") or text
    except Exception as exc:  # any failure repaired nothing; the work is still pushed
        if CANCEL.wait(1 if isinstance(exc, subprocess.SubprocessError) else 0):
            raise
        more = getattr(exc, "result", None) or {}
    more = {**more, "duration_ms": more.get("duration_ms") or (time.monotonic() - began) * 1000}
    for key in ("total_cost_usd", "num_turns", "duration_ms"):
        result[key] = (result.get(key) or 0) + (more.get(key) or 0)
    return text


def publish(
    app: App, cfg: Config, worktree: Path, gitfile: str, number: int, result: Json,
    pull: Json | None = None,
) -> str:  # fmt: skip
    """Commit, check, push and open or update the pull request; return what to print."""
    if (worktree / ".git").read_text() != gitfile:
        raise Blocked("the worktree's .git file was changed", result)
    api = app.token(cfg.repo, contents="write", pull_requests="write", issues="write")
    branch, out, pull = f"agent/issue-{number}", result["structured_output"], pull or {}
    title, text, mark = one_line(out["pr_title"]), out["pr_body"], REWORK.format(pull.get("until"))
    files = commit(cfg, worktree, api, title, result, pull.get("unmerged", {}))
    extra = config.protected(config.table(show_main(cfg.clone, "pyproject.toml")))
    notes, done = f"/issues/{pull.get('number')}/comments", "reworked"
    if pull and len(set(git(worktree, "rev-parse", "HEAD", f"origin/{branch}").split())) == 1:
        api("POST", notes, {"body": mark + "The rework session changed nothing."})
        set_status(api, number, "status:review")
        return "the rework changed nothing, back to status:review"
    for left in (2, 1, 0):
        body = pr_body(text, number, summed(result, pull.get("body") or ""))
        found = pr_format.check(title, body, branch, files, extra)
        problems = "\n".join(f"- {p}" for p in found)
        if not (left and found and all(p.startswith("the description") for p in found)):
            break
        recheck(worktree, gitfile, result)
        text = repair(cfg, worktree, number, text, problems, result)
    if any(pr_format.protected(path, extra) for path in files):
        raise Blocked(f"nothing was pushed:\n\n{problems}", result)
    if (worktree / ".git").read_text() != gitfile:
        raise Blocked("the worktree's .git file was changed", result)
    push(worktree, api.token, cfg.repo, branch, force=not pull)
    if problems:
        link = f"https://github.com/{cfg.repo}/tree/{branch}"
        raise Blocked(f"the work is on {link}, but its description fails:\n\n{problems}", result)
    if pull:
        api("PATCH", f"/pulls/{pull['number']}", {"title": title, "body": body})
        api("POST", notes, {"body": mark + "Reworked: see the new commit and description."})
    else:
        new, done = {"title": title, "head": branch, "base": "main", "body": body}, "opened"
        pull = api("POST", "/pulls", new)
    set_status(api, number, "status:review")
    return f"{done} {pull['html_url']}"


def failure(exc: BaseException) -> str:
    """The blocked reason: the message, plus the end of stderr or of GitHub's answer."""
    if isinstance(exc, Blocked):
        return str(exc)
    detail = getattr(exc, "stderr", None) or ""
    if isinstance(exc, urllib.error.HTTPError):
        detail = exc.read().decode(errors="replace")
    reason = f"{type(exc).__name__}: {exc}"
    return f"{reason}\n\n```\n{detail.strip()[-1000:]}\n```" if detail.strip() else reason


def block(api: Api, number: int, reason: str, result: Json) -> None:
    retry = "Fix the cause, then add `status:ready` again to retry."
    text = f"**Blocked:** {reason[:4000]}\n\n{run_line(result)}\n{retry}"
    api("POST", f"/issues/{number}/comments", {"body": SECRET_RE.sub("[removed]", text)})
    set_status(api, number, "status:blocked")


def work(app: App, cfg: Config, issue: Json) -> str:
    """Take one task from claim to pull request; return the line to print."""
    number = issue["number"]
    try:
        claim_id = claim(app.token(cfg.repo, issues="write"), number, cfg.bot)
    except Claimed as exc:
        return f"#{number}: claimed by another dispatcher until {exc}"
    worktree = cfg.clone / ".worktrees" / f"issue-{number}"
    result: Json = {}
    pull, review = None, ""
    try:
        api = app.token(cfg.repo, contents="read", pull_requests="read")
        pull = pull_for(api, cfg, number)
        worktree, gitfile = prepare(cfg, number, api.token)
        config.protected(config.table(show_main(cfg.clone, "pyproject.toml")))
        if pull:
            review = checkout(cfg, worktree, gitfile, api, pull)
        result = run_agent(cfg, worktree, number, prompt_for(cfg, issue, review))
        return f"#{number}: {publish(app, cfg, worktree, gitfile, number, result, pull)}"
    except Exception as exc:  # every failure goes back to the owner instead of retrying forever
        # Ctrl-C also stops git and uv, which can fail here before the main thread sets CANCEL.
        if CANCEL.wait(1 if isinstance(exc, subprocess.SubprocessError) else 0):
            back = "status:review" if pull else "status:ready"
            set_status(app.token(cfg.repo, issues="write"), number, back)
            return f"#{number}: cancelled, back to {back}"
        reason = failure(exc)
        result = getattr(exc, "result", None) or result
        block(app.token(cfg.repo, issues="write"), number, reason, result)
        return f"#{number}: blocked ({reason.splitlines()[0]})"
    finally:
        try:
            app.token(cfg.repo, issues="write")("DELETE", f"/issues/comments/{claim_id}")
        finally:
            with GIT_LOCK:
                discard(cfg, worktree)


def say(line: str) -> None:
    print(line, flush=True)


def waiting(api: Api, cfg: Config) -> str:
    """What to print when nothing is ready: the triage issues that only lack the label."""
    exists = readiness.path_checker(cfg.clone)
    passing = [
        str(issue["number"])
        for issue in labeled(api, "status:triage")
        if "human-authored" not in {lb["name"] for lb in issue["labels"]}
        and all(ok for ok, _ in readiness.check(readiness.parse_body(issue["body"]), exists))
    ]
    if not passing:
        return "nothing ready"
    command = f'make ready REPO={cfg.repo} ISSUES="{" ".join(passing)}"'
    return f"nothing ready. #{', #'.join(passing)} pass readiness and wait for: {command}"


def outcome(done: Future[str]) -> str:
    try:
        return done.result()
    except Exception as exc:  # work() failed while reporting a failure, such as no network
        return f"a task failed and could not be reported: {failure(exc).splitlines()[0]}"


def dispatch(app: App, cfg: Config, dry_run: bool = False, out: Out = say) -> None:
    """One pass over one repo: say what is taken and refused, then each result as it comes."""
    api = app.token(cfg.repo, issues="read", pull_requests="read")
    waits: list[str] = []
    found = [(task, blockers(app, api, task)) for task in select(api, cfg, waits)]
    waits += [f"#{t['number']}: waiting on {', '.join(held)}" for t, held in found if held]
    tasks = [task for task, held in found if not held] + reworks(api, cfg, waits.append)
    for task in tasks:
        out(f"#{task['number']}: {'would take' if dry_run else 'taking'} {task['title']!r}")
    for line in waits or ([] if tasks else [waiting(api, cfg)]):
        out(line)
    if dry_run or not tasks:
        return
    with ThreadPoolExecutor(cfg.parallel) as pool:
        pending = {pool.submit(work, app, cfg, task) for task in tasks}
        try:
            for done in as_completed(pending):
                pending.discard(done)
                out(outcome(done))
        except KeyboardInterrupt:  # Ctrl-C, or SIGTERM through the CLI's handler
            out("cancelling: stopping sessions and putting tasks back")
            cancel()
            pool.shutdown(cancel_futures=True)
            for left in pending:
                if not left.cancelled():
                    out(outcome(left))
            raise SystemExit(130) from None


def cancel() -> None:
    """Stop every running session and any about to start; work() then puts tasks back."""
    with SESSIONS_LOCK:
        CANCEL.set()
        for proc in SESSIONS:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)


def stale(app: App, base: Config) -> str:
    """Why this checkout can't run a pass, or "" when it is a clean, up-to-date main."""
    root = base.root
    fetch_main(root, base.repo, app.token(base.repo, contents="read").token)
    behind = git(root, "rev-parse", "HEAD") != git(root, "rev-parse", "origin/main")
    off_main = git(root, "branch", "--show-current").strip() != "main"
    if behind or off_main or git(root, "status", "--porcelain", "--untracked-files=no").strip():
        fix = "git switch main && git pull --ff-only"
        return f"refusing to run: {root} is not a clean, up-to-date main. Fix: {fix}"
    return ""


def run_pass(
    app: App, base: Config, targets: list[Config], dry_run: bool = False, out: Out = say,
    each: Callable[[Config, Out], None] | None = None,
) -> None:  # fmt: skip
    """One pass over each target repo in turn, under one lock, from an up-to-date main."""
    (base.root / ".worktrees").mkdir(exist_ok=True)
    with open(base.root / ".worktrees" / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return out("another pass is running")
        if reason := stale(app, base):
            return out(reason)
        for cfg in targets:

            def tagged(line: str, name: str = cfg.name) -> None:
                out(f"{name} {line}" if len(targets) > 1 else line)

            try:
                if each:
                    each(cfg, tagged)
                dispatch(app, cfg, dry_run, tagged)
            except Exception as exc:  # one repo's failure must not stop the others
                tagged(f"failed: {failure(exc)}")
