import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def make(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["make", "-s", *args], cwd=ROOT, capture_output=True, text=True)


def python_files(folder: Path, lines: int) -> str:
    (folder / "a.py").write_text("x = 1\n" * lines)
    (folder / "b.py").write_text("y = 2\n" * 2)
    return f"LOC_FILES={folder}/*.py"


def test_loc_passes_at_the_cap(tmp_path: Path) -> None:
    result = make("loc", python_files(tmp_path, 3), "LOC_MAX=5")
    assert result.returncode == 0
    assert result.stdout.startswith("5 lines")


def test_loc_fails_over_the_cap(tmp_path: Path) -> None:
    result = make("loc", python_files(tmp_path, 4), "LOC_MAX=5")
    assert result.returncode != 0
    assert result.stdout.startswith("6 lines")


def test_loc_passes_on_this_repo() -> None:
    assert make("loc").returncode == 0


def test_check_runs_lint_test_and_loc() -> None:
    plan = subprocess.run(["make", "-n", "check"], cwd=ROOT, capture_output=True, text=True).stdout
    assert "ruff check" in plan and "pytest" in plan and "wc -l" in plan


def test_help_lists_the_shared_targets() -> None:
    listed = {line.split()[0] for line in make("help").stdout.splitlines()}
    assert {"help", "install", "check", "lint", "test", "format", "loc"} <= listed


def test_labels_are_unique_with_colors_and_descriptions() -> None:
    labels = json.loads((ROOT / ".github" / "labels.json").read_text())
    names = [label["name"] for label in labels]
    assert len(names) == len(set(names))
    assert {"status:triage", "status:ready", "human-authored", "lesson", "metrics"} <= set(names)
    for label in labels:
        assert re.fullmatch(r"[0-9a-f]{6}", label["color"])
        assert label["description"]


def test_claude_md_is_at_most_40_lines() -> None:
    assert len((ROOT / "CLAUDE.md").read_text().splitlines()) <= 40


def test_readme_only_names_make_targets_that_exist() -> None:
    targets = set(re.findall(r"^([a-z-]+):", (ROOT / "Makefile").read_text(), flags=re.M))
    named = set(re.findall(r"`make ([a-z-]+)", (ROOT / "README.md").read_text()))
    assert named and named <= targets


def test_task_skill_and_form_use_the_headings_the_parser_reads() -> None:
    from task_agent import readiness

    skill = (ROOT / ".claude" / "skills" / "task" / "SKILL.md").read_text()
    form = (ROOT / ".github" / "ISSUE_TEMPLATE" / "task.yml").read_text()
    assert re.match(r"---\nname: task\ndescription: \S.*\n---\n", skill)
    assert re.findall(r"^   ### (.+)$", skill, flags=re.M) == list(readiness.HEADINGS)
    assert re.findall(r"^      label: (.+)$", form, flags=re.M) == list(readiness.HEADINGS)
