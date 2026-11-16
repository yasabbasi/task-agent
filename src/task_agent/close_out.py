"""Tidy up after a merged pull request: lessons, labels and status."""

import json
import re
import subprocess
from pathlib import Path

from task_agent.gh import gh
from task_agent.pr_format import CLOSING_LINE, SECTION, visible

RULE = re.compile(r"^rule:[ \t]*(\S.*?)[ \t]*$", re.M)
TITLE_LENGTH, MAX_RULES = 72, 5


def attempt(*args: str, input: str | None = None) -> str | None:
    """Run gh; a failure prints gh's message and returns None."""
    try:
        return gh(*args, input=input)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr.strip())
        return None


def lessons(body: str) -> list[str]:
    """The distinct `rule:` lines under the Lessons heading of a description."""
    sections = dict(SECTION.findall(visible(body, "```")))
    return list(dict.fromkeys(RULE.findall(sections.get("Lessons", ""))))


def file_lessons(repo: str, rules: list[str], url: str) -> bool:
    """Open a triage issue for each rule no open issue starts with; False when gh failed."""
    listing = ("issue", "list", "--repo", repo, "--state", "open", "--limit", "1000")
    if (found := attempt(*listing, "--json", "body", "--jq", r'.[].body | split("\n")[0]')) is None:
        return False
    filed, ok = set(found.splitlines()), True
    for rule in rules:
        if rule not in filed:
            create = ("issue", "create", "--repo", repo, "--title", rule[:TITLE_LENGTH].strip())
            labels = ("--label", "status:triage,lesson", "--body-file", "-")
            ok &= attempt(*create, *labels, input=f"{rule}\n\n{url}") is not None
    return ok


def clear_status(repo: str, number: str) -> bool:
    """Remove every status: label from one issue if it is closed; False when gh failed."""
    view = ("issue", "view", number, "--repo", repo, "--json", "state,labels")
    if (found := attempt(*view, "--jq", ".state, .labels[].name")) is None:
        return False
    state, *names = found.splitlines() or [""]
    status = sorted(name for name in names if name.startswith("status:"))
    edit = ("issue", "edit", number, "--repo", repo, "--remove-label", ",".join(status))
    return state != "CLOSED" or not status or attempt(*edit) is not None


def run_event(event_path: str, bot: str) -> int:
    """Handle a pull_request closed event: 1 if a gh call failed; unmerged changes nothing."""
    pr = (event := json.loads(Path(event_path).read_text()))["pull_request"]
    if not pr.get("merged"):
        return 0
    repo, body = event["repository"]["full_name"], (pr["body"] or "").replace("\r\n", "\n")
    default = pr["base"]["ref"] == event["repository"]["default_branch"]
    refs = dict.fromkeys(CLOSING_LINE.findall(visible(body, " "))) if default else ()
    ok = [clear_status(repo, ref.removeprefix("#")) for ref in refs]
    if pr["user"]["login"] != bot:
        edit = ("pr", "edit", str(pr["number"]), "--repo", repo)
        ok.append(attempt(*edit, "--add-label", "human-authored") is not None)
    if len(rules := lessons(body)) > MAX_RULES:
        print(f"{len(rules) - MAX_RULES} rules were left out; at most {MAX_RULES} are filed")
    filed = not rules or file_lessons(repo, rules[:MAX_RULES], pr["html_url"])
    return int(not all([*ok, filed]))
