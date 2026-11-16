"""Tidy up after a merged pull request: status, labels and lessons."""

import json
import re
import subprocess
from pathlib import Path

from task_agent.gh import gh
from task_agent.pr_format import CLOSING_LINE, SECTION, visible

RULE = re.compile(r"^rule:[ \t]*(\S.*?)[ \t]*$", re.M)
TITLE_LENGTH = 72
MAX_LESSONS = 5


def tried(*args: str, input: str | None = None) -> str | None:
    """Run gh and return its output; a failure prints gh's message and returns None."""
    try:
        return gh(*args, input=input)
    except subprocess.CalledProcessError as exc:
        print(f"gh {' '.join(args[:2])} failed: {(exc.stderr or '').strip()}")
        return None


def lessons(text: str) -> list[str]:
    """The `rule:` lines under the Lessons heading of a description's visible text."""
    return RULE.findall(dict(SECTION.findall(text)).get("Lessons", ""))


def file_lessons(repo: str, rules: list[str], url: str) -> bool:
    """Open a triage issue for up to MAX_LESSONS rules no open issue has as its title."""
    listing = ("issue", "list", "--repo", repo, "--state", "open", "--limit", "1000")
    if (titles := tried(*listing, "--json", "title", "--jq", ".[].title")) is None:
        return False
    new = [rule for rule in dict.fromkeys(rules) if rule not in titles.split("\n")]
    if (left_out := len(new) - MAX_LESSONS) > 0:
        print(f"left out {left_out} lessons; at most {MAX_LESSONS} are filed")
    filed = []
    for rule in new[:MAX_LESSONS]:
        create = ("issue", "create", "--repo", repo, "--title", rule[:TITLE_LENGTH].strip())
        labels = ("--label", "status:triage,lesson", "--body-file", "-")
        filed.append(tried(*create, *labels, input=f"{rule}\n\n{url}"))
    return None not in filed


def clear_status(repo: str, number: str) -> bool:
    """Remove every status: label from one closed issue; False if a gh call failed."""
    view = ("issue", "view", number, "--repo", repo, "--json", "state,labels")
    if (found := tried(*view, "--jq", ".state, .labels[].name")) is None:
        return False
    state, *names = found.split("\n")
    if state != "CLOSED":
        print(f"#{number} is not closed, so its labels were kept")
        return True
    status = sorted(name for name in names if name.startswith("status:"))
    edit = ("issue", "edit", number, "--repo", repo, "--remove-label", ",".join(status))
    return not status or tried(*edit) is not None


def run_event(event_path: str, bot: str) -> int:
    """Handle a pull_request closed event; 1 if a gh call failed. Unmerged changes nothing."""
    pr = (event := json.loads(Path(event_path).read_text()))["pull_request"]
    if not pr.get("merged"):
        return 0
    repo, text = event["repository"]["full_name"], (pr["body"] or "").replace("\r\n", "\n")
    text, ok = visible(text, "```"), list[bool]()
    if pr["base"]["ref"] == event["repository"]["default_branch"]:
        refs = dict.fromkeys(CLOSING_LINE.findall(text))
        ok += [clear_status(repo, ref.removeprefix("#")) for ref in refs]
    if pr["user"]["login"] != bot:
        edit = ("pr", "edit", str(pr["number"]), "--repo", repo, "--add-label", "human-authored")
        ok.append(tried(*edit) is not None)
    if rules := lessons(text):
        ok.append(file_lessons(repo, rules, pr["html_url"]))
    return int(not all(ok))
