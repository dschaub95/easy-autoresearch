"""Configuration loading and filesystem conventions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILENAME = "autoresearch.yaml"
STATE_DIRNAME = ".autoresearch"
DB_FILENAME = "state.db"
PROMPTS_DIRNAME = "prompts"
LOGS_DIRNAME = "logs"
DASHBOARD_STATE_FILENAME = "dashboard.json"
DEFAULT_BASELINE_LOG = "baseline.log"


def config_path(repo_path: Path) -> Path:
    return repo_path / CONFIG_FILENAME


def state_dir(repo_path: Path) -> Path:
    return repo_path / STATE_DIRNAME


def db_path(repo_path: Path) -> Path:
    return state_dir(repo_path) / DB_FILENAME


def prompts_dir(repo_path: Path) -> Path:
    return state_dir(repo_path) / PROMPTS_DIRNAME


def logs_dir(repo_path: Path) -> Path:
    return state_dir(repo_path) / LOGS_DIRNAME


def dashboard_state_path(repo_path: Path) -> Path:
    return state_dir(repo_path) / DASHBOARD_STATE_FILENAME


@dataclass(slots=True)
class ProjectConfig:
    repo_path: str
    name: str


@dataclass(slots=True)
class CommandsConfig:
    run: str = "uv run pytest"
    metric_pattern: str | None = None


@dataclass(slots=True)
class SessionConfig:
    max_duration_seconds: int = 3600


@dataclass(slots=True)
class ExperimentsConfig:
    max_experiments: int = 1
    max_runs_per_experiment: int = 1


@dataclass(slots=True)
class AgentConfig:
    provider: str = "codex"
    model: str | None = "gpt-5.4-mini"
    sandbox_mode: str = "workspace-write"
    prompt_template: str = ".autoresearch/prompts/codex-system.md"


@dataclass(slots=True)
class ConstraintsConfig:
    runtime: int | float | str | None = 1.1


@dataclass(slots=True)
class AutoResearchConfig:
    project: ProjectConfig
    commands: CommandsConfig = field(default_factory=CommandsConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    experiments: ExperimentsConfig = field(default_factory=ExperimentsConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    constraints: ConstraintsConfig = field(default_factory=ConstraintsConfig)
    editable_paths: list[str] = field(default_factory=list)
    readonly_paths: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutoResearchConfig:
        project_data = data.get("project") or {}
        commands_data = data.get("commands") or {}
        session_data = data.get("session") or {}
        experiments_data = data.get("experiments") or {}
        agent_data = data.get("agent") or {}
        constraints_data = data.get("constraints") or {}
        if not agent_data and (codex_data := data.get("codex") or {}):
            agent_data = {
                "provider": "codex",
                "prompt_template": codex_data.get(
                    "prompt_template", AgentConfig().prompt_template
                ),
            }
        elif codex_data := data.get("codex") or {}:
            if "prompt_template" not in agent_data and codex_data.get(
                "prompt_template"
            ):
                agent_data["prompt_template"] = codex_data["prompt_template"]
        return cls(
            project=ProjectConfig(**project_data),
            commands=CommandsConfig(**commands_data),
            session=SessionConfig(**session_data),
            experiments=ExperimentsConfig(**experiments_data),
            agent=AgentConfig(**agent_data),
            constraints=ConstraintsConfig(**constraints_data),
            editable_paths=list(data.get("editable_paths") or []),
            readonly_paths=list(data.get("readonly_paths") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_config_for_repo(repo_path: Path) -> AutoResearchConfig:
    return AutoResearchConfig(
        project=ProjectConfig(repo_path=str(repo_path.resolve()), name=repo_path.name),
    )


def write_config(config: AutoResearchConfig, repo_path: Path) -> Path:
    path = config_path(repo_path)
    path.write_text(
        yaml.safe_dump(config.to_dict(), sort_keys=False),
        encoding="utf-8",
    )
    return path


def load_config(repo_path: Path) -> AutoResearchConfig:
    path = config_path(repo_path)
    if not path.exists():
        raise FileNotFoundError(f"Config not found at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = AutoResearchConfig.from_dict(data)
    if Path(config.project.repo_path).resolve() != repo_path.resolve():
        config.project.repo_path = str(repo_path.resolve())
    return config
