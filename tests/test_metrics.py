import io
from base64 import b64decode, encodebytes
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import BOT, CFG, SECTIONS, FakeApi, FakeApp

from task_agent import cli, config, dispatch, metrics, pr_format, verify

RUN = "<!-- task-agent:run cost={cost} turns=3 secs=61 -->"
BASE = replace(CFG, repos={"owner/repo": "."})
START, END = "<!-- metrics:start -->", "<!-- metrics:end -->"
TABLE = (
    f"| {metrics.HEAD} |\n| --- | --- | --- | --- | --- | --- | --- |\n"
    "| owner/repo | 0 | n/a | n/a | n/a | n/a | 0 |\n| Total | 0 | n/a | n/a | n/a | n/a | 0 |"
)


class Hub(FakeApi):
    """A repo whose listings are empty and whose README on main holds the text given."""

    def __init__(self, readme: str, fails: str = "") -> None:
        super().__init__()
        self.readme, self.fails, self.branch = readme, fails, False
        self.open = list[dict[str, Any]]()

    def __call__(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if path == "/git/ref/heads/main":
            return {"object": {"sha": "abc"}}
        if path == "/contents/README.md?ref=abc":
            return {"content": encodebytes(self.readme.encode()).decode(), "sha": "blob"}
        if path == "/issues?state=open&labels=metrics":
            return self.open
        if (method != "GET" and path == self.fails) or (method == "DELETE" and not self.branch):
            raise RuntimeError("boom\nat line 2")
        self.branch = self.branch or path == "/git/refs"
        return [] if method == "GET" else {"number": 7, "html_url": "https://example/pull/8"}


class Flaky(FakeApp):
    """An App that is not installed on owner/down."""

    def token(self, repo: str, **permissions: str) -> FakeApi:
        if repo == "owner/down":
            raise RuntimeError("no installation")
        return super().token(repo, **permissions)


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
    [
        ([], "n/a"),
        ([2550.0], "42 min"),
        ([3599.0], "59 min"),
        ([3600.0], "1.0 h"),
        ([3600.0, 21600.0], "3.5 h"),
    ],
)
def test_span_prints_minutes_under_an_hour_and_hours_otherwise(
    samples: list[float], text: str
) -> None:
    assert metrics.span(samples) == text


def test_row_prints_costs_in_dollars_and_n_a_without_data() -> None:
    assert metrics.row("o/r", 2, [], [60.0], [0.5, 0.64]) == "o/r | 2 | n/a | 1 min | $0.57 | $1.14"
    assert metrics.row("o/r", 0, [], [], []) == "o/r | 0 | n/a | n/a | n/a | n/a"


def test_row_prints_the_median_not_the_mean() -> None:
    text = metrics.row("o/r", 3, [60.0, 120.0, 3000.0], [60.0, 7200.0, 86400.0], [0.1, 0.2, 0.9])
    assert text == "o/r | 3 | 2 min | 2.0 h | $0.20 | $1.20"


def test_a_pull_request_is_one_sample_and_an_issue_is_one_task() -> None:
    issues = [issue(number, "type:bug") for number in (1, 2, 3)]
    both = pull(1, body=f"Closes #1\nCloses #2\n\n{RUN.format(cost='0.10')}\n")
    again = pull(3, cost="0.90", merged_at="2026-01-01T15:00:00Z")
    found = metrics.numbers(issues, [both, pull(3, cost="0.20"), again], {}, BOT)
    assert found == [3, [], [2550.0, 2550.0, 7200.0], [0.1, 0.2, 0.9], 0]


def test_an_unlabeled_event_between_the_labels_does_not_break_the_wait() -> None:
    gone = labeled("status:ready", "09:05") | {"event": "unlabeled"}
    events = [labeled("status:ready", "09:00"), gone, labeled("status:claimed", "09:05")]
    events.append(labeled("status:review", "09:30"))
    assert metrics.numbers([issue(1, "type:bug")], [pull(1)], {1: events}, BOT)[1] == [1800.0]


def test_a_description_may_use_windows_line_ends_or_be_null() -> None:
    windows = pull(1, body=f"Closes #1\r\n\r\n{RUN.format(cost='0.57')}\r\n")
    issues = [issue(1, "type:bug"), issue(2, "type:bug")]
    found = metrics.numbers(issues, [windows, pull(2, body=None)], {}, BOT)
    assert found == [1, [], [2550.0], [0.57], 0]


