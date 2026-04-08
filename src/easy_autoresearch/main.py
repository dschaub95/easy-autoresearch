"""Main entrypoint and session workflow for easy-autoresearch."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Literal

from .agent import Codex, CodingAgent
from .app.server import DashboardServer, run_dashboard_server
from .config import (
    AutoResearchConfig,
    config_path,
    db_path,
    default_config_for_repo,
    load_config,
    logs_dir,
    prompts_dir,
    state_dir,
    write_config,
)
from .git import (
    GitWorktreeError,
    commit_all_changes,
    current_head_sha,
    discard_uncommitted_changes,
    ensure_clean_tracking,
    has_uncommitted_changes,
    restore_worktree_snapshot,
    save_worktree_snapshot,
    switch_to_session_branch,
)
from .prompts import (
    CODEX_SYSTEM_PROMPT,
    build_agent_phase_prompt,
    build_commit_message_prompt,
    build_experiment_summary,
    build_initial_planning_prompt,
    build_runtime_constraint_text,
    build_setup_commit_message_prompt,
    build_setup_prompt,
    build_summary_prompt,
)
from .storage import (
    connect,
    create_agent_step,
    create_experiment,
    create_run,
    create_session,
    finish_agent_step,
    finish_run,
    finish_session,
    initialize_database,
    update_experiment,
    update_session_setup_commit,
    update_session_status,
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
    runtime_seconds: float | None = None


AgentPhase = Literal["planning", "execution", "issue_resolution"]
AgentStepPhase = Literal[
    "initial_planning", "planning", "execution", "issue_resolution", "commit_message"
]


@dataclass(slots=True)
class AgentPhaseResult:
    phase: AgentStepPhase
    prompt: str
    status: str
    exit_code: int | None
    log_path: Path
    stderr_path: Path
    response_text: str
    stderr: str
    agent_session_id: str | None


@dataclass(slots=True)
class ExperimentResult:
    status: str
    best_metric: float | None
    run_count: int
    best_runtime_seconds: float | None = None
    previous_best_metric: float | None = None
    metric_improved: bool = False
    changes_discarded: bool = False
    commit_sha: str | None = None


def create_agent(config: AutoResearchConfig, repo_path: Path) -> CodingAgent:
    if config.agent.provider != "codex":
        raise ValueError(f"Unsupported agent provider: {config.agent.provider}")
    return Codex(
        repo_path,
        model=config.agent.model,
        sandbox_mode=config.agent.sandbox_mode,
        stream_output=True,
    )


class AutoResearch:
    """Session-scoped application state and workflow."""

    def __init__(
        self,
        repo_path: Path,
        config: AutoResearchConfig | None = None,
        *,
        assume_yes: bool = False,
        headless: bool = False,
        server_host: str = "127.0.0.1",
        server_port: int = 8765,
    ) -> None:
        self.repo_path = repo_path.resolve()
        self.config = config
        self.config_file = config_path(self.repo_path)
        self.state_dir = state_dir(self.repo_path)
        self.logs_dir = logs_dir(self.repo_path)
        self.prompts_dir = prompts_dir(self.repo_path)
        self.database_path = db_path(self.repo_path)
        self.ready_to_start = True
        self.assume_yes = assume_yes
        self.headless = headless
        self.server_host = server_host
        self.server_port = server_port
        self.dashboard_server: DashboardServer | None = None
        self.did_scaffold = False
        self.session_id: int | None = None
        self.session_branch: str | None = None
        self.setup_commit_sha: str | None = None
        self.baseline_runtime_seconds: float | None = None
        self.runtime_cap_seconds: float | None = None

    def scaffold_repo(self) -> None:
        self.repo_path.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ensure_log_directories()
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = self.prompts_dir / "codex-system.md"
        if not prompt_path.exists():
            prompt_path.write_text(CODEX_SYSTEM_PROMPT, encoding="utf-8")
        if not self.config_file.exists():
            self.config = default_config_for_repo(self.repo_path)
            write_config(self.config, self.repo_path)
        initialize_database(self.database_path)
        self.did_scaffold = True

    def has_existing_setup(self) -> bool:
        return self.config_file.exists()

    def overwrite_setup(self) -> None:
        if self.config_file.exists():
            self.config_file.unlink()
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        self.config = None

    def should_overwrite_existing_setup(self, *, overwrite: bool) -> bool:
        if overwrite:
            return True
        return prompt_for_existing_setup(self.repo_path)

    def resolve_setup_state(self, *, overwrite: bool = False) -> None:
        if self.has_existing_setup():
            if self.should_overwrite_existing_setup(overwrite=overwrite):
                self.overwrite_setup()
                print(
                    f"Overwriting existing easy-autoresearch setup in {self.repo_path}"
                )
            else:
                self.config = load_config(self.repo_path)
                print(
                    f"Continuing with existing easy-autoresearch setup in {self.repo_path}"
                )

    def scaffold_if_needed(self) -> None:
        if not self.has_existing_setup():
            self.scaffold_repo()
        elif self.config is None:
            self.config = load_config(self.repo_path)

    def review_scaffold_if_needed(self) -> None:
        if (
            self.did_scaffold
            and not self.assume_yes
            and not prompt_for_config_review(self.config_file)
        ):
            print("Cancelled after scaffolding so you can adjust the config.")
            self.ready_to_start = False

    def prepare_repo_setup(self) -> None:
        if not self.did_scaffold or not self.ready_to_start:
            return
        agent = create_agent(self.require_config(), self.repo_path)
        result = agent.run(
            self.build_setup_prompt(),
            output_path=self.setup_logs_dir / "setup.agent.jsonl",
            stderr_path=self.setup_logs_dir / "setup.agent.stderr.log",
            timeout_seconds=self.require_config().session.max_duration_seconds,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                "Agent setup failed. Check "
                f"{result.output_path.relative_to(self.repo_path)} and "
                f"{result.stderr_path.relative_to(self.repo_path)}."
            )
        self.config = load_config(self.repo_path)
        if not has_uncommitted_changes(self.repo_path):
            return
        commit_message_result = agent.run(
            build_setup_commit_message_prompt(),
            output_path=self.setup_logs_dir / "setup.commit_message.jsonl",
            stderr_path=self.setup_logs_dir / "setup.commit_message.stderr.log",
            timeout_seconds=self.require_config().session.max_duration_seconds,
            text_capture="latest",
        )
        if (
            commit_message_result.exit_code != 0
            or not commit_message_result.text.strip()
        ):
            raise RuntimeError(
                "Agent setup commit message generation failed. Check "
                f"{commit_message_result.output_path.relative_to(self.repo_path)} and "
                f"{commit_message_result.stderr_path.relative_to(self.repo_path)}."
            )
        self.setup_commit_sha = commit_all_changes(
            self.repo_path, commit_message_result.text.strip()
        )
        self.persist_setup_commit_sha()

    def review_prepared_setup_if_needed(self) -> None:
        if (
            self.did_scaffold
            and self.ready_to_start
            and not self.assume_yes
            and not prompt_for_setup_review(self.repo_path)
        ):
            print("Cancelled after setup so you can review the changes.")
            self.ready_to_start = False
            self.cancel_open_session()

    def open_session_branch(self) -> None:
        if not self.ready_to_start or self.session_id is not None:
            return
        config = self.require_config()
        opened_at = utc_now()
        with connect(self.database_path) as connection:
            session_id = create_session(
                connection,
                repo_path=str(self.repo_path),
                max_duration_seconds=config.session.max_duration_seconds,
                status="preparing",
                setup_commit_sha=self.setup_commit_sha,
                started_at=opened_at,
                created_at=opened_at,
            )
        try:
            session_branch = switch_to_session_branch(self.repo_path, session_id)
        except GitWorktreeError:
            with connect(self.database_path) as connection:
                finish_session(
                    connection,
                    session_id=session_id,
                    status="failed",
                    finished_at=utc_now(),
                )
            raise
        self.session_id = session_id
        self.session_branch = session_branch
        print(f"Switched to session branch {session_branch}")

    def persist_setup_commit_sha(self) -> None:
        if self.session_id is None:
            return
        with connect(self.database_path) as connection:
            update_session_setup_commit(
                connection,
                session_id=self.session_id,
                setup_commit_sha=self.setup_commit_sha,
            )

    def cancel_open_session(self) -> None:
        if self.session_id is None:
            return
        with connect(self.database_path) as connection:
            finish_session(
                connection,
                session_id=self.session_id,
                status="cancelled",
                finished_at=utc_now(),
            )

    def fail_open_session(self) -> None:
        if self.session_id is None:
            return
        with connect(self.database_path) as connection:
            finish_session(
                connection,
                session_id=self.session_id,
                status="failed",
                finished_at=utc_now(),
            )

    def start_dashboard(self) -> None:
        if self.headless or self.dashboard_server is not None:
            return
        self.dashboard_server = DashboardServer(
            repo_path=self.repo_path,
            host=self.server_host,
            port=self.server_port,
        )
        self.dashboard_server.start()
        if getattr(self.dashboard_server, "reused_existing", False):
            print(f"Dashboard already running at {self.dashboard_server.url}")
        else:
            print(f"Dashboard available at {self.dashboard_server.url}")

    def ensure_log_directories(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.setup_logs_dir.mkdir(parents=True, exist_ok=True)
        self.agent_logs_dir.mkdir(parents=True, exist_ok=True)
        self.agent_stderr_logs_dir.mkdir(parents=True, exist_ok=True)
        self.summary_logs_dir.mkdir(parents=True, exist_ok=True)
        self.run_logs_dir.mkdir(parents=True, exist_ok=True)

    @property
    def setup_logs_dir(self) -> Path:
        return self.logs_dir / "setup"

    @property
    def agent_logs_dir(self) -> Path:
        return self.logs_dir / "agent"

    @property
    def agent_stderr_logs_dir(self) -> Path:
        return self.logs_dir / "agent-stderr"

    @property
    def summary_logs_dir(self) -> Path:
        return self.logs_dir / "summaries"

    @property
    def run_logs_dir(self) -> Path:
        return self.logs_dir / "runs"

    def run_log_path(self, experiment_index: int, run_index: int) -> Path:
        return self.run_logs_dir / f"experiment-{experiment_index}-run-{run_index}.log"

    def summary_path_for_experiment(self, experiment_index: int) -> Path:
        return self.summary_logs_dir / f"experiment-{experiment_index}.md"

    def agent_artifact_paths(self, stem: str) -> tuple[Path, Path]:
        return (
            self.agent_logs_dir / f"{stem}.jsonl",
            self.agent_stderr_logs_dir / f"{stem}.log",
        )

    def stop_dashboard(self) -> None:
        if self.dashboard_server is not None:
            self.dashboard_server.stop()
            self.dashboard_server = None

    def run_session(self) -> int:
        if not self.ready_to_start:
            return 0
        if self.session_id is None or self.session_branch is None:
            raise RuntimeError("Session branch must be created before running.")
        config = self.require_config()
        self.validate_runnable_config()
        print(f"Starting autoresearch in {self.repo_path}")
        session_id = self.session_id
        with connect(self.database_path) as connection:
            update_session_status(
                connection,
                session_id=session_id,
                status="running",
            )
            print("Running baseline experiment")

            experiment_count = 0
            total_run_count = 0
            session_status = "failed"

            baseline_started_at = utc_now()
            baseline_experiment_id = create_experiment(
                connection,
                session_id=session_id,
                kind="baseline",
                description="Initial baseline execution",
                max_runs=1,
                status="running",
                agent_provider=None,
                created_at=baseline_started_at,
                updated_at=baseline_started_at,
            )
            baseline_result = self.run_baseline_experiment(
                connection,
                experiment_id=baseline_experiment_id,
                experiment_index=1,
            )
            self.baseline_runtime_seconds = baseline_result.best_runtime_seconds
            self.runtime_cap_seconds = self.resolve_runtime_cap_seconds(
                self.baseline_runtime_seconds
            )
            session_best_metric = baseline_result.best_metric
            total_run_count += baseline_result.run_count
            if (
                baseline_result.status == "completed"
                and config.experiments.max_experiments == 0
            ):
                session_status = "completed"

            for experiment_index in range(1, config.experiments.max_experiments + 1):
                experiment_count += 1
                experiment_started_at = utc_now()
                base_commit_sha = current_head_sha(self.repo_path)
                experiment_id = create_experiment(
                    connection,
                    session_id=session_id,
                    kind="candidate",
                    description=f"Candidate experiment {experiment_index}",
                    max_runs=config.experiments.max_runs_per_experiment,
                    status="running",
                    agent_provider=config.agent.provider,
                    previous_best_metric=session_best_metric,
                    base_commit_sha=base_commit_sha,
                    created_at=experiment_started_at,
                    updated_at=experiment_started_at,
                )
                print(f"Running candidate experiment {experiment_index}")
                experiment_result = self.run_agent_experiment(
                    connection,
                    experiment_id=experiment_id,
                    experiment_index=experiment_index,
                    previous_best_metric=session_best_metric,
                    base_commit_sha=base_commit_sha,
                )
                total_run_count += experiment_result.run_count
                if experiment_result.metric_improved:
                    session_best_metric = experiment_result.best_metric
                if experiment_result.status == "completed":
                    session_status = "completed"
                    break

            finish_session(
                connection,
                session_id=session_id,
                status=session_status,
                finished_at=utc_now(),
            )
        print(
            "Started session "
            f"{session_id} with 1 baseline run and {experiment_count} experiment(s) "
            f"and {total_run_count} run(s) ({session_status})."
        )
        return 0 if session_status == "completed" else 1

    def run_baseline_experiment(
        self,
        connection,
        *,
        experiment_id: int,
        experiment_index: int,
    ) -> ExperimentResult:
        config = self.require_config()
        run_index = 1
        print("Baseline run 1/1")
        run_started_at = utc_now()
        run_id = create_run(
            connection,
            experiment_id=experiment_id,
            run_index=run_index,
            command=config.commands.run,
            status="running",
            started_at=run_started_at,
            created_at=run_started_at,
        )
        result = run_command(
            config.commands.run,
            cwd=self.repo_path,
            timeout_seconds=config.session.max_duration_seconds,
            metric_pattern=config.commands.metric_pattern,
        )
        log_path = self.run_log_path(experiment_index, run_index)
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

        update_experiment(
            connection,
            experiment_id=experiment_id,
            status=result.status,
            updated_at=utc_now(),
            best_metric=result.metric_value,
        )
        return ExperimentResult(
            status=result.status,
            best_metric=result.metric_value,
            run_count=1,
            best_runtime_seconds=result.runtime_seconds,
        )

    def run_agent_experiment(
        self,
        connection,
        *,
        experiment_id: int,
        experiment_index: int,
        previous_best_metric: float | None,
        base_commit_sha: str,
    ) -> ExperimentResult:
        config = self.require_config()
        agent = create_agent(config, self.repo_path)
        template = self.load_prompt_template()
        best_metric: float | None = None
        experiment_status = "failed"
        run_count = 0
        best_result: CommandResult | None = None
        last_agent_log_path: Path | None = None
        last_agent_stderr_path: Path | None = None
        last_result: CommandResult | None = None
        initial_planning_result = self.run_initial_planning_step(
            connection,
            agent=agent,
            template=template,
            experiment_id=experiment_id,
            experiment_index=experiment_index,
            previous_best_metric=previous_best_metric,
        )
        if initial_planning_result is not None:
            last_agent_log_path = initial_planning_result.log_path
            last_agent_stderr_path = initial_planning_result.stderr_path
        if (
            initial_planning_result is not None
            and initial_planning_result.status != "completed"
        ):
            discard_uncommitted_changes(self.repo_path)
            update_experiment(
                connection,
                experiment_id=experiment_id,
                status=experiment_status,
                updated_at=utc_now(),
                best_metric=best_metric,
                previous_best_metric=previous_best_metric,
                metric_improved=False,
                changes_discarded=True,
                agent_session_id=agent.session_id,
                base_commit_sha=base_commit_sha,
                agent_log_path=(
                    str(last_agent_log_path.relative_to(self.repo_path))
                    if last_agent_log_path is not None
                    else None
                ),
                agent_stderr_path=(
                    str(last_agent_stderr_path.relative_to(self.repo_path))
                    if last_agent_stderr_path is not None
                    else None
                ),
            )
            return ExperimentResult(
                status=experiment_status,
                best_metric=best_metric,
                run_count=run_count,
                previous_best_metric=previous_best_metric,
                metric_improved=False,
                changes_discarded=True,
            )
        with tempfile.TemporaryDirectory(
            prefix="experiment-", dir=self.state_dir
        ) as tmp:
            best_snapshot_dir = Path(tmp) / "best"
            for run_index in range(1, config.experiments.max_runs_per_experiment + 1):
                if run_index > 1:
                    if best_snapshot_dir.exists():
                        restore_worktree_snapshot(self.repo_path, best_snapshot_dir)
                    else:
                        discard_uncommitted_changes(self.repo_path)
                run_count += 1
                run_started_at = utc_now()
                run_id = create_run(
                    connection,
                    experiment_id=experiment_id,
                    run_index=run_index,
                    command=config.commands.run,
                    status="running",
                    started_at=run_started_at,
                    created_at=run_started_at,
                )
                print(
                    f"Candidate run {run_index}/{config.experiments.max_runs_per_experiment}"
                )
                phase_results = self.run_agent_phases(
                    connection,
                    agent=agent,
                    experiment_id=experiment_id,
                    experiment_index=experiment_index,
                    run_index=run_index,
                )
                if phase_results:
                    last_agent_log_path = phase_results[-1].log_path
                    last_agent_stderr_path = phase_results[-1].stderr_path
                if not phase_results or any(
                    phase_result.status != "completed" for phase_result in phase_results
                ):
                    finish_run(
                        connection,
                        run_id=run_id,
                        status="failed",
                        exit_code=(
                            next(
                                (
                                    phase_result.exit_code
                                    for phase_result in phase_results
                                    if phase_result.status != "completed"
                                ),
                                1,
                            )
                            if phase_results
                            else 1
                        ),
                        stdout="\n\n".join(
                            phase_result.response_text
                            for phase_result in phase_results
                            if phase_result.response_text
                        ),
                        stderr="\n\n".join(
                            phase_result.stderr
                            for phase_result in phase_results
                            if phase_result.stderr
                        ),
                        metric_value=None,
                        log_path=(
                            str(last_agent_log_path.relative_to(self.repo_path))
                            if last_agent_log_path is not None
                            else None
                        ),
                        finished_at=utc_now(),
                    )
                    continue

                result = run_command(
                    config.commands.run,
                    cwd=self.repo_path,
                    timeout_seconds=config.session.max_duration_seconds,
                    metric_pattern=config.commands.metric_pattern,
                )
                print("Evaluation finished")
                if result.metric_value is None:
                    result = mark_result_failed(
                        result,
                        reason=(
                            "No metric matched pattern "
                            f"{config.commands.metric_pattern!r}."
                        ),
                    )
                runtime_constraint_satisfied = self.runtime_constraint_satisfied(result)
                if runtime_constraint_satisfied is False:
                    result = mark_result_failed(
                        result,
                        reason=(
                            "Runtime constraint violated. "
                            f"Observed runtime {format_runtime_seconds(result.runtime_seconds)} "
                            f"exceeds cap {format_runtime_seconds(self.runtime_cap_seconds)}."
                        ),
                    )
                last_result = result
                can_promote_result = runtime_constraint_satisfied is not False
                run_log_path = self.run_log_path(experiment_index, run_index)
                run_log_path.write_text(result.stdout, encoding="utf-8")
                finish_run(
                    connection,
                    run_id=run_id,
                    status=result.status,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    metric_value=result.metric_value,
                    log_path=str(run_log_path.relative_to(self.repo_path)),
                    finished_at=utc_now(),
                )
                if can_promote_result and metric_improved(
                    result.metric_value, best_metric
                ):
                    best_metric = result.metric_value
                    best_result = result
                    save_worktree_snapshot(self.repo_path, best_snapshot_dir)
                if result.status == "completed":
                    experiment_status = "completed"
                    break

            metric_did_improve = metric_improved(best_metric, previous_best_metric)
            if metric_did_improve and best_snapshot_dir.exists():
                restore_worktree_snapshot(self.repo_path, best_snapshot_dir)

            summary_text: str | None = None
            summary_path: Path | None = None
            summary_source_result = best_result if metric_did_improve else last_result
            print(f"Writing summary for experiment {experiment_index}")
            if agent.session_id:
                summary_path = self.summary_path_for_experiment(experiment_index)
                summary_output_path, summary_stderr_path = self.agent_artifact_paths(
                    f"experiment-{experiment_index}.summary"
                )
                summary_result = agent.run(
                    self.build_summary_prompt(summary_source_result),
                    output_path=summary_output_path,
                    stderr_path=summary_stderr_path,
                    timeout_seconds=config.session.max_duration_seconds,
                )
                summary_text = self.build_experiment_summary(
                    summary_result.text,
                    summary_source_result,
                    previous_best_metric=previous_best_metric,
                    metric_improved=metric_did_improve,
                    changes_discarded=not metric_did_improve,
                    baseline_runtime_seconds=self.baseline_runtime_seconds,
                    runtime_cap_seconds=self.runtime_cap_seconds,
                    runtime_constraint_satisfied=(
                        self.runtime_constraint_satisfied(summary_source_result)
                        if summary_source_result is not None
                        else None
                    ),
                )
                summary_path.write_text(summary_text, encoding="utf-8")

            commit_sha: str | None = None
            if metric_did_improve:
                commit_message = self.run_commit_message_step(
                    connection,
                    agent=agent,
                    experiment_id=experiment_id,
                    experiment_index=experiment_index,
                    run_index=run_count,
                )
                commit_sha = commit_all_changes(self.repo_path, commit_message)
            else:
                discard_uncommitted_changes(self.repo_path)

        update_experiment(
            connection,
            experiment_id=experiment_id,
            status=experiment_status,
            updated_at=utc_now(),
            best_metric=best_metric,
            previous_best_metric=previous_best_metric,
            metric_improved=metric_did_improve,
            changes_discarded=not metric_did_improve,
            agent_session_id=agent.session_id,
            commit_sha=commit_sha,
            base_commit_sha=base_commit_sha,
            summary=summary_text,
            summary_path=(
                str(summary_path.relative_to(self.repo_path))
                if summary_path is not None
                else None
            ),
            agent_log_path=(
                str(last_agent_log_path.relative_to(self.repo_path))
                if last_agent_log_path is not None
                else None
            ),
            agent_stderr_path=(
                str(last_agent_stderr_path.relative_to(self.repo_path))
                if last_agent_stderr_path is not None
                else None
            ),
        )
        return ExperimentResult(
            status=experiment_status,
            best_metric=best_metric,
            run_count=run_count,
            previous_best_metric=previous_best_metric,
            metric_improved=metric_did_improve,
            changes_discarded=not metric_did_improve,
            commit_sha=commit_sha,
        )

    def run_initial_planning_step(
        self,
        connection,
        *,
        agent: CodingAgent,
        template: str,
        experiment_id: int,
        experiment_index: int,
        previous_best_metric: float | None,
    ) -> AgentPhaseResult | None:
        print("Agent phase: initial_planning")
        prompt = self.build_initial_planning_prompt(
            template=template,
            experiment_index=experiment_index,
            previous_best_metric=previous_best_metric,
        )
        started_at = utc_now()
        step_id = create_agent_step(
            connection,
            experiment_id=experiment_id,
            run_index=0,
            phase="initial_planning",
            prompt=prompt,
            status="running",
            started_at=started_at,
            created_at=started_at,
        )
        output_path, stderr_path = self.agent_artifact_paths(
            f"experiment-{experiment_index}.initial_planning"
        )
        result = agent.run(
            prompt,
            output_path=output_path,
            stderr_path=stderr_path,
            timeout_seconds=self.require_config().session.max_duration_seconds,
        )
        status = "completed" if result.exit_code == 0 else "failed"
        phase_result = AgentPhaseResult(
            phase="initial_planning",
            prompt=prompt,
            status=status,
            exit_code=result.exit_code,
            log_path=output_path,
            stderr_path=stderr_path,
            response_text=result.text,
            stderr=result.stderr,
            agent_session_id=result.session_id,
        )
        finish_agent_step(
            connection,
            step_id=step_id,
            status=status,
            exit_code=result.exit_code,
            agent_session_id=result.session_id,
            response_text=result.text,
            stderr=result.stderr,
            log_path=str(output_path.relative_to(self.repo_path)),
            stderr_path=str(stderr_path.relative_to(self.repo_path)),
            finished_at=utc_now(),
        )
        return phase_result

    def run_agent_phases(
        self,
        connection,
        *,
        agent: CodingAgent,
        experiment_id: int,
        experiment_index: int,
        run_index: int,
    ) -> list[AgentPhaseResult]:
        config = self.require_config()
        phase_results: list[AgentPhaseResult] = []
        for phase in ("planning", "execution", "issue_resolution"):
            print(f"Agent phase: {phase}")
            prompt = self.build_agent_phase_prompt(experiment_index, run_index, phase)
            started_at = utc_now()
            step_id = create_agent_step(
                connection,
                experiment_id=experiment_id,
                run_index=run_index,
                phase=phase,
                prompt=prompt,
                status="running",
                started_at=started_at,
                created_at=started_at,
            )
            output_path, stderr_path = self.agent_artifact_paths(
                f"experiment-{experiment_index}-run-{run_index}.{phase}"
            )
            result = agent.run(
                prompt,
                output_path=output_path,
                stderr_path=stderr_path,
                timeout_seconds=config.session.max_duration_seconds,
            )
            status = "completed" if result.exit_code == 0 else "failed"
            phase_result = AgentPhaseResult(
                phase=phase,
                prompt=prompt,
                status=status,
                exit_code=result.exit_code,
                log_path=output_path,
                stderr_path=stderr_path,
                response_text=result.text,
                stderr=result.stderr,
                agent_session_id=result.session_id,
            )
            finish_agent_step(
                connection,
                step_id=step_id,
                status=status,
                exit_code=result.exit_code,
                agent_session_id=result.session_id,
                response_text=result.text,
                stderr=result.stderr,
                log_path=str(output_path.relative_to(self.repo_path)),
                stderr_path=str(stderr_path.relative_to(self.repo_path)),
                finished_at=utc_now(),
            )
            phase_results.append(phase_result)
            if status != "completed":
                break
        return phase_results

    def run_commit_message_step(
        self,
        connection,
        *,
        agent: CodingAgent,
        experiment_id: int,
        experiment_index: int,
        run_index: int,
    ) -> str:
        print("Agent phase: commit_message")
        prompt = build_commit_message_prompt()
        started_at = utc_now()
        step_id = create_agent_step(
            connection,
            experiment_id=experiment_id,
            run_index=run_index,
            phase="commit_message",
            prompt=prompt,
            status="running",
            started_at=started_at,
            created_at=started_at,
        )
        output_path, stderr_path = self.agent_artifact_paths(
            f"experiment-{experiment_index}.commit_message"
        )
        result = agent.run(
            prompt,
            output_path=output_path,
            stderr_path=stderr_path,
            timeout_seconds=self.require_config().session.max_duration_seconds,
            text_capture="latest",
        )
        status = "completed" if result.exit_code == 0 else "failed"
        finish_agent_step(
            connection,
            step_id=step_id,
            status=status,
            exit_code=result.exit_code,
            agent_session_id=result.session_id,
            response_text=result.text,
            stderr=result.stderr,
            log_path=str(output_path.relative_to(self.repo_path)),
            stderr_path=str(stderr_path.relative_to(self.repo_path)),
            finished_at=utc_now(),
        )
        commit_message = result.text.strip()
        if status != "completed" or not commit_message:
            raise RuntimeError("Agent failed to produce a commit message.")
        return commit_message

    def build_agent_phase_prompt(
        self,
        experiment_index: int,
        run_index: int,
        phase: AgentPhase,
    ) -> str:
        config = self.require_config()
        return build_agent_phase_prompt(
            experiment_index=experiment_index,
            run_index=run_index,
            phase=phase,
            evaluation_command=config.commands.run,
            runtime_constraint_text=(
                self.build_runtime_constraint_text() if phase == "planning" else None
            ),
        )

    def build_initial_planning_prompt(
        self,
        *,
        template: str | None,
        experiment_index: int,
        previous_best_metric: float | None,
    ) -> str:
        config = self.require_config()
        return build_initial_planning_prompt(
            template,
            experiment_index=experiment_index,
            evaluation_command=config.commands.run,
            summary_dir=str(self.summary_logs_dir.relative_to(self.repo_path)),
            run_logs_dir=str(self.run_logs_dir.relative_to(self.repo_path)),
            agent_logs_dir=str(self.agent_logs_dir.relative_to(self.repo_path)),
            agent_stderr_logs_dir=str(
                self.agent_stderr_logs_dir.relative_to(self.repo_path)
            ),
            database_path=str(self.database_path.relative_to(self.repo_path)),
            previous_best_metric=previous_best_metric,
            runtime_constraint_text=self.build_runtime_constraint_text(),
        )

    def build_setup_prompt(self) -> str:
        return build_setup_prompt()

    def build_summary_prompt(self, result: CommandResult | None) -> str:
        return build_summary_prompt(result)

    def build_experiment_summary(
        self,
        summary: str,
        result: CommandResult | None,
        *,
        previous_best_metric: float | None,
        metric_improved: bool,
        changes_discarded: bool,
        baseline_runtime_seconds: float | None = None,
        runtime_cap_seconds: float | None = None,
        runtime_constraint_satisfied: bool | None = None,
    ) -> str:
        return build_experiment_summary(
            summary,
            result,
            previous_best_metric=previous_best_metric,
            metric_improved=metric_improved,
            changes_discarded=changes_discarded,
            baseline_runtime_seconds=baseline_runtime_seconds,
            runtime_cap_seconds=runtime_cap_seconds,
            runtime_constraint_satisfied=runtime_constraint_satisfied,
        )

    def load_prompt_template(self) -> str:
        prompt_path = self.repo_path / self.require_config().agent.prompt_template
        return prompt_path.read_text(encoding="utf-8").strip()

    def require_config(self) -> AutoResearchConfig:
        if self.config is None:
            raise RuntimeError("AutoResearch config has not been loaded")
        return self.config

    def build_runtime_constraint_text(self) -> str | None:
        return build_runtime_constraint_text(
            runtime_cap_seconds=self.runtime_cap_seconds,
            baseline_runtime_seconds=self.baseline_runtime_seconds,
        )

    def resolve_runtime_cap_seconds(
        self, baseline_runtime_seconds: float | None
    ) -> float | None:
        runtime_limit = self.require_config().constraints.runtime
        if runtime_limit is None:
            return None
        if isinstance(runtime_limit, (int, float)) and not isinstance(
            runtime_limit, bool
        ):
            if baseline_runtime_seconds is None:
                return None
            return baseline_runtime_seconds * float(runtime_limit)
        return parse_duration_to_seconds(runtime_limit)

    def runtime_constraint_satisfied(self, result: CommandResult | None) -> bool | None:
        if result is None or self.runtime_cap_seconds is None:
            return None
        if result.runtime_seconds is None:
            return False
        return result.runtime_seconds <= self.runtime_cap_seconds

    def validate_pre_setup_config(self) -> None:
        config = self.require_config()
        runtime_limit = config.constraints.runtime
        if runtime_limit is not None:
            if isinstance(runtime_limit, (int, float)) and not isinstance(
                runtime_limit, bool
            ):
                if runtime_limit <= 0:
                    raise ValueError("constraints.runtime must be greater than zero")
            elif isinstance(runtime_limit, str):
                parse_duration_to_seconds(runtime_limit)
            else:
                raise ValueError(
                    "constraints.runtime must be null, a float ratio, or a duration string"
                )

    def validate_runnable_config(self) -> None:
        config = self.require_config()
        if config.experiments.max_experiments > 0:
            if not config.commands.run:
                raise ValueError("commands.run must be configured")
            if not config.commands.metric_pattern:
                raise ValueError(
                    "commands.metric_pattern must be configured for agent experiments"
                )
        self.validate_pre_setup_config()


def parse_metric(output: str, pattern: str | None) -> float | None:
    if not pattern:
        return None
    match = re.search(pattern, output, flags=re.MULTILINE)
    if match is None:
        return None
    return float(match.group(1))


def metric_improved(candidate: float | None, reference: float | None) -> bool:
    if candidate is None:
        return False
    if reference is None:
        return True
    return candidate > reference


def parse_duration_to_seconds(value: str) -> float:
    cleaned = value.strip().lower()
    if not cleaned:
        raise ValueError("constraints.runtime cannot be empty")
    total_seconds = 0.0
    position = 0
    pattern = re.compile(r"(\d+(?:\.\d+)?)([hms])")
    unit_order = {"h": 3, "m": 2, "s": 1}
    previous_order = 4
    for match in pattern.finditer(cleaned):
        if match.start() != position:
            raise ValueError(
                "constraints.runtime must use duration strings like 30s, 5m, or 1h30m"
            )
        magnitude = float(match.group(1))
        unit = match.group(2)
        current_order = unit_order[unit]
        if current_order >= previous_order:
            raise ValueError(
                "constraints.runtime duration units must be ordered from largest to smallest"
            )
        previous_order = current_order
        if unit == "h":
            total_seconds += magnitude * 3600
        elif unit == "m":
            total_seconds += magnitude * 60
        else:
            total_seconds += magnitude
        position = match.end()
    if position != len(cleaned) or total_seconds <= 0:
        raise ValueError(
            "constraints.runtime must use duration strings like 30s, 5m, or 1h30m"
        )
    return total_seconds


def format_runtime_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}s"


def mark_result_failed(result: CommandResult, *, reason: str) -> CommandResult:
    return CommandResult(
        command=result.command,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=f"{result.stderr}\n{reason}".strip(),
        status="failed",
        metric_value=result.metric_value,
        runtime_seconds=result.runtime_seconds,
    )


def run_command(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: int,
    metric_pattern: str | None = None,
) -> CommandResult:
    started_at = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    queue: Queue[tuple[str, str]] = Queue()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def reader(stream, name: str, output_parts: list[str]) -> None:
        try:
            if stream is None:
                return
            for chunk in iter(stream.readline, ""):
                output_parts.append(chunk)
                queue.put((name, chunk))
        finally:
            if stream is not None:
                stream.close()

    stdout_thread = threading.Thread(
        target=reader,
        args=(process.stdout, "stdout", stdout_parts),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=reader,
        args=(process.stderr, "stderr", stderr_parts),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    metric_value: float | None = None
    try:
        exit_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        exit_code = None

    while stdout_thread.is_alive() or stderr_thread.is_alive() or not queue.empty():
        try:
            stream_name, chunk = queue.get(timeout=0.1)
        except Empty:
            continue
        if metric_pattern and metric_value is None and stream_name == "stdout":
            metric_value = parse_metric("".join(stdout_parts), metric_pattern)

    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    runtime_seconds = time.perf_counter() - started_at
    metric_value = metric_value or parse_metric(stdout, metric_pattern)

    if timed_out:
        return CommandResult(
            command=command,
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            status="timed_out",
            metric_value=metric_value,
            runtime_seconds=runtime_seconds,
        )

    status = "completed" if exit_code == 0 else "failed"
    return CommandResult(
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        status=status,
        metric_value=metric_value,
        runtime_seconds=runtime_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="easy-autoresearch")
    parser.add_argument("repo_path", nargs="?", type=Path, default=Path("."))
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Automatically answer yes to yes/no prompts.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite any existing easy-autoresearch setup before starting.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without starting the local observability server.",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=8765,
        help="Port for the local observability server.",
    )
    return parser


def build_dashboard_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="easy-autoresearch dashboard")
    parser.add_argument("repo_path", nargs="?", type=Path, default=Path("."))
    parser.add_argument(
        "--server-port",
        type=int,
        default=8765,
        help="Port for the local observability server.",
    )
    return parser


def build_dashboard_stop_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="easy-autoresearch dashboard-stop")
    parser.add_argument("repo_path", nargs="?", type=Path, default=Path("."))
    return parser


def build_serve_dashboard_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="easy-autoresearch serve-dashboard")
    parser.add_argument("--repo-path", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def prompt_for_existing_setup(repo_path: Path) -> bool:
    while True:
        response = (
            input(
                f"Existing easy-autoresearch setup found in {repo_path}. "
                "Continue with it or overwrite it? [c/o]: "
            )
            .strip()
            .lower()
        )
        if response in {"c", "continue"}:
            return False
        if response in {"o", "overwrite"}:
            return True
        print("Enter 'c' to continue or 'o' to overwrite.")


def prompt_for_config_review(config_file: Path) -> bool:
    while True:
        response = (
            input(
                f"Review and modify {config_file} if needed before repo setup continues. "
                "Continue with setup? [y/n]: "
            )
            .strip()
            .lower()
        )
        if response in {"y", "yes"}:
            return True
        if response in {"n", "no"}:
            return False
        print("Enter 'y' to continue setup or 'n' to stop.")


def prompt_for_setup_review(repo_path: Path) -> bool:
    while True:
        response = (
            input(
                f"Review the setup changes in {repo_path}. Start autoresearch now? [y/n]: "
            )
            .strip()
            .lower()
        )
        if response in {"y", "yes"}:
            return True
        if response in {"n", "no"}:
            return False
        print("Enter 'y' to start or 'n' to stop.")


def run_dashboard_command(argv: list[str] | None = None) -> int:
    parser = build_dashboard_parser()
    args = parser.parse_args(argv)
    autoresearch = AutoResearch(args.repo_path, server_port=args.server_port)
    autoresearch.start_dashboard()
    return 0


def run_dashboard_stop_command(argv: list[str] | None = None) -> int:
    parser = build_dashboard_stop_parser()
    args = parser.parse_args(argv)
    dashboard_server = DashboardServer(repo_path=args.repo_path.resolve())
    if dashboard_server.stop():
        print(f"Dashboard stopped for {args.repo_path.resolve()}")
    else:
        print(f"No running dashboard found for {args.repo_path.resolve()}")
    return 0


def run_serve_dashboard_command(argv: list[str] | None = None) -> int:
    parser = build_serve_dashboard_parser()
    args = parser.parse_args(argv)
    run_dashboard_server(
        repo_path=args.repo_path.resolve(),
        host=args.host,
        port=args.port,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "dashboard":
        return run_dashboard_command(argv[1:])
    if argv and argv[0] == "dashboard-stop":
        return run_dashboard_stop_command(argv[1:])
    if argv and argv[0] == "serve-dashboard":
        return run_serve_dashboard_command(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    autoresearch = AutoResearch(
        args.repo_path,
        assume_yes=args.yes,
        headless=args.headless,
        server_port=args.server_port,
    )
    autoresearch.resolve_setup_state(overwrite=args.overwrite)
    try:
        ensure_clean_tracking(autoresearch.repo_path)
        autoresearch.scaffold_if_needed()
        autoresearch.start_dashboard()
        autoresearch.review_scaffold_if_needed()
        if not autoresearch.did_scaffold:
            autoresearch.validate_pre_setup_config()
        autoresearch.open_session_branch()
        try:
            autoresearch.prepare_repo_setup()
            autoresearch.review_prepared_setup_if_needed()
        except Exception:
            autoresearch.fail_open_session()
            raise
        return autoresearch.run_session()
    except GitWorktreeError as error:
        print(error)
        return 1
    finally:
        try:
            autoresearch.stop_dashboard()
        except RuntimeError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
