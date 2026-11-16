"""The loop's numbers: tasks done, how long they took and what they cost."""

import re
from datetime import datetime
from statistics import median
from typing import Any

from task_agent.app_token import Api, App
from task_agent.config import Config
from task_agent.pr_format import CLOSING_LINE, visible

Json = dict[str, Any]
COST = re.compile(r"<!-- task-agent:run cost=(\d+\.\d+) ")
HEAD = "Repo | Tasks | Ready to review | Open to merge | Median cost | Total cost | Human-authored"
SKIP = {"metrics", "human-authored"}


def names(item: Json) -> set[str]:
    return {label["name"] for label in item["labels"]}


def seconds(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def numbers(issues: list[Json], pulls: list[Json], events: dict[int, list[Json]], bot: str) -> Any:
    """One repo's [tasks, ready-to-review seconds, open-to-merge seconds, costs, human merges]."""
    merged = [pr for pr in pulls if pr.get("merged_at")]
    closer: dict[str, Json] = {}
    for pr in merged:
        refs = CLOSING_LINE.findall(visible((pr["body"] or "").replace("\r\n", "\n"), "```"))
        closer |= dict.fromkeys(refs if pr["user"]["login"] == bot else [], pr)
    waits, merges, costs = list[float](), list[float](), list[float]()
    for issue in issues:
        labels, pull = names(issue), closer.get(f"#{issue['number']}")
        typed = any(name.startswith("type:") for name in labels) and not labels & SKIP
        if not pull or not typed or issue["state"] != "closed" or "pull_request" in issue:
            continue
        merges.append(seconds(pull["created_at"], pull["merged_at"]))
        costs += [float(cost) for cost in COST.findall(pull["body"])[-1:]]
        added = [e for e in events.get(issue["number"], []) if e["event"] == "labeled"]
        stamps = [(e["label"]["name"], e["created_at"]) for e in added]
        ready = max((at for name, at in stamps if name == "status:ready"), default="")
        review = [at for name, at in stamps if name == "status:review" and ready and at >= ready]
        waits += [seconds(ready, min(review))] if review else []
    return [len(merges), waits, merges, costs, sum("human-authored" in names(pr) for pr in merged)]


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


def report(app: App, base: Config) -> str:
    """The Markdown table: a row for every configured repo and a Total row."""
    rows: dict[str, Any] = {}
    for repo in base.repos:
        api = app.token(repo, issues="read", pull_requests="read")
        issues = [i for i in pages(api, "/issues?state=closed&") if "pull_request" not in i]
        events = {i["number"]: pages(api, f"/issues/{i['number']}/events?") for i in issues}
        rows[repo] = numbers(issues, pages(api, "/pulls?state=closed&"), events, base.bot)
    rows["Total"] = [sum(column[1:], column[0]) for column in zip(*rows.values(), strict=True)]
    lines = [f"{row(name, *n[:4])} | {n[4]}" for name, n in rows.items()]
    return "\n".join(f"| {line} |" for line in [HEAD, " | ".join(["---"] * 7), *lines])
