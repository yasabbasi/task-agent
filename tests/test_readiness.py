import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from task_agent import cli, readiness

BODY = """### What

Add a --dry-run flag to dispatch so I can see what it would claim.

### Done when

- [ ] `make dispatch DRY_RUN=1` lists ready issues without claiming them
- [ ] a test covers it

### Starting point

`src/task_agent/cli.py`

### Depends on

_No response_

### Type

feature

### Priority

p2

### Effort

s
"""
Calls = list[tuple[tuple[str, ...], str | None]]


def always(_path: str) -> bool:
    return True


def test_parse_body_reads_the_form_fields() -> None:
    fields = readiness.parse_body(BODY)
    assert fields["what"].startswith("Add a --dry-run flag")
    assert fields["done"].count("- [ ]") == 2
    assert (fields["type"], fields["priority"], fields["effort"]) == ("feature", "p2", "s")


def test_parse_body_treats_no_response_and_no_body_as_empty() -> None:
    assert readiness.parse_body(BODY)["depends"] == ""
    assert readiness.parse_body(None)["what"] == ""


def test_parse_body_keeps_the_depends_on_text() -> None:
    body = BODY.replace("_No response_", "#12\nowner/repo#3")
    assert readiness.parse_body(body)["depends"] == "#12\nowner/repo#3"


def test_depends_on_reads_issues_in_this_repo_and_in_others() -> None:
    fields = readiness.parse_body(BODY.replace("_No response_", "#12, owner/other#3\n- #12"))
    assert readiness.depends_on(fields, "o/r") == [("o/r", 12), ("owner/other", 3)]
    assert readiness.depends_on(readiness.parse_body(BODY), "o/r") == []


def test_the_comment_gains_a_pass_line_naming_the_dependencies() -> None:
    body = BODY.replace("_No response_", "#12\nowner/other#3")
    results = readiness.check(readiness.parse_body(body), always)
    assert "- pass: Depends on #12, owner/other#3, and waits" in readiness.comment(results)
    assert len(readiness.check(readiness.parse_body(BODY), always)) == len(results) - 1


def test_a_complete_issue_passes_every_check() -> None:
    assert all(ok for ok, _ in readiness.check(readiness.parse_body(BODY), always))


@pytest.mark.parametrize(
    ("edit", "failing"),
    [
        (lambda b: b.replace(readiness.parse_body(BODY)["what"], "Fix it"), 0),
        (lambda b: b.replace("- [ ]", "-"), 1),
        (lambda b: b.replace("`src/task_agent/cli.py`", "the cli"), 2),
    ],
)
def test_each_check_can_fail_alone(edit: Callable[[str], str], failing: int) -> None:
    results = readiness.check(readiness.parse_body(edit(BODY)), always)
    assert [i for i, (ok, _) in enumerate(results) if not ok] == [failing]


def test_missing_starting_point_paths_are_named() -> None:
    ok, text = readiness.check(readiness.parse_body(BODY), lambda path: False)[2]
    assert not ok
    assert "`src/task_agent/cli.py`" in text


def test_path_checker_stays_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("")
    (tmp_path / "outside.py").write_text("")
    exists = readiness.path_checker(root)
    assert exists("a.py")
    assert not exists("b.py")
    assert not exists("../outside.py")
    assert not exists("/etc/hosts")


def test_labels_for_ignores_values_outside_the_form() -> None:
    fields = readiness.parse_body(BODY)
    assert readiness.labels_for(fields) == {"type:feature", "priority:p2", "effort:s"}
    assert readiness.labels_for(readiness.parse_body("### Type\n\nmisc\n")) == set()


def test_comment_reads_pass_or_fail_without_emoji() -> None:
    passed = readiness.comment([(True, "a")])
    failed = readiness.comment([(True, "a"), (False, "b")], note="moved back")
    assert passed.startswith(readiness.MARKER)
    assert "- pass: a" in passed and "`status:ready`" in passed
    assert "- fail: b" in failed and "moved back" in failed
    assert (passed + failed).isascii()


def write_event(folder: Path, action: str, labels: list[str]) -> str:
    issue = {"number": 7, "body": BODY, "labels": [{"name": name} for name in labels]}
    path = folder / "event.json"
    path.write_text(json.dumps({"action": action, "issue": issue}))
    return str(path)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    made: Calls = []

    def gh(*args: str, input: str | None = None) -> str:
        made.append((args, input))
        return "555\n" if "--jq" in args else ""

    monkeypatch.setattr(readiness, "gh", gh)
    return made


def test_opened_issue_gets_its_labels_and_an_updated_comment(tmp_path: Path, calls: Calls) -> None:
    readiness.run_event(write_event(tmp_path, "opened", []), "o/r", tmp_path)
    edit = next(args for args, _ in calls if args[:2] == ("issue", "edit"))
    assert "effort:s,priority:p2,status:triage,type:feature" in edit
    patch = next(args for args, _ in calls if args[:3] == ("api", "-X", "PATCH"))
    assert patch[3] == "repos/o/r/issues/comments/555"


def test_an_edit_after_ready_sends_the_issue_back_to_triage(tmp_path: Path, calls: Calls) -> None:
    labels = ["status:ready", "type:feature", "priority:p2", "effort:s"]
    readiness.run_event(write_event(tmp_path, "edited", labels), "o/r", tmp_path)
    edit = next(args for args, _ in calls if args[:2] == ("issue", "edit"))
    assert edit[-4:] == ("--add-label", "status:triage", "--remove-label", "status:ready")
    assert "back in triage" in calls[-1][0][-1]


def test_the_first_comment_is_posted_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    made: Calls = []

    def gh(*args: str, input: str | None = None) -> str:
        made.append((args, input))
        return ""

    monkeypatch.setattr(readiness, "gh", gh)
    readiness.upsert_comment("o/r", 7, "hello")
    assert made[-1] == (("issue", "comment", "7", "--repo", "o/r", "--body-file", "-"), "hello")


def test_cli_ready_checks_a_body_on_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(BODY))
    assert cli.main(["ready"]) == 0
    assert "- pass: Starting point paths exist (1 checked)" in capsys.readouterr().out


def test_cli_ready_fails_when_a_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(BODY.replace("src/task_agent", "src/missing")))
    assert cli.main(["ready"]) == 1
