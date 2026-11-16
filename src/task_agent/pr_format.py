"""Checks on an App pull request. gate runs them from main's code, before the change's own."""

import re

ROOT_ONLY = {".github", "prompts", "makefile", "gnumakefile", "pyproject.toml", "uv.lock"}
ANY_DEPTH = {".claude", ".mcp.json", "claude.md", "claude.local.md"}
BRANCH = re.compile(r"agent/issue-(\d+)")
ISSUE = r"(?:[\w.-]+/[\w.-]+)?#\d+|GH-\d+|https?://github\.com/\S+/(?:issues|pull)/\d+"
CLOSING = re.compile(rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*({ISSUE})", re.I)
CLOSING_LINE = re.compile(r"^(?:Closes|Fixes|Resolves) (#\d+)[ \t]*$", re.M)
MAX_FILES = 3000


def protected(path: str) -> bool:
    """True for a path only a person may change, compared without case as macOS does."""
    parts = path.lower().split("/")
    return parts[0] in ROOT_ONLY or any(part in ANY_DEPTH for part in parts)


def check(title: str, body: str, branch: str, files: list[str]) -> list[str]:
    """Problems with an App pull request; an empty list means it passes."""
    problems, body = [], body.replace("\r\n", "\n")
    if changed := sorted(path for path in files if protected(path)):
        problems.append("agent pull requests may not change: " + ", ".join(changed))
    if not (match := BRANCH.fullmatch(branch)):
        return [*problems, f"the branch must be named agent/issue-N, not {branch}"]
    issue = f"#{match[1]}"
    if CLOSING_LINE.findall(body) != [issue]:
        problems.append(f"the description needs exactly one closing line, `Closes {issue}`")
    if others := sorted({ref for ref in CLOSING.findall(body) if ref != issue}):
        problems.append("the description may close only its own issue, not " + ", ".join(others))
    if CLOSING.search(title):
        problems.append("the title may not close an issue")
    return problems
