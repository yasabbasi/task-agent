import pytest
from conftest import SECTIONS

from task_agent import pr_format

BODY = f"Closes #7\n\n{SECTIONS}"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/gate.yml",
        ".github/CODEOWNERS",
        ".claude/settings.json",
        "docs/.claude/skills/x/SKILL.md",
        ".mcp.json",
        "tests/.mcp.json",
        "prompts/agent.md",
        "Makefile",
        "makefile",
        "GNUmakefile",
        "pyproject.toml",
        "uv.lock",
        "CLAUDE.md",
        "claude.md",
        "src/task_agent/CLAUDE.md",
        "CLAUDE.local.md",
        ".GitHub/workflows/x.yml",
    ],
)
def test_only_a_person_may_change_these_paths(path: str) -> None:
    assert pr_format.protected(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/task_agent/cli.py",
        "docs/prompts/x.md",
        "tests/pyproject.toml",
        "README.md",
        "docs/Makefile",
    ],
)
def test_agents_may_change_other_paths(path: str) -> None:
    assert not pr_format.protected(path)


def test_a_pull_request_that_closes_its_own_issue_passes() -> None:
    assert (
        pr_format.check("Add a file", BODY, "agent/issue-7", ["src/a.py", "tests/test_a.py"]) == []
    )
    assert (
        pr_format.check("Add a file", BODY + "This fixes #7 for good.", "agent/issue-7", []) == []
    )


def test_a_description_saved_on_github_with_crlf_line_endings_passes() -> None:
    assert pr_format.check("Add a file", BODY.replace("\n", "\r\n"), "agent/issue-7", []) == []


def test_protected_changes_are_named() -> None:
    problems = pr_format.check("Add", BODY, "agent/issue-7", ["src/a.py", "uv.lock", "CLAUDE.md"])
    assert problems == ["agent pull requests may not change: CLAUDE.md, uv.lock"]


@pytest.mark.parametrize(
    "body",
    [
        "## Why\n\nNo closing line.\n",
        "closes #7\n",
        "Closes #8\n",
        "Closes #7\nFixes #7\n",
        "Closes #7 and more\n",
        "Closes #70\n",
    ],
)
def test_the_description_needs_exactly_one_closing_line_for_its_issue(body: str) -> None:
    problems = pr_format.check("Add", body, "agent/issue-7", [])
    assert "the description needs exactly one closing line, `Closes #7`" in problems


@pytest.mark.parametrize(
    ("text", "ref"),
    [
        ("Also fixes #99.", "#99"),
        ("RESOLVES: #99", "#99"),
        ("closed owner/repo#3", "owner/repo#3"),
        ("fix https://github.com/owner/repo/issues/5", "https://github.com/owner/repo/issues/5"),
        ("Resolved GH-12", "GH-12"),
        ("fixes\n#99", "#99"),
    ],
)
def test_the_description_may_not_close_another_issue(text: str, ref: str) -> None:
    problems = pr_format.check("Add", BODY + text, "agent/issue-7", [])
    assert problems == [f"the description may close only its own issue, not {ref}"]


def test_prose_about_fixing_things_is_not_a_closing_reference() -> None:
    text = BODY + "This fixes the parser, closes the gap in #12's tests and prefixes #3."
    assert pr_format.check("Fix the parser", text, "agent/issue-7", []) == []


def test_the_title_may_not_close_an_issue() -> None:
    assert pr_format.check("Add a file, fixes #9", BODY, "agent/issue-7", []) == [
        "the title may not close an issue"
    ]


HEADINGS_PROBLEM = (
    "the description needs exactly these headings, in this order: `## Why`, `## What changed`, "
    "`## Evidence`, `## Not done`, `## After merge`, `## Lessons`"
)


@pytest.mark.parametrize(
    "body",
    [
        BODY.replace("## Not done\n\nNothing.\n\n", ""),
        BODY.replace("## Why\n\nBecause.\n\n", "") + "\n## Why\n\nBecause.\n",
        BODY + "\n## Notes\n\nMore.\n",
    ],
)
def test_the_description_needs_the_template_headings_in_order(body: str) -> None:
    assert pr_format.check("Add", body, "agent/issue-7", []) == [HEADINGS_PROBLEM]


def test_empty_sections_are_named() -> None:
    body = BODY.replace("Because.", "").replace("None", "<!-- Each rule: line is filed. -->")
    assert pr_format.check("Add", body, "agent/issue-7", []) == [
        "the description has empty sections: Why, Lessons"
    ]


def test_a_section_holding_only_a_code_block_is_filled() -> None:
    assert "## Evidence\n\n```\nmake check\n```\n\n## Not done" in BODY
    assert pr_format.check("Add", BODY, "agent/issue-7", []) == []


def test_a_heading_inside_a_code_block_is_not_a_section() -> None:
    body = BODY.replace("make check", "## Why\n\n## Output")
    assert pr_format.check("Add", body, "agent/issue-7", []) == []


def test_word_count_skips_code_blocks_and_comments() -> None:
    assert pr_format.word_count("one two\n\n```sh\nmake check\n```\n<!-- a\nnote -->three") == 3


def test_a_description_over_the_word_limit_states_its_count() -> None:
    spare = pr_format.MAX_WORDS - pr_format.word_count(BODY)
    assert pr_format.check("Add", BODY + "word " * spare, "agent/issue-7", []) == []
    assert pr_format.check("Add", BODY + "word " * (spare + 1), "agent/issue-7", []) == [
        "the description has 251 words; the limit is 250"
    ]


def test_the_branch_must_name_one_issue() -> None:
    for branch in ("main", "agent/issue-7-extra", "fix/agent/issue-7", "agent/issue-x"):
        assert pr_format.check("Add", BODY, branch, []) == [
            f"the branch must be named agent/issue-N, not {branch}"
        ]
