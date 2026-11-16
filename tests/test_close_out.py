import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import SECTIONS

from task_agent import cli, close_out, config

BOT = "task-agent-test[bot]"
URL = "https://github.com/o/r/pull/12"
LONG = "always run the whole check before you finish, " * 3
BODY = "Closes #7\n\n" + SECTIONS.replace("None\n", f"rule: use `uv run`\nrule: {LONG}\n")
Calls = list[tuple[tuple[str, ...], str | None]]


class FakeGh:
    """Records every gh call, remembers the issues it created and lists them with `bodies`."""

    def __init__(
        self,
        bodies: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
        state: str = "CLOSED",
        fails: tuple[str, ...] = (),
    ) -> None:
        self.calls: Calls = []
        self.issues = [{"title": "open", "body": body} for body in bodies]
        self.labels, self.state, self.fails = labels, state, fails

    def __call__(self, *args: str, input: str | None = None) -> str:
        self.calls.append((args, input))
        if (command := " ".join(args[:2])) in self.fails:
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr=f"no {command} today\n")
        if command == "issue create":
            self.issues.append({"title": args[5], "body": input or ""})
        if command == "issue list":
            return json.dumps(self.issues)
        return "".join(
            f"{line}\n" for line in (self.state, *self.labels) if command == "issue view"
        )

    def made(self, *start: str) -> Calls:
        return [(args, input) for args, input in self.calls if args[: len(start)] == start]


def fake(monkeypatch: pytest.MonkeyPatch, **answers: Any) -> FakeGh:
    gh = FakeGh(**answers)
    monkeypatch.setattr(close_out, "gh", gh)
    return gh


def write_event(
    folder: Path, body: str | None = BODY, merged: bool = True, by: str = BOT, base: str = "main"
) -> str:
    pr = {"number": 12, "merged": merged, "body": body, "html_url": URL, "user": {"login": by}}
    repo = {"full_name": "o/r", "default_branch": "main"}
    path = folder / "event.json"
    path.write_text(json.dumps({"pull_request": pr | {"base": {"ref": base}}, "repository": repo}))
    return str(path)


def titles_filed(gh: FakeGh) -> list[str]:
    return [args[5] for args, _ in gh.made("issue", "create")]


def test_lessons_reads_rule_lines_only_under_the_lessons_heading() -> None:
    body = SECTIONS.replace("Because.", "rule: not a lesson").replace("None\n", "rule: a\nb\n")
    assert close_out.lessons(body) == ["a"]
    assert close_out.lessons(SECTIONS) == []


def test_rule_lines_inside_code_fences_and_comments_are_not_filed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch)
    hidden = "```\nrule: quoted\n```\n<!--\nrule: unseen\n-->\nrule: real\n"
    close_out.run_event(write_event(tmp_path, SECTIONS.replace("None\n", hidden)), BOT)
    assert titles_filed(gh) == ["real"]


def test_closing_lines_inside_code_fences_and_comments_are_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, labels=("status:review",))
    body = "Closes #7\n\n<!--\nCloses #8\n-->\n\n```\nCloses #9\n```\n"
    close_out.run_event(write_event(tmp_path, body), BOT)
    assert [args[2] for args, _ in gh.made("issue", "view")] == ["7"]


def test_a_merge_into_another_branch_keeps_status_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, labels=("status:review",))
    close_out.run_event(write_event(tmp_path, "Closes #7\n", base="release"), BOT)
    assert gh.calls == []


def test_an_issue_that_is_not_closed_keeps_its_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = fake(monkeypatch, labels=("status:review",), state="OPEN")
    assert close_out.run_event(write_event(tmp_path, "Closes #7\n"), BOT) == 0
    assert not gh.made("issue", "edit")
    assert capsys.readouterr().out == "#7 is not closed, so its labels were kept\n"


def test_at_most_five_new_rules_are_filed_and_the_rest_counted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = fake(monkeypatch, bodies=("r0", f"r1\n\n{URL}"))
    body = SECTIONS.replace("None\n", "".join(f"rule: r{n}\n" for n in range(9)))
    close_out.run_event(write_event(tmp_path, body), BOT)
    assert titles_filed(gh) == ["r2", "r3", "r4", "r5", "r6"]
    assert capsys.readouterr().out == "left out 2 lessons; at most 5 are filed\n"


def test_rules_are_compared_with_the_first_paragraph_of_each_open_issue_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, bodies=(f"a\x0cb\r\n\r\n{URL}", f"{LONG}x\n\n{URL}", f"see\n\n{LONG}y"))
    body = SECTIONS.replace("None\n", f"rule: a\x0cb\nrule: a\nrule: {LONG}x\nrule: {LONG}y\n")
    close_out.run_event(write_event(tmp_path, body), BOT)
    assert [text for _, text in gh.made("issue", "create")] == [
        f"{rule}\n\n{URL}" for rule in ("a", f"{LONG}y")
    ]


def test_closing_out_the_same_merge_twice_files_each_rule_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch)
    rules = ["use `uv run`", f"{LONG}x", f"{LONG}y"]
    body = SECTIONS.replace("None\n", "".join(f"rule: {rule}\n" for rule in rules))
    for _ in range(2):
        assert close_out.run_event(write_event(tmp_path, body), BOT) == 0
    assert [text for _, text in gh.made("issue", "create")] == [f"{r}\n\n{URL}" for r in rules]
    assert titles_filed(gh)[1] == titles_filed(gh)[2] == LONG[:72].strip()