def test_pages_follows_pages_of_100_until_a_short_one() -> None:
    def page(path: str) -> list[dict[str, Any]]:
        return [{"number": 1}] * (3 if path.endswith("&page=3") else 100)

    api = FakeApi({"/issues?state=closed&per_page=100&page=": page})
    assert len(metrics.pages(api, "/issues?state=closed&")) == 203
    assert [call[1][-7:] for call in api.calls] == ["&page=1", "&page=2", "&page=3"]


def test_a_repo_whose_calls_fail_gets_a_row_of_n_a_and_a_line() -> None:
    def first(items: list[dict[str, Any]]) -> Any:
        return lambda path: items if path.endswith("&page=1") else []

    routes = {"/issues?": first([issue(1, "type:bug")]), "/pulls?": first([pull(1)])}
    base = replace(BASE, repos={"owner/down": ".", "owner/repo": "."})
    said = list[str]()
    lines = metrics.report(Flaky(FakeApi(routes)), base, said.append).splitlines()
    assert lines[2:] == [
        "| owner/down | n/a | n/a | n/a | n/a | n/a | n/a |",
        "| owner/repo | 1 | n/a | 42 min | $0.57 | $0.57 | 0 |",
        "| Total | 1 | n/a | 42 min | $0.57 | $0.57 | 0 |",
    ]
    assert said == ["metrics: owner/down could not be read: no installation"]


def test_publish_opens_an_issue_a_branch_a_commit_and_a_pull_request() -> None:
    api = Hub(f"# Title\n\n{START}\nNone yet.\n{END}\n\nMore.\n")
    app = FakeApp(api)
    assert metrics.publish(app, BASE) == "metrics: opened https://example/pull/8"
    wanted = {"contents": "write", "pull_requests": "write", "issues": "write"}
    assert app.asked[-1] == ("owner/repo", wanted)
    writes = [call for call in api.calls if call[0] != "GET"]
    assert [call[:2] for call in writes] == [
        ("POST", "/issues"),
        ("POST", "/git/refs"),
        ("PUT", "/contents/README.md"),
        ("POST", "/pulls"),
    ]
    opened, ref, commit, pulled = (call[2] for call in writes)
    assert opened == {"title": "Refresh the metrics", "labels": ["metrics"]}
    assert ref == {"ref": "refs/heads/agent/issue-7", "sha": "abc"}
    readme = b64decode(commit.pop("content")).decode()
    assert readme == f"# Title\n\n{START}\n{TABLE}\n{END}\n\nMore.\n"
    assert commit == {"message": "Refresh the metrics", "sha": "blob", "branch": "agent/issue-7"}
    assert (pulled["title"], pulled["head"], pulled["base"]) == (
        commit["message"],
        "agent/issue-7",
        "main",
    )
    assert pr_format.check(pulled["title"], pulled["body"], pulled["head"], ["README.md"]) == []


@pytest.mark.parametrize(
    ("readme", "why"),
    [
        (f"{START}\n{TABLE}\n{END}\n", "already has this table"),
        (f"{START}\nNone yet.\n", "has no metrics markers"),
        (f"None yet.\n{END}\n", "has no metrics markers"),
    ],
)
def test_publish_opens_nothing_for_the_same_table_or_a_missing_marker(
    readme: str, why: str
) -> None:
    api = Hub(readme)
    line = metrics.publish(FakeApp(api), BASE)
    assert line == f"metrics: README.md on main {why}, so nothing was opened"
    assert not [call for call in api.calls if call[0] != "GET"]


def test_two_pull_requests_that_close_one_issue_are_one_task() -> None:
    found = metrics.numbers([issue(1, "type:bug")], [pull(1), pull(1, cost="0.90")], {}, BOT)
    assert found[0] == 1


def test_the_write_token_is_asked_only_for_the_base_repo() -> None:
    app = FakeApp(Hub(f"{START}\nNone yet.\n{END}\n"))
    metrics.publish(app, replace(BASE, repos={"owner/repo": ".", "owner/other": "."}))
    assert [repo for repo, asked in app.asked if "write" in asked.values()] == ["owner/repo"]


def test_publish_opens_nothing_when_a_repo_could_not_be_read() -> None:
    api = Hub(f"{START}\nNone yet.\n{END}\n")
    base = replace(BASE, repos={"owner/down": ".", "owner/repo": "."})
    line = metrics.publish(Flaky(api), base)
    assert line == "metrics: owner/down could not be read: no installation, so nothing was opened"
    assert not [call for call in api.calls if call[0] != "GET"]


