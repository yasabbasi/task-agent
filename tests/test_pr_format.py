import time

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


def test_a_comment_start_in_inline_code_does_not_open_a_comment() -> None:
    body = BODY.replace("Because.", "Because `<!--` was read as a comment.") + "<!-- run -->"
    assert pr_format.check("Add", body, "agent/issue-7", []) == []
    assert pr_format.word_count("a `<!--` b\n\n## Why\n\nc <!-- run -->") == 4


@pytest.mark.parametrize(
    "block",
    [
        "~~~\n## Why\n\n## Output\n~~~",
        "- ```\n  ## Why\n\n  ## Output\n  ```",
        "1. ~~~sh\n## Why\n~~~",
        "~~~~\n## Why\n~~~\n## Output\n~~~~",
        "````\n## Why\n```\n## Output\n````",
    ],
)
def test_tilde_fences_and_fences_in_list_items_are_code_blocks(block: str) -> None:
    body = BODY.replace("```\nmake check\n```", block)
    assert pr_format.check("Add", body, "agent/issue-7", []) == []
    assert pr_format.word_count(body) == pr_format.word_count(BODY)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("```\nmake check", "Run ` this:\n```\nmake `check`"),
        ("Nothing.", "A stray ` here.\n<!-- a `b`\n\n## Lessons -->"),
    ],
)
def test_a_stray_backtick_above_a_fence_or_comment_leaves_the_headings_intact(
    old: str, new: str
) -> None:
    assert pr_format.check("Add", BODY.replace(old, new, 1), "agent/issue-7", []) == []


@pytest.mark.parametrize("start", ["", "- ", "1. "])
def test_a_line_that_starts_with_a_triple_backtick_span_is_not_a_fence(start: str) -> None:
    body = BODY.replace("Because.", f"{start}```make``` runs `ruff`.")
    assert pr_format.check("Add", body, "agent/issue-7", []) == []


@pytest.mark.parametrize("end", ["", " `"])
def test_unclosed_backtick_runs_above_a_long_line_of_backticks_are_read_quickly(end: str) -> None:
    runs = " ".join("`" * length for length in range(1, 256)) + "\n"
    body = runs + "`" * (pr_format.MAX_CHARS - len(runs) - len(end)) + end
    started = time.perf_counter()
    assert pr_format.check("Add", body, "agent/issue-7", [])
    assert time.perf_counter() - started < 1


def test_word_count_reads_a_long_run_of_punctuation_quickly() -> None:
    started = time.perf_counter()
    assert pr_format.word_count("-" * (pr_format.MAX_CHARS - 1) + "a") == 1
    assert time.perf_counter() - started < 1


def test_a_description_over_the_character_limit_gets_one_problem_and_is_not_parsed() -> None:
    body = "fixes #99 `" * 6000
    assert pr_format.check("Add, fixes #9", body, "agent/issue-7", ["uv.lock"]) == [
        "agent pull requests may not change: uv.lock",
        "the title may not close an issue",
        "the description has 66000 characters; the limit is 65536",
    ]
    assert pr_format.visible(body, "```") == ""
    assert pr_format.word_count(body) == 0


def test_an_unterminated_fence_hides_the_headings_after_it() -> None:
    body = BODY.replace("make check\n```", "make check")
    assert pr_format.check("Add", body, "agent/issue-7", []) == [HEADINGS_PROBLEM]


def test_word_count_counts_only_tokens_with_a_letter_or_digit() -> None:
    assert pr_format.word_count("## Why\n\n- one | 2 |\n- --- __ é") == 4


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