def test_status_and_the_author_label_are_handled_before_lessons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, labels=("status:review",))
    close_out.run_event(write_event(tmp_path, by="yas"), BOT)
    assert [args[:2] for args, _ in gh.calls] == [
        ("issue", "view"),
        ("issue", "edit"),
        ("pr", "edit"),
        ("issue", "list"),
        ("issue", "create"),
        ("issue", "create"),
    ]


@pytest.mark.parametrize(
    "failing", ["issue view", "issue edit", "pr edit", "issue list", "issue create"]
)
def test_a_failed_gh_call_is_printed_and_the_rest_still_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failing: str,
) -> None:
    gh = fake(monkeypatch, labels=("status:review",), fails=(failing,))
    event = write_event(tmp_path, "Closes #7\nCloses #9\n\n" + BODY, by="yas")
    assert cli.main(["close-out", "--event", event, "--bot", BOT]) == 1
    assert f"gh {failing} failed: no {failing} today" in capsys.readouterr().out
    assert len(gh.made("issue", "view")) == 2 and gh.made("pr", "edit") and gh.made("issue", "list")
    assert failing == "issue list" or len(gh.made("issue", "create")) == 2


def test_each_rule_becomes_a_triage_lesson_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch)
    close_out.run_event(write_event(tmp_path), BOT)
    (short, short_body), (long, long_body) = gh.made("issue", "create")
    assert short == ("issue", "create", "--repo", "o/r", "--title", "use `uv run`") + (
        "--label",
        "status:triage,lesson",
        "--body-file",
        "-",
    )
    assert short_body == f"use `uv run`\n\n{URL}"
    assert len(long[5]) <= 72 and LONG.startswith(long[5])
    assert long_body == f"{LONG.strip()}\n\n{URL}"


def test_a_rule_that_starts_an_open_issue_body_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, bodies=(f"use `uv run`\n\n{URL}", "something else"))
    close_out.run_event(write_event(tmp_path), BOT)
    assert [args[5] for args, _ in gh.made("issue", "create")] == [LONG[:72].strip()]


def test_the_same_rule_twice_opens_one_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch)
    body = SECTIONS.replace("None\n", "rule: same\nrule: same\n")
    close_out.run_event(write_event(tmp_path, body), BOT)
    assert len(gh.made("issue", "create")) == 1


def test_a_merge_by_someone_else_is_labeled_human_authored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch)
    close_out.run_event(write_event(tmp_path, body=None, by="yas"), BOT)
    assert gh.calls == [
        (("pr", "edit", "12", "--repo", "o/r", "--add-label", "human-authored"), None)
    ]


def test_a_merge_by_the_bot_is_not_labeled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gh = fake(monkeypatch)
    close_out.run_event(write_event(tmp_path), BOT)
    assert not gh.made("pr", "edit")


def test_status_labels_leave_each_closed_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, labels=("status:review", "type:bug", "status:claimed"))
    body = "Closes #7\r\nFixes #9\r\nCloses #7\r\n\r\nSee #3 and `Closes #4`."
    close_out.run_event(write_event(tmp_path, body), BOT)
    assert [args for args, _ in gh.made("issue", "edit")] == [
        ("issue", "edit", n, "--repo", "o/r", "--remove-label", "status:claimed,status:review")
        for n in ("7", "9")
    ]


def test_an_issue_without_a_status_label_is_not_edited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, labels=("type:bug",))
    close_out.run_event(write_event(tmp_path, SECTIONS.replace("Because.", "Closes #7")), BOT)
    assert gh.made("issue", "view") and not gh.made("issue", "edit")


def test_a_pull_request_closed_without_merging_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch)
    close_out.run_event(write_event(tmp_path, merged=False, by="yas"), BOT)
    assert gh.calls == []


def test_cli_takes_the_bot_from_the_flag_or_the_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch)
    event = write_event(tmp_path, body=None, by=config.load().bot)
    assert cli.main(["close-out", "--event", event]) == 0
    assert gh.calls == []
    assert cli.main(["close-out", "--event", event, "--bot", "other[bot]"]) == 0
    assert len(gh.made("pr", "edit")) == 1


def test_gh_is_called_without_a_shell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ran: list[tuple[list[str], dict[str, object]]] = []

    class Done:
        stdout = "[]"

    def run(argv: list[str], **kwargs: object) -> Done:
        ran.append((argv, kwargs))
        return Done()

    monkeypatch.setattr("subprocess.run", run)
    body = SECTIONS.replace("None\n", "rule: $(x); y\n")
    close_out.run_event(write_event(tmp_path, body, by="yas"), BOT)
    assert ran and all(argv[0] == "gh" and not kwargs.get("shell") for argv, kwargs in ran)
    (create,) = [argv for argv, _ in ran if argv[1:3] == ["issue", "create"]]
    assert create[5:7] == ["--title", "$(x); y"]
    assert ran[-1][1]["input"] == f"$(x); y\n\n{URL}"
