import os
import sys
import time
from pathlib import Path

import pytest
from conftest import run_git

from task_agent import config, watch

ECHO = "import sys; print(len(sys.stdin.read()), 'characters', *sys.argv[1:])"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(watch, "STATE", tmp_path / "state")
    (tmp_path / "state" / "logs").mkdir(parents=True)
    (tmp_path / "state" / "requests").mkdir()
    return tmp_path / "state"


def test_say_prints_and_keeps_the_line_in_the_log(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch.say("hello")
    assert capsys.readouterr().out.endswith(" hello\n")
    assert (state / "logs" / "watch.log").read_text().endswith(" hello\n")


def test_update_fast_forwards_and_reports_new_code(
    cfg: config.Config, github: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch, "ROOT", cfg.root)
    assert watch.update() is False
    other = cfg.root.parent / "other"
    run_git(cfg.root.parent, "clone", "--quiet", str(github), "other")
    run_git(other, "commit", "--quiet", "--allow-empty", "-m", "new code")
    run_git(other, "push", "--quiet", "origin", "main")
    assert watch.update() is True
    assert run_git(cfg.root, "rev-parse", "HEAD") == run_git(other, "rev-parse", "HEAD")
    run_git(cfg.root, "switch", "--quiet", "-c", "elsewhere")
    run_git(other, "commit", "--quiet", "--allow-empty", "-m", "newer code")
    run_git(other, "push", "--quiet", "origin", "main")
    assert watch.update() is True


def test_a_pass_gets_the_key_on_stdin_and_its_output_is_logged(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch, "CLI", [sys.executable, "-c", ECHO])
    watch.one_pass("the key")
    log = (state / "logs" / "watch.log").read_text()
    assert log.endswith(" 7 characters dispatch-all --key-stdin\n")


def test_restart_hands_the_key_to_the_new_code_through_a_pipe(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(watch, "CLI", [sys.executable, "-c", "pass"])
    monkeypatch.setattr(os, "execv", lambda path, argv: seen.append(argv))
    watch.restart("the key", 300)
    argv = seen[0]
    assert argv[3:6] == ["watch", "--every", "300"] and argv[6] == "--key-fd"
    with os.fdopen(int(argv[7])) as pipe:
        assert pipe.read() == "the key"


def test_restart_keeps_the_old_watcher_when_the_new_code_does_not_start(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch, "CLI", [sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setattr(os, "execv", lambda path, argv: pytest.fail("must not restart"))
    watch.restart("the key", 300)
    assert "staying on the old watcher" in (state / "logs" / "watch.log").read_text()


@pytest.mark.parametrize("request_file", ["wake", "verify-typo"])
def test_a_request_that_arrives_during_a_pass_starts_the_next_pass_at_once(
    state: Path, monkeypatch: pytest.MonkeyPatch, request_file: str
) -> None:
    passes, requests = list[str](), state / "requests"

    def one_pass(key: str) -> None:
        passes.append(key)
        if len(passes) == 2:
            raise KeyboardInterrupt
        (requests / request_file).write_text("")

    monkeypatch.setattr(watch, "read_key", lambda title, vault: f"key from {vault}")
    monkeypatch.setattr(watch, "update", lambda: False)
    monkeypatch.setattr(watch, "one_pass", one_pass)
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("must not wait"))
    with pytest.raises(KeyboardInterrupt):
        watch.watch(600, None)
    assert len(passes) == 2


def test_watch_reads_the_key_once_and_wakes_early_only_for_a_new_request(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    passes, sleeps, requests = list[str](), list[float](), state / "requests"
    (requests / "wake").write_text("")
    (requests / "verify-typo").write_text("")

    def one_pass(key: str) -> None:
        assert not (requests / "wake").exists()
        passes.append(key)
        if len(passes) == 2:
            raise KeyboardInterrupt

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            (requests / "wake").write_text("")

    monkeypatch.setattr(watch, "read_key", lambda title, vault: f"key from {vault}")
    monkeypatch.setattr(watch, "update", lambda: False)
    monkeypatch.setattr(watch, "one_pass", one_pass)
    monkeypatch.setattr(time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        watch.watch(600, None)
    assert passes == ["key from Work", "key from Work"]
    assert sleeps == [5, 5, 5] and (requests / "verify-typo").exists()


def test_the_watcher_runs_git_without_the_owners_global_config(
    cfg: config.Config, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch, "ROOT", cfg.root)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "missing" / "gitconfig"))
    assert watch.run("git", "config", "--global", "--list").returncode == 0
    assert watch.update() is False
    assert not (state / "logs" / "watch.log").exists()
