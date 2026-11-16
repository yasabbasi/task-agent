"""Tidy up after a merged pull request: lessons, labels and status."""

import json
import re
from pathlib import Path

from task_agent.gh import gh
from task_agent.pr_format import CLOSING_LINE, SECTION, visible

RULE = re.compile(r"^rule:[ \t]*(\S.*?)[ \t]*$", re.M)
TITLE_LENGTH = 72


def lessons(body: str) -> list[str]:
    """The `rule:` lines under the Lessons heading of a description."""
    sections = dict(SECTION.findall(visible(body, "```")))
    return RULE.findall(sections.get("Lessons", ""))


def file_lessons(repo: str, rules: list[str], url: str) -> None:
    """Open a triage issue for each rule whose title no open issue has."""
    listing = ("issue", "list", "--repo", repo, "--state", "open", "--limit", "1000")
    open_titles = set(gh(*listing, "--json", "title", "--jq", ".[].title").splitlines())
    for rule in rules:
        if (title := rule[:TITLE_LENGTH].strip()) in open_titles:
            continue
        create = ("issue", "create", "--repo", repo, "--title", title)
        gh(*create, "--label", "status:triage,lesson", "--body-file", "-", input=f"{rule}\n\n{url}")
        open_titles.add(title)


def clear_status(repo: str, number: str) -> None:
    """Remove every status: label from one issue."""
    view = ("issue", "view", number, "--repo", repo)
    names = gh(*view, "--json", "labels", "--jq", ".labels[].name")
    if status := sorted(name for name in names.splitlines() if name.startswith("status:")):
        gh("issue", "edit", number, "--repo", repo, "--remove-label", ",".join(status))


def run_event(event_path: str, bot: str) -> None:
    """Handle a pull_request closed event; one closed without merging changes nothing."""
    pr = (event := json.loads(Path(event_path).read_text()))["pull_request"]
    if not pr.get("merged"):
        return
    repo, body = event["repository"]["full_name"], (pr["body"] or "").replace("\r\n", "\n")
    if rules := lessons(body):
        file_lessons(repo, rules, pr["html_url"])
    if pr["user"]["login"] != bot:
        gh("pr", "edit", str(pr["number"]), "--repo", repo, "--add-label", "human-authored")
    for ref in dict.fromkeys(CLOSING_LINE.findall(body)):
        clear_status(repo, ref.removeprefix("#"))
