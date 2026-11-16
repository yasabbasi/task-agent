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
    """Records every gh call; answers the open rules and an issue's state and labels."""

    def __init__(
        self,
        filed: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
        state: str = "CLOSED",
        fail: str = "",
    ) -> None:
        self.calls: Calls = []
        self.filed, self.labels, self.state, self.fail = filed, labels, state, fail

    def __call__(self, *args: str, input: str | None = None) -> str:
        self.calls.append((args, input))
        if args[1] == self.fail:
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr=f"gh: {args[1]} failed\n")
        answers = {"list": self.filed, "view": (self.state, *self.labels)}
        return "".join(f"{line}\n" for line in answers.get(args[1], ()))

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
    event = {
        "pull_request": pr | {"base": {"ref": base}},
        "repository": {"full_name": "o/r", "default_branch": "main"},
    }
    path = folder / "event.json"
    path.write_text(json.dumps(event))
    return str(path)


def test_lessons_reads_rule_lines_only_under_the_lessons_heading() -> None:
    body = SECTIONS.replace("Because.", "rule: not a lesson").replace("None\n", "rule: a\nb\n")
    assert close_out.lessons(body) == ["a"]
    assert close_out.lessons(SECTIONS) == []


def test_lessons_skips_rule_lines_inside_code_fences() -> None:
    body = SECTIONS.replace("None\n", "```\nrule: quoted\n```\nrule: real\n")
    assert close_out.lessons(body) == ["real"]


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


def test_a_rule_an_open_issue_already_holds_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, filed=("use `uv run`", "something else"))
    close_out.run_event(write_event(tmp_path), BOT)
    assert [args[5] for args, _ in gh.made("issue", "create")] == [LONG[:72].strip()]


def test_rules_that_share_their_first_72_characters_are_both_filed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, filed=(f"{LONG}one",))
    body = SECTIONS.replace("None\n", f"rule: {LONG}one\nrule: {LONG}two\nrule: {LONG}three\n")
    close_out.run_event(write_event(tmp_path, body), BOT)
    assert [text for _, text in gh.made("issue", "create")] == [
        f"{LONG}two\n\n{URL}",
        f"{LONG}three\n\n{URL}",
    ]


def test_at_most_five_rules_are_filed_and_the_rest_are_counted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = fake(monkeypatch)
    rules = "".join(f"rule: number {n}\n" for n in range(8))
    assert close_out.run_event(write_event(tmp_path, SECTIONS.replace("None\n", rules)), BOT) == 0
    assert [args[5] for args, _ in gh.made("issue", "create")] == [f"number {n}" for n in range(5)]
    assert "3 rules were left out" in capsys.readouterr().out


def test_rules_and_closing_lines_in_hidden_text_are_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, labels=("status:review",))
    hidden = "a stray ` backtick\n<!--\nCloses #8\nrule: hidden `x`\n-->\n"
    fenced = "```\nCloses #9\n```\n<!--\nCloses #10\n-->\n"
    body = fenced + SECTIONS.replace("None\n", hidden + "rule: real\n")
    close_out.run_event(write_event(tmp_path, body), BOT)
    assert [args[5] for args, _ in gh.made("issue", "create")] == ["real"]
    assert not gh.made("issue", "view")


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


def test_an_issue_that_is_still_open_keeps_its_status_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, labels=("status:review",), state="OPEN")
    close_out.run_event(write_event(tmp_path), BOT)
    assert gh.made("issue", "view") and not gh.made("issue", "edit")


def test_a_merge_into_another_branch_clears_no_status_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, labels=("status:review",))
    close_out.run_event(write_event(tmp_path, by="yas", base="release"), BOT)
    assert not gh.made("issue", "view") and not gh.made("issue", "edit")
    assert gh.made("pr", "edit") and len(gh.made("issue", "create")) == 2


def test_labels_are_handled_before_lessons_are_filed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, labels=("status:review",))
    close_out.run_event(write_event(tmp_path, by="yas"), BOT)
    order = [args[:2] for args, _ in gh.calls]
    assert order[:4] == [("issue", "view"), ("issue", "edit"), ("pr", "edit"), ("issue", "list")]


@pytest.mark.parametrize("failing", ["view", "list", "create"])
def test_a_failed_gh_call_is_printed_and_the_rest_still_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failing: str,
) -> None:
    gh = fake(monkeypatch, labels=("status:review",), fail=failing)
    event = write_event(tmp_path, by="yas")
    assert cli.main(["close-out", "--event", event, "--bot", BOT]) == 1
    assert f"gh: {failing} failed" in capsys.readouterr().out
    assert gh.made("pr", "edit") and len(gh.made("issue", "create")) == (failing != "list") * 2


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
        stdout = ""

    def run(argv: list[str], **kwargs: object) -> Done:
        ran.append((argv, kwargs))
        return Done()

    monkeypatch.setattr("subprocess.run", run)
    body = SECTIONS.replace("None\n", "rule: $(x); y\n")
    close_out.run_event(write_event(tmp_path, body, by="yas"), BOT)
    assert ran and all(argv[0] == "gh" and not kwargs.get("shell") for argv, kwargs in ran)
    assert ran[-1][0][:3] == ["gh", "issue", "create"] and "$(x); y" in ran[-1][0]
