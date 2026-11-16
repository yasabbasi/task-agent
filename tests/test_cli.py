import subprocess

import pytest

from task_agent import cli


def test_version_flag_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--version"])
    assert exit_.value.code == 0
    assert capsys.readouterr().out.strip()[0].isdigit()


def test_unknown_command_fails() -> None:
    with pytest.raises(SystemExit) as exit_:
        cli.main(["nope"])
    assert exit_.value.code == 2


def test_console_script_runs() -> None:
    result = subprocess.run(["task-agent", "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "usage: task-agent" in result.stdout
