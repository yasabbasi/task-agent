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


def test_ruleset_requires_the_owner_and_the_gate_from_actions() -> None:
    ruleset = json.loads((ROOT / ".github" / "ruleset.json").read_text())
    rules = {rule["type"]: rule.get("parameters", {}) for rule in ruleset["rules"]}
    assert ruleset["name"] == "main" and ruleset["enforcement"] == "active"
    assert {actor["bypass_mode"] for actor in ruleset["bypass_actors"]} == {"pull_request"}
    assert {"deletion", "non_fast_forward"} <= set(rules)
    review = rules["pull_request"]
    assert review["require_code_owner_review"] and review["require_last_push_approval"]
    assert review["allowed_merge_methods"] == ["squash"]
    checks = rules["required_status_checks"]["required_status_checks"]
    assert checks == [{"context": "gate", "integration_id": 15368}]


def test_gate_checks_the_pull_request_before_it_runs_the_change() -> None:
    gate = (ROOT / ".github" / "workflows" / "gate.yml").read_text()
    order = ["default_branch", "task-agent pr-check", "check out the change", "make check"]
    assert sorted(order, key=gate.index) == order
    assert "${{" not in "".join(line for line in gate.splitlines() if "run:" in line)


def test_workflows_pin_actions_by_commit() -> None:
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        for used in re.findall(r"uses: (\S+)", workflow.read_text()):
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", used), used


def test_prompts_name_every_protected_path_and_the_refused_commands() -> None:
    rules = (ROOT / "prompts" / "rules.md").read_text()
    for name in (".github/", ".claude/", ".mcp.json", "prompts/", "Makefile", "pyproject.toml"):
        assert f"`{name}`" in rules
    assert "`uv.lock`" in rules and "`CLAUDE.md`" in rules
    assert "git command is refused" in rules and "make loc" in rules
    agent = (ROOT / "prompts" / "agent.md").read_text()
    assert set(re.findall(r"\{(\w+)\}", agent)) == {"number", "repo", "title", "body", "review"}
