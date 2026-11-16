"""Parse a task issue and check whether it is ready for an agent."""

import json
import re
from collections.abc import Callable
from pathlib import Path

from task_agent.gh import gh

MARKER = "<!-- task-agent:ready -->"
HEADINGS = {
    "What": "what",
    "Done when": "done",
    "Starting point": "start",
    "Depends on": "depends",
    "Type": "type",
    "Priority": "priority",
    "Effort": "effort",
}
CHOICES = {
    "type": ("feature", "bug", "docs", "chore"),
    "priority": ("p1", "p2", "p3"),
    "effort": ("s", "m", "l"),
}
Result = tuple[bool, str]
NAME = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
DEPENDS = re.compile(rf"(?<![\w./-])(?:https://github\.com/({NAME})/issues/|({NAME})?#)([0-9]+)")


def parse_body(body: str | None) -> dict[str, str]:
    """Split an issue-form body into its fields, keyed as in HEADINGS."""
    fields = dict.fromkeys(HEADINGS.values(), "")
    for chunk in re.split(r"^### ", body or "", flags=re.M)[1:]:
        heading, _, text = chunk.partition("\n")
        if key := HEADINGS.get(heading.strip()):
            fields[key] = "" if text.strip() == "_No response_" else text.strip()
    return fields


def path_checker(root: Path) -> Callable[[str], bool]:
    """Return exists(path), which only accepts paths inside root."""
    root = root.resolve()

    def exists(path: str) -> bool:
        target = (root / path).resolve()
        return target.is_relative_to(root) and target.exists()

    return exists


def depends_on(fields: dict[str, str], repo: str) -> list[tuple[str, int]]:
    """The (repo, number) of each issue named in Depends on, as `#N`, `owner/repo#N` or a URL."""
    found = DEPENDS.findall(fields["depends"])
    return list(dict.fromkeys((url or other or repo, int(n)) for url, other, n in found))


def check(fields: dict[str, str], exists: Callable[[str], bool]) -> list[Result]:
    """The readiness checks for one parsed issue, each with its verdict."""
    words = len(fields["what"].split())
    items = re.findall(r"^\s*- \[[ xX]\] +\S", fields["done"], flags=re.M)
    paths = re.findall(r"`([^`\n]+)`", fields["start"])
    missing = ", ".join(f"`{p}`" for p in paths if not exists(p))
    if not paths:
        start = "Starting point names at least one existing path in backticks"
    elif missing:
        start = f"Starting point paths exist (missing: {missing})"
    else:
        start = f"Starting point paths exist ({len(paths)} checked)"
    needs = ", ".join(f"{repo}#{number}" for repo, number in depends_on(fields, ""))
    waits = f"Depends on {needs}, and waits until each is closed as completed"
    if not needs:
        waits = "Depends on names an issue as `#N`, `owner/repo#N` or its URL"
    return [
        (3 <= words <= 80, f"What is stated in 3 to 80 words ({words})"),
        (bool(items), f"Done when has at least one checklist item ({len(items)})"),
        (bool(paths) and not missing, start),
        *([(bool(needs), waits)] * bool(fields["depends"])),
    ]


def labels_for(fields: dict[str, str]) -> set[str]:
    """The type, priority and effort labels chosen in the form."""
    chosen = {key: fields[key].strip().lower() for key in CHOICES}
    return {f"{key}:{value}" for key, value in chosen.items() if value in CHOICES[key]}


def comment(results: list[Result], note: str = "") -> str:
    """The readiness comment: one line per check, then what to do next."""
    lines = [MARKER, "**Readiness check**", ""]
    lines += [f"- {'pass' if ok else 'fail'}: {text}" for ok, text in results]
    lines += ["", note, ""] if note else [""]
    if all(ok for ok, _ in results):
        lines.append("Every check passes. The owner adds `status:ready` to hand it to an agent.")
    else:
        lines.append("Edit the issue to fix the failed checks. This comment updates on each edit.")
    return "\n".join(lines)


def upsert_comment(repo: str, number: int, body: str) -> None:
    """Update this check's earlier comment on the issue, or post the first one."""
    mine = f'select(.user.login == "github-actions[bot]" and (.body | startswith("{MARKER}")))'
    listing = ("api", "--paginate", f"repos/{repo}/issues/{number}/comments")
    ids = gh(*listing, "--jq", f".[] | {mine} | .id").split()
    if ids:
        gh("api", "-X", "PATCH", f"repos/{repo}/issues/comments/{ids[0]}", "-f", f"body={body}")
    else:
        gh("issue", "comment", str(number), "--repo", repo, "--body-file", "-", input=body)


def run_event(event_path: str, repo: str, root: Path) -> None:
    """Handle an issue opened or edited event inside GitHub Actions."""
    issue = (event := json.loads(Path(event_path).read_text()))["issue"]
    have = {label["name"] for label in issue["labels"]}
    if "metrics" in have:
        return
    fields = parse_body(issue["body"])
    add, remove, note = labels_for(fields) - have, set[str](), ""
    if event["action"] == "opened" and not any(name.startswith("status:") for name in have):
        add.add("status:triage")
    if event["action"] == "edited" and "status:ready" in have:
        add.add("status:triage")
        remove.add("status:ready")
        note = "The issue was edited after it was marked ready, so it is back in triage."
    edit = ["issue", "edit", str(issue["number"]), "--repo", repo]
    edit += ["--add-label", ",".join(sorted(add))] if add else []
    edit += ["--remove-label", ",".join(sorted(remove))] if remove else []
    if add or remove:
        gh(*edit)
    if "lesson" not in have:
        upsert_comment(repo, issue["number"], comment(check(fields, path_checker(root)), note))
