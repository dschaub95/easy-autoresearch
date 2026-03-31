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
DEFAULT_BASELINE_LOG = "baseline.log"
CODEX_SYSTEM_PROMPT = """# Codex System Prompt

You are operating inside a repository prepared by easy-autoresearch.

Current phase:
- No autonomous proposal generation is enabled yet.
- Use this prompt file as the future home for session-specific Codex instructions.

Expected future responsibilities:
- inspect repository state
- propose an experiment
- execute the experiment loop
- record results back through the orchestrator
"""


def config_path(repo_path: Path) -> Path:
    return repo_path / CONFIG_FILENAME


def state_dir(repo_path: Path) -> Path:
    return repo_path / STATE_DIRNAME


def db_path(repo_path: Path) -> Path:
    return state_dir(repo_path) / DB_FILENAME


def prompts_dir(repo_path: Path) -> Path:
    return state_dir(repo_path) / PROMPTS_DIRNAME


@dataclass(slots=True)
class ProjectConfig:
    repo_path: str
    name: str


@dataclass(slots=True)
class CommandsConfig:
    baseline: str = "uv run pytest"
    metric_pattern: str | None = None


@dataclass(slots=True)
class SessionConfig:
    max_duration_seconds: int = 3600


@dataclass(slots=True)
class ExperimentsConfig:
    max_experiments: int = 1
    max_runs_per_experiment: int = 1


@dataclass(slots=True)
class CodexConfig:
    command: str = "codex"
    prompt_template: str = ".autoresearch/prompts/codex-system.md"


@dataclass(slots=True)
class AutoResearchConfig:
    project: ProjectConfig
    commands: CommandsConfig = field(default_factory=CommandsConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    experiments: ExperimentsConfig = field(default_factory=ExperimentsConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    editable_paths: list[str] = field(default_factory=list)
    readonly_paths: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutoResearchConfig:
        project_data = data.get("project") or {}
        commands_data = data.get("commands") or {}
        session_data = data.get("session") or {}
        experiments_data = data.get("experiments") or {}
        codex_data = data.get("codex") or {}
        return cls(
            project=ProjectConfig(**project_data),
            commands=CommandsConfig(**commands_data),
            session=SessionConfig(**session_data),
            experiments=ExperimentsConfig(**experiments_data),
            codex=CodexConfig(**codex_data),
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