def test_publish_opens_nothing_while_a_metrics_issue_is_open() -> None:
    api = Hub(f"{START}\nNone yet.\n{END}\n")
    api.open = [issue(5, "metrics", state="open")]
    line = metrics.publish(FakeApp(api), BASE)
    assert line == "metrics: #5 is still open, so nothing was opened"
    assert not [call for call in api.calls if call[0] != "GET"]


@pytest.mark.parametrize("fails", ["/git/refs", "/contents/README.md", "/pulls"])
def test_a_failed_call_closes_the_issue_and_deletes_the_branch(fails: str) -> None:
    api = Hub(f"{START}\nNone yet.\n{END}\n", fails)
    line = metrics.publish(FakeApp(api), BASE)
    assert line.startswith("metrics: could not publish") and "\n" not in line
    assert ("PATCH", "/issues/7", {"state": "closed"}) in api.calls
    deleted = ("DELETE", "/git/refs/heads/agent/issue-7", None) in api.calls
    assert deleted or fails == "/git/refs"


def test_a_call_that_fails_before_the_issue_exists_closes_nothing() -> None:
    api = Hub(f"{START}\nNone yet.\n{END}\n", "/issues")
    assert metrics.publish(FakeApp(api), BASE).startswith("metrics: could not publish")
    assert [call[0] for call in api.calls if call[0] != "GET"] == ["POST"]


def test_the_readme_has_both_markers_under_key_numbers() -> None:
    section = (cli.ROOT / "README.md").read_text().split("\n## ")[1]
    assert section.startswith("Key numbers\n")
    assert section.index(START) < section.index(END)


def test_the_watcher_publishes_once_when_a_request_file_asks(tmp_path: Path) -> None:
    base, lines = replace(BASE, root=tmp_path), list[str]()
    api = Hub(f"{START}\n{END}\n")
    metrics.requested(FakeApp(api), base, lines.append)
    assert lines == [] and api.calls == []
    request = tmp_path / ".worktrees" / "requests" / "metrics"
    request.parent.mkdir(parents=True)
    request.write_text("")
    metrics.requested(FakeApp(api), base, lines.append)
    metrics.requested(FakeApp(api), base, lines.append)
    assert lines == ["metrics: opened https://example/pull/8"]
    assert not request.exists()


@pytest.mark.parametrize("guarded", [True, False])
def test_dispatch_all_answers_a_metrics_request_only_inside_the_guarded_pass(
    monkeypatch: pytest.MonkeyPatch, guarded: bool
) -> None:
    order = list[str]()

    def run_pass(app: Any, base: Any, targets: list[Any], each: Any) -> None:
        if guarded:
            each(targets[0], print)

    text = (cli.ROOT / "pyproject.toml").read_text()
    monkeypatch.setattr(dispatch, "show_main", lambda root, path: text)
    monkeypatch.setattr("sys.stdin", io.StringIO("the key"))
    monkeypatch.setattr(dispatch, "run_pass", run_pass)
    monkeypatch.setattr(verify, "requested", lambda app, cfg, out: order.append("verify"))
    monkeypatch.setattr(cli, "requested", lambda app, base, out: order.append(out.__name__))
    assert cli.main(["dispatch-all", "--key-stdin"]) == 0
    assert order == (["verify", "say"] if guarded else [])


def test_metrics_publish_prints_what_publishing_did(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (cli.ROOT / "pyproject.toml").read_text()
    monkeypatch.setattr(dispatch, "show_main", lambda root, path: text)
    monkeypatch.setattr("sys.stdin", io.StringIO("the key"))
    monkeypatch.setattr(cli, "publish", lambda app, base: "metrics: opened https://example/pull/8")
    assert cli.main(["metrics", "--publish", "--key-stdin"]) == 0
    assert capsys.readouterr().out == "metrics: opened https://example/pull/8\n"


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
    assert lines[-1] == "| Total | 4 | 10 min | 42 min | $0.57 | $2.28 | 4 |"
    assert len(lines) == 7
    read = {"issues": "read", "pull_requests": "read"}
    assert app.asked == [(repo, read) for repo in config.load(text).repos]
    assert not [call for call in app.api.calls if "/issues/2/" in call[1]]
