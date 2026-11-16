import json
from pathlib import Path

import pytest
from conftest import SECTIONS

from task_agent import cli, close_out, config

BOT = "task-agent-test[bot]"
URL = "https://github.com/o/r/pull/12"
LONG = "always run the whole check before you finish, " * 3
BODY = "Closes #7\n\n" + SECTIONS.replace("None\n", f"rule: use `uv run`\nrule: {LONG}\n")
Calls = list[tuple[tuple[str, ...], str | None]]


class FakeGh:
    """Records every gh call; answers the title listing and the label lookup."""

    def __init__(self, titles: tuple[str, ...] = (), labels: tuple[str, ...] = ()) -> None:
        self.calls: Calls = []
        self.titles, self.labels = titles, labels

    def __call__(self, *args: str, input: str | None = None) -> str:
        self.calls.append((args, input))
        answers = {"list": self.titles, "view": self.labels}
        return "".join(f"{line}\n" for line in answers.get(args[1], ()))

    def made(self, *start: str) -> Calls:
        return [(args, input) for args, input in self.calls if args[: len(start)] == start]


def fake(monkeypatch: pytest.MonkeyPatch, **answers: tuple[str, ...]) -> FakeGh:
    gh = FakeGh(**answers)
    monkeypatch.setattr(close_out, "gh", gh)
    return gh


def write_event(folder: Path, body: str | None = BODY, merged: bool = True, by: str = BOT) -> str:
    pr = {"number": 12, "merged": merged, "body": body, "html_url": URL, "user": {"login": by}}
    path = folder / "event.json"
    path.write_text(json.dumps({"pull_request": pr, "repository": {"full_name": "o/r"}}))
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


def test_a_rule_whose_title_matches_an_open_issue_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = fake(monkeypatch, titles=("use `uv run`", "something else"))
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
        stdout = ""

    def run(argv: list[str], **kwargs: object) -> Done:
        ran.append((argv, kwargs))
        return Done()

    monkeypatch.setattr("subprocess.run", run)
    close_out.run_event(write_event(tmp_path, body="rule: $(x); y", by="yas"), BOT)
    assert ran and all(argv[0] == "gh" and not kwargs.get("shell") for argv, kwargs in ran)
