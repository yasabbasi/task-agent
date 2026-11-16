import io
from typing import Any

import pytest
from conftest import BOT, SECTIONS, FakeApi, FakeApp

from task_agent import cli, config, dispatch, metrics

RUN = "<!-- task-agent:run cost={cost} turns=3 secs=61 -->"


def issue(number: int, *labels: str, state: str = "closed") -> dict[str, Any]:
    return {"number": number, "state": state, "labels": [{"name": name} for name in labels]}


def pull(closes: int, cost: str = "0.57", who: str = BOT, **extra: Any) -> dict[str, Any]:
    body = f"Closes #{closes}\n\n{SECTIONS}\n{RUN.format(cost=cost)}\n"
    times = {"created_at": "2026-01-01T13:00:00Z", "merged_at": "2026-01-01T13:42:30Z"}
    return {"user": {"login": who}, "body": body, "labels": []} | times | extra


def labeled(name: str, at: str) -> dict[str, Any]:
    return {"event": "labeled", "label": {"name": name}, "created_at": f"2026-01-01T{at}:00Z"}


def test_a_task_is_a_typed_issue_closed_by_a_merged_app_pull_request() -> None:
    issues = [issue(number, "type:feature") for number in range(1, 7)]
    issues[1]["labels"].append({"name": "metrics"})
    issues[2]["labels"].append({"name": "human-authored"})
    issues += [issue(7), issue(8, "type:bug", state="open")]
    pulls = [pull(number) for number in (1, 2, 3, 7, 8)]
    pulls += [pull(4, who="someone"), pull(5, merged_at=None)]
    pulls.append(pull(6, body=f"```\nCloses #6\n```\n<!--\nCloses #6\n-->\n{SECTIONS}"))
    assert metrics.numbers(issues, pulls, {}, BOT) == [1, [], [2550.0], [0.57], 0]


def test_ready_to_review_runs_from_the_last_ready_label_to_the_first_review_after_it() -> None:
    events = [labeled("status:ready", "09:00"), labeled("status:review", "09:30")]
    events += [labeled("status:ready", "10:00"), {"event": "closed", "created_at": "x"}]
    events += [labeled("status:review", "10:20"), labeled("status:review", "11:00")]
    waits = metrics.numbers([issue(1, "type:bug")], [pull(1)], {1: events}, BOT)[1]
    assert waits == [1200.0]


def test_a_task_never_labeled_for_review_after_ready_has_no_wait() -> None:
    events = [labeled("status:review", "09:30"), labeled("status:ready", "10:00")]
    assert metrics.numbers([issue(1, "type:bug")], [pull(1)], {1: events}, BOT)[1] == []


def test_cost_comes_from_the_last_run_line_and_may_be_missing() -> None:
    faked = pull(1, body=f"Closes #1\n\n{RUN.format(cost='9.00')}\n{RUN.format(cost='1.25')}\n")
    pulls = [faked, pull(2, body="Closes #2\n")]
    issues = [issue(1, "type:bug"), issue(2, "type:bug")]
    assert metrics.numbers(issues, pulls, {}, BOT)[3] == [1.25]


def test_human_authored_counts_merged_pull_requests_with_the_label() -> None:
    label = [{"name": "human-authored"}]
    pulls = [pull(1, who="owner", labels=label), pull(2, labels=label, merged_at=None), pull(3)]
    assert metrics.numbers([], pulls, {}, BOT) == [0, [], [], [], 1]


@pytest.mark.parametrize(
    ("samples", "text"),
    [([], "n/a"), ([2550.0], "42 min"), ([3599.0], "59 min"), ([3600.0, 21600.0], "3.5 h")],
)
def test_span_prints_minutes_under_an_hour_and_hours_otherwise(
    samples: list[float], text: str
) -> None:
    assert metrics.span(samples) == text


def test_row_prints_costs_in_dollars_and_n_a_without_data() -> None:
    assert metrics.row("o/r", 2, [], [60.0], [0.5, 0.64]) == "o/r | 2 | n/a | 1 min | $0.57 | $1.14"
    assert metrics.row("o/r", 0, [], [], []) == "o/r | 0 | n/a | n/a | n/a | n/a"


def test_pages_follows_pages_of_100_until_a_short_one() -> None:
    def page(path: str) -> list[dict[str, Any]]:
        return [{"number": 1}] * (100 if path.endswith("&page=1") else 3)

    api = FakeApi({"/issues?state=closed&per_page=100&page=": page})
    assert len(metrics.pages(api, "/issues?state=closed&")) == 103
    assert len(api.calls) == 2


def test_metrics_command_prints_a_row_per_repo_and_a_total(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def first(items: list[dict[str, Any]]) -> Any:
        return lambda path: items if path.endswith("&page=1") else []

    bot = "yasabbasi-task-agent[bot]"
    human = pull(9, who="owner", labels=[{"name": "human-authored"}])
    events = [labeled("status:ready", "12:00"), labeled("status:review", "12:10")]
    routes = {
        "/issues?": first([issue(1, "type:feature"), issue(2) | {"pull_request": {}}]),
        "/issues/1/events?": first(events),
        "/pulls?state=closed&": first([pull(1, who=bot), human]),
    }
    app = FakeApp(FakeApi(routes))
    text = (cli.ROOT / "pyproject.toml").read_text()
    monkeypatch.setattr(dispatch, "show_main", lambda root, path: text)
    monkeypatch.setattr("sys.stdin", io.StringIO("the key"))
    monkeypatch.setattr(cli, "App", lambda client_id, key: app)
    assert cli.main(["metrics", "--key-stdin"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"| {metrics.HEAD} |"
    assert lines[1] == "| --- | --- | --- | --- | --- | --- | --- |"
    assert lines[2] == "| yasabbasi/task-agent | 1 | 10 min | 42 min | $0.57 | $0.57 | 1 |"
    assert lines[-1] == "| Total | 3 | 10 min | 42 min | $0.57 | $1.71 | 3 |"
    assert len(lines) == 6
    read = {"issues": "read", "pull_requests": "read"}
    assert app.asked == [(repo, read) for repo in config.load(text).repos]
    assert not [call for call in app.api.calls if "/issues/2/" in call[1]]
