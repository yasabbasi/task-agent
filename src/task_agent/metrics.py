"""The loop's numbers: tasks done, how long they took and what they cost."""

import re
from base64 import b64decode, b64encode
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from statistics import median
from typing import Any

from task_agent.app_token import Api, App
from task_agent.config import Config
from task_agent.pr_format import CLOSING_LINE, visible

Json = dict[str, Any]
Out = Callable[[str], None]
COST = re.compile(r"<!-- task-agent:run cost=(\d+\.\d+) ")
HEAD = "Repo | Tasks | Ready to review | Open to merge | Median cost | Total cost | Human-authored"
SKIP = {"metrics", "human-authored"}
MARKS = ("<!-- metrics:start -->", "<!-- metrics:end -->")
TITLE = "Refresh the metrics"
BODY = (
    "Closes #{}\n\n## Why\n\nThe loop's numbers are kept in the README and reviewed like a change."
    "\n\n## What changed\n\nThe table under Key numbers.\n\n## Evidence\n\n`task-agent metrics`"
    " computed it.\n\n## Not done\n\nNothing.\n\n## After merge\n\nNothing.\n\n## Lessons\n\nNone\n"
)


def names(item: Json) -> set[str]:
    return {label["name"] for label in item["labels"]}


def seconds(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def numbers(issues: list[Json], pulls: list[Json], events: dict[int, list[Json]], bot: str) -> Any:
    """One repo's [tasks, ready-to-review seconds, open-to-merge seconds, costs, human merges]."""
    merged = [pr for pr in pulls if pr.get("merged_at")]
    tasks, done = dict[str, int](), set[str]()
    for issue in issues:
        typed = any(name.startswith("type:") for name in names(issue)) and not names(issue) & SKIP
        if typed and issue["state"] == "closed" and "pull_request" not in issue:
            tasks[f"#{issue['number']}"] = issue["number"]
    waits, merges, costs = list[float](), list[float](), list[float]()
    for pr in merged:
        body = (pr["body"] or "").replace("\r\n", "\n")
        closed = tasks.keys() & CLOSING_LINE.findall(visible(body, "```"))
        if pr["user"]["login"] != bot or not closed:
            continue
        done |= closed
        merges.append(seconds(pr["created_at"], pr["merged_at"]))
        costs += [float(cost) for cost in COST.findall(body)[-1:]]
    for number in sorted(tasks[ref] for ref in done):
        added = [e for e in events.get(number, []) if e["event"] == "labeled"]
        stamps = [(e["label"]["name"], e["created_at"]) for e in added]
        ready = max((at for name, at in stamps if name == "status:ready"), default="")
        review = [at for name, at in stamps if name == "status:review" and ready and at >= ready]
        waits += [seconds(ready, min(review))] if review else []
    return [len(done), waits, merges, costs, sum("human-authored" in names(pr) for pr in merged)]


def span(samples: list[float]) -> str:
    """The median of some seconds, as whole minutes under an hour and as hours otherwise."""
    secs = median(samples or [0])
    text = f"{int(secs // 60)} min" if secs < 3600 else f"{secs / 3600:.1f} h"
    return text if samples else "n/a"


def row(name: str, tasks: int, waits: list[float], merges: list[float], costs: list[float]) -> str:
    cost = [f"${median(costs):.2f}", f"${sum(costs):.2f}"] if costs else ["n/a", "n/a"]
    return " | ".join([name, str(tasks), span(waits), span(merges), *cost])


def pages(api: Api, path: str) -> list[Json]:
    """Every item of a listing, read in pages of 100."""
    found, batch = list[Json](), [Json()] * 100
    while len(batch) == 100:
        batch = api("GET", f"{path}per_page=100&page={len(found) // 100 + 1}")
        found += batch
    return found


def report(app: App, base: Config, out: Out = print) -> str:
    """The Markdown table: a row for every configured repo and a Total row."""
    rows: dict[str, Any] = {}
    for repo in base.repos:
        try:
            api = app.token(repo, issues="read", pull_requests="read")
            issues = [i for i in pages(api, "/issues?state=closed&") if "pull_request" not in i]
            events = {i["number"]: pages(api, f"/issues/{i['number']}/events?") for i in issues}
            rows[repo] = numbers(issues, pages(api, "/pulls?state=closed&"), events, base.bot)
        except Exception as exc:  # one repo's failure must not hide the others
            out(f"metrics: {repo} could not be read: {exc}")
            rows[repo] = []
    read = [n for n in rows.values() if n]
    rows["Total"] = [sum(column[1:], column[0]) for column in zip(*read, strict=True)]
    blank = " | ".join(["n/a"] * 6)
    lines = [f"{row(k, *n[:4])} | {n[4]}" if n else f"{k} | {blank}" for k, n in rows.items()]
    return "\n".join(f"| {line} |" for line in [HEAD, " | ".join(["---"] * 7), *lines])


def publish(app: App, base: Config) -> str:
    """Open a pull request from the App that puts the table in the README; returns what it did."""
    unread, number = list[str](), 0
    try:
        table = report(app, base, unread.append)
        api = app.token(base.repo, contents="write", pull_requests="write", issues="write")
        if unread or (left := api("GET", "/issues?state=open&labels=metrics")):
            why = unread[0] if unread else f"metrics: #{left[0]['number']} is still open"
            return f"{' '.join(why.split())}, so nothing was opened"
        sha = api("GET", "/git/ref/heads/main")["object"]["sha"]
        readme = api("GET", f"/contents/README.md?ref={sha}")
        head, _, rest = b64decode(readme["content"]).decode().partition(MARKS[0])
        old, end, tail = rest.partition(MARKS[1])
        if not end or old.strip() == table:
            why = "already has this table" if end else "has no metrics markers"
            return f"metrics: README.md on main {why}, so nothing was opened"
        number = api("POST", "/issues", {"title": TITLE, "labels": ["metrics"]})["number"]
        branch = f"agent/issue-{number}"
        api("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
        text = b64encode(f"{head}{MARKS[0]}\n{table}\n{end}{tail}".encode()).decode()
        commit = {"message": TITLE, "content": text, "sha": readme["sha"], "branch": branch}
        api("PUT", "/contents/README.md", commit)
        pull = {"title": TITLE, "body": BODY.format(number), "head": branch, "base": "main"}
        return f"metrics: opened {api('POST', '/pulls', pull)['html_url']}"
    except Exception as exc:  # the watcher's log gets one line, and nothing half-made is left
        with suppress(Exception):
            if number:
                api("PATCH", f"/issues/{number}", {"state": "closed"})
                api("DELETE", f"/git/refs/heads/agent/issue-{number}")
        return f"metrics: could not publish: {exc!r}"


def requested(app: App, base: Config, out: Out) -> None:
    """Publish when a request file asks for it, so the watcher's key can be used."""
    request = base.root / ".worktrees" / "requests" / "metrics"
    if request.exists():
        request.unlink()
        out(publish(app, base))
