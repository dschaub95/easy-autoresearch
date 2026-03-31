"""Main entrypoint and session workflow for easy-autoresearch."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import (
    CODEX_SYSTEM_PROMPT,
    AutoResearchConfig,
    config_path,
    db_path,
    default_config_for_repo,
    load_config,
    prompts_dir,
    state_dir,
    write_config,
)
from .db import (
    connect,
    create_experiment,
    create_run,
    create_session,
    finish_run,
    finish_session,
    initialize_database,
    update_experiment,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class CommandResult:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    status: str
    metric_value: float | None


class AutoResearch:
    """Session-scoped application state and workflow."""

    def __init__(
        self,
        repo_path: Path,
        config: AutoResearchConfig | None = None,
    ) -> None:
        self.repo_path = repo_path.resolve()
        self.config = config
        self.config_file = config_path(self.repo_path)
        self.state_dir = state_dir(self.repo_path)
        self.prompts_dir = prompts_dir(self.repo_path)
        self.database_path = db_path(self.repo_path)

    def scaffold_repo(self) -> None:
        self.repo_path.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = self.prompts_dir / "codex-system.md"
        if not prompt_path.exists():
            prompt_path.write_text(CODEX_SYSTEM_PROMPT, encoding="utf-8")
        if not self.config_file.exists():
            self.config = default_config_for_repo(self.repo_path)
            write_config(self.config, self.repo_path)
        initialize_database(self.database_path)

    def has_existing_setup(self) -> bool:
        return self.config_file.exists()

    def overwrite_setup(self) -> None:
        if self.config_file.exists():
            self.config_file.unlink()
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        self.config = None

    def setup(self, *, overwrite: bool = False) -> None:
        if self.has_existing_setup():
            if overwrite:
                self.overwrite_setup()
                print(
                    f"Overwriting existing easy-autoresearch setup in {self.repo_path}"
                )
            elif prompt_for_existing_setup(self.repo_path):
                self.overwrite_setup()
                print(
                    f"Overwriting existing easy-autoresearch setup in {self.repo_path}"
                )
            else:
                self.config = load_config(self.repo_path)
                print(
                    f"Continuing with existing easy-autoresearch setup in {self.repo_path}"
                )

        if not self.has_existing_setup():
            self.scaffold_repo()
            print(f"Scaffolded easy-autoresearch files in {self.repo_path}")
            print(f"Config: {self.config_file}")
        elif self.config is None:
            self.config = load_config(self.repo_path)

    def start(self) -> int:
        config = self.require_config()
        started_at = utc_now()
        with connect(self.database_path) as connection:
            session_id = create_session(
                connection,
                repo_path=str(self.repo_path),
                max_duration_seconds=config.session.max_duration_seconds,
                status="running",
                started_at=started_at,
                created_at=started_at,
            )

            experiment_count = 0
            total_run_count = 0
            session_status = "failed"
            for experiment_index in range(1, config.experiments.max_experiments + 1):
                experiment_count += 1
                experiment_started_at = utc_now()
                experiment_kind = "baseline" if experiment_index == 1 else "candidate"
                description = (
                    "Initial baseline execution"
                    if experiment_index == 1
                    else f"Candidate experiment {experiment_index}"
                )
                experiment_id = create_experiment(
                    connection,
                    session_id=session_id,
                    kind=experiment_kind,
                    description=description,
                    max_runs=config.experiments.max_runs_per_experiment,
                    status="running",
                    created_at=experiment_started_at,
                    updated_at=experiment_started_at,
                )

                best_metric: float | None = None
                experiment_status = "failed"
                for run_index in range(1, config.experiments.max_runs_per_experiment + 1):
                    total_run_count += 1
                    run_started_at = utc_now()
                    run_id = create_run(
                        connection,
                        experiment_id=experiment_id,
                        run_index=run_index,
                        command=config.commands.baseline,
                        status="running",
                        started_at=run_started_at,
                        created_at=run_started_at,
                    )
                    result = run_command(
                        config.commands.baseline,
                        cwd=self.repo_path,
                        timeout_seconds=config.session.max_duration_seconds,
                        metric_pattern=config.commands.metric_pattern,
                    )
                    log_path = (
                        self.state_dir / f"experiment-{experiment_index}-run-{run_index}.log"
                    )
                    log_path.write_text(result.stdout, encoding="utf-8")
                    finish_run(
                        connection,
                        run_id=run_id,
                        status=result.status,
                        exit_code=result.exit_code,
                        stdout=result.stdout,
                        stderr=result.stderr,
                        metric_value=result.metric_value,
                        log_path=str(log_path.relative_to(self.repo_path)),
                        finished_at=utc_now(),
                    )
                    if result.metric_value is not None and (
                        best_metric is None or result.metric_value > best_metric
                    ):
                        best_metric = result.metric_value
                    if result.status == "completed":
                        experiment_status = "completed"
                        break

                update_experiment(
                    connection,
                    experiment_id=experiment_id,
                    status=experiment_status,
                    updated_at=utc_now(),
                    best_metric=best_metric,
                )
                if experiment_status == "completed":
                    session_status = "completed"
                    break

            finish_session(
                connection,
                session_id=session_id,
                status=session_status,
                finished_at=utc_now(),
            )
        session_id, run_count = session_id, total_run_count
        print(
            "Started session "
            f"{session_id} with {experiment_count} experiment(s) "
            f"and {run_count} run(s) ({session_status})."
        )
        return 0 if session_status == "completed" else 1

    def require_config(self) -> AutoResearchConfig:
        if self.config is None:
            raise RuntimeError("AutoResearch config has not been loaded")
        return self.config


def parse_metric(output: str, pattern: str | None) -> float | None:
    if not pattern:
        return None
    match = re.search(pattern, output, flags=re.MULTILINE)
    if match is None:
        return None
    return float(match.group(1))


def run_command(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: int,
    metric_pattern: str | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        return CommandResult(
            command=command,
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            status="timed_out",
            metric_value=parse_metric(stdout, metric_pattern),
        )

    status = "completed" if completed.returncode == 0 else "failed"
    return CommandResult(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        status=status,
        metric_value=parse_metric(completed.stdout, metric_pattern),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="easy-autoresearch")
    parser.add_argument("repo_path", nargs="?", type=Path, default=Path("."))
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite any existing easy-autoresearch setup before starting.",
    )
    return parser


def prompt_for_existing_setup(repo_path: Path) -> bool:
    while True:
        response = input(
            f"Existing easy-autoresearch setup found in {repo_path}. "
            "Continue with it or overwrite it? [c/o]: "
        ).strip().lower()
        if response in {"c", "continue"}:
            return False
        if response in {"o", "overwrite"}:
            return True
        print("Enter 'c' to continue or 'o' to overwrite.")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    autoresearch = AutoResearch(args.repo_path)
    autoresearch.setup(overwrite=args.overwrite)
    return autoresearch.start()
