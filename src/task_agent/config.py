"""Settings from [tool.task-agent] in pyproject.toml, which agents may not change."""

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    """One repo's tasks, the App that works on them, and each session's limits."""

    repo: str
    app_slug: str
    client_id: str
    key_title: str
    key_vault: str
    parallel: int = 3
    max_turns: int = 40
    budget_usd: float = 3.0
    timeout_min: int = 50
    agent_commands: tuple[str, ...] = ()
    repos: dict[str, str] = field(default_factory=dict)
    root: Path = ROOT
    clone: Path = ROOT

    @property
    def owner(self) -> str:
        return self.repo.split("/")[0]

    @property
    def name(self) -> str:
        return self.repo.split("/")[1]

    @property
    def bot(self) -> str:
        return f"{self.app_slug}[bot]"

    def target(self, repo: str | None = None, clone: str | None = None) -> Config:
        """This config aimed at another configured repo, or at a clone given by path."""
        repo = repo or self.repo
        path = clone or self.repos.get(repo)
        if path is None:
            raise SystemExit(f"{repo} is not under [tool.task-agent.repos]; pass --clone")
        return replace(self, repo=repo, clone=(self.root / Path(path).expanduser()).resolve())


def table(text: str) -> dict[str, Any]:
    """The [tool.task-agent] table of a pyproject.toml, empty when it has none."""
    found: dict[str, Any] = tomllib.loads(text).get("tool", {}).get("task-agent", {})
    return found


def load(text: str | None = None, root: Path = ROOT) -> Config:
    """The settings in this pyproject.toml text, or in the file under root."""
    data = table((root / "pyproject.toml").read_text() if text is None else text)
    data["agent_commands"] = tuple(data.get("agent_commands", ()))
    return Config(**data, root=root, clone=root)
