"""Checks on an App pull request. gate runs them from main's code, before the change's own."""

import re

ROOT_ONLY = {".github", "prompts", "makefile", "gnumakefile", "pyproject.toml", "uv.lock"}
ANY_DEPTH = {".claude", ".mcp.json", "claude.md", "claude.local.md"}
BRANCH = re.compile(r"agent/issue-(\d+)")
ISSUE = r"(?:[\w.-]+/[\w.-]+)?#\d+|GH-\d+|https?://github\.com/\S+/(?:issues|pull)/\d+"
CLOSING = re.compile(rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*({ISSUE})", re.I)
CLOSING_LINE = re.compile(r"^(?:Closes|Fixes|Resolves) (#\d+)[ \t]*$", re.M)
MAX_FILES = 3000
MAX_WORDS = 250
HEADINGS = ["Why", "What changed", "Evidence", "Not done", "After merge", "Lessons"]
FENCE = r"^[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+)?(```|~~~).*?(?:^[ \t]*\2|\Z)"
INLINE = r"(?<!`)(`+)(?!`)(?:[^\n]|\n(?!\n|[ \t]*<!--))+?(?<!`)\4(?!`)"
SKIPPED = re.compile(rf"({FENCE})|({INLINE})|<!--.*?-->", re.M | re.S)
SECTION = re.compile(r"^##[ \t]+(.*?)[ \t]*$(.*?)(?=^##[ \t]|\Z)", re.M | re.S)


def visible(body: str, fence: str) -> str:
    """A description with each fenced code block replaced by fence and HTML comments removed."""
    return SKIPPED.sub(lambda skipped: fence if skipped[1] else skipped[3] or " ", body)


def word_count(body: str) -> int:
    """The words in a description, not counting fenced code blocks and HTML comments."""
    return len(re.findall(r"\S*[^\W_]\S*", visible(body, " ")))


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
    sections = SECTION.findall(visible(body, "```"))
    if [name for name, _ in sections] != HEADINGS:
        wanted = ", ".join(f"`## {name}`" for name in HEADINGS)
        problems.append(f"the description needs exactly these headings, in this order: {wanted}")
    if empty := [name for name, text in sections if not text.strip()]:
        problems.append("the description has empty sections: " + ", ".join(empty))
    if (words := word_count(body)) > MAX_WORDS:
        problems.append(f"the description has {words} words; the limit is {MAX_WORDS}")
    return problems
