from pathlib import Path

import pytest
from conftest import CFG, ROOT

from task_agent import config

TEXT = """
[tool.task-agent]
repo = "o/home"
app_slug = "app"
client_id = "id"
key_title = "key"
key_vault = "vault"
budget_usd = 1.5
agent_commands = ["make sample"]

[tool.task-agent.repos]
"o/home" = "."
"o/other" = "../other"
"""


def test_load_reads_the_table_from_text(tmp_path: Path) -> None:
    cfg = config.load(TEXT, tmp_path)
    assert (cfg.repo, cfg.owner, cfg.name, cfg.bot) == ("o/home", "o", "home", "app[bot]")
    assert (cfg.budget_usd, cfg.max_turns, cfg.agent_commands) == (1.5, 40, ("make sample",))
    assert cfg.root == cfg.clone == tmp_path
    assert cfg.model == ""


def test_this_repo_configures_itself_and_the_other_three() -> None:
    cfg = config.load()
    assert cfg.repo == "yasabbasi/task-agent" and cfg.target().clone == ROOT
    assert [repo.split("/")[1] for repo in cfg.repos] == [
        "task-agent",
        "data-pipeline",
        "mcp-server",
        "container-stack",
    ]
    assert (cfg.max_turns, cfg.budget_usd, cfg.timeout_min) == (40, 3.0, 50)


def test_target_aims_at_a_configured_repo_or_a_given_clone(tmp_path: Path) -> None:
    cfg = config.load(TEXT, tmp_path / "home")
    other = cfg.target("o/other")
    assert (other.repo, other.clone, other.root) == (
        "o/other",
        tmp_path / "other",
        tmp_path / "home",
    )
    assert cfg.target("o/new", str(tmp_path / "elsewhere")).clone == tmp_path / "elsewhere"
    with pytest.raises(SystemExit, match="o/new is not under"):
        cfg.target("o/new")


def test_table_is_empty_without_the_section() -> None:
    assert config.table("[project]\nname = 'x'\n") == {}
    assert CFG.repos == {}
