"""Main entrypoint and session workflow for easy-autoresearch."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Literal

from .agent import CodingAgent
from .app.server import DashboardServer
from .codex import Codex
from .config import (
    CODEX_SYSTEM_PROMPT,
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


AgentPhase = Literal["planning", "execution", "issue_resolution"]


@dataclass(slots=True)
class AgentPhaseResult:
    phase: AgentPhase
    prompt: str
    status: str
    exit_code: int | None
    log_path: Path
    stderr_path: Path
    response_text: str
    stderr: str
    agent_session_id: str | None


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
        self.headless = headless
        self.server_host = server_host
        self.server_port = server_port
        self.dashboard_server: DashboardServer | None = None
        self.did_scaffold = False

    def scaffold_repo(self) -> None:
        self.repo_path.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
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

    def resolve_setup_state(self, *, overwrite: bool = False) -> None:
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

    def scaffold_if_needed(self) -> None:
        if not self.has_existing_setup():
            self.scaffold_repo()
        elif self.config is None:
            self.config = load_config(self.repo_path)

    def review_scaffold_if_needed(self) -> None:
        if self.did_scaffold and not prompt_for_config_review(self.config_file):
            print("Cancelled after scaffolding so you can adjust the config.")
            self.ready_to_start = False

    def prepare_repo_setup(self) -> None:
        if not self.did_scaffold or not self.ready_to_start:
            return
        template = self.load_prompt_template()
        result = create_agent(self.require_config(), self.repo_path).run(
            self.build_setup_prompt(template),
            output_path=self.logs_dir / "setup.agent.jsonl",
            stderr_path=self.logs_dir / "setup.agent.stderr.log",
            timeout_seconds=self.require_config().session.max_duration_seconds,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                "Agent setup failed. Check "
                f"{result.output_path.relative_to(self.repo_path)} and "
                f"{result.stderr_path.relative_to(self.repo_path)}."
            )
        self.config = load_config(self.repo_path)

    def review_prepared_setup_if_needed(self) -> None:
        if (
            self.did_scaffold
            and self.ready_to_start
            and not prompt_for_setup_review(self.repo_path)
        ):
            print("Cancelled after setup so you can review the changes.")
            self.ready_to_start = False

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

    def stop_dashboard(self) -> None:
        if self.dashboard_server is not None:
            self.dashboard_server.stop()
            self.dashboard_server = None

    def run_session(self) -> int:
        if not self.ready_to_start:
            return 0
        config = self.require_config()
        self.validate_config()
        print(f"Starting autoresearch in {self.repo_path}")
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
                max_runs=config.experiments.max_runs_per_experiment,
                status="running",
                agent_provider=None,
                created_at=baseline_started_at,
                updated_at=baseline_started_at,
            )
            baseline_status, _, baseline_run_count = self.run_baseline_experiment(
                connection,
                experiment_id=baseline_experiment_id,
                experiment_index=1,
            )
            total_run_count += baseline_run_count
            if (
                baseline_status == "completed"
                and config.experiments.max_experiments == 0
            ):
                session_status = "completed"

            for experiment_index in range(1, config.experiments.max_experiments + 1):
                experiment_count += 1
                experiment_started_at = utc_now()
                experiment_id = create_experiment(
                    connection,
                    session_id=session_id,
                    kind="candidate",
                    description=f"Candidate experiment {experiment_index}",
                    max_runs=config.experiments.max_runs_per_experiment,
                    status="running",
                    agent_provider=config.agent.provider,
                    created_at=experiment_started_at,
                    updated_at=experiment_started_at,
                )
                print(f"Running candidate experiment {experiment_index}")
                experiment_status, _, run_count = self.run_agent_experiment(
                    connection,
                    experiment_id=experiment_id,
                    experiment_index=experiment_index,
                )
                total_run_count += run_count
                if experiment_status == "completed":
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
    ) -> tuple[str, float | None, int]:
        config = self.require_config()
        best_metric: float | None = None
        experiment_status = "failed"
        run_count = 0
        for run_index in range(1, config.experiments.max_runs_per_experiment + 1):
            run_count += 1
            print(
                f"Baseline run {run_index}/{config.experiments.max_runs_per_experiment}"
            )
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
        return experiment_status, best_metric, run_count

    def run_agent_experiment(
        self,
        connection,
        *,
        experiment_id: int,
        experiment_index: int,
    ) -> tuple[str, float | None, int]:
        config = self.require_config()
        agent = create_agent(config, self.repo_path)
        template = self.load_prompt_template()
        best_metric: float | None = None
        experiment_status = "failed"
        run_count = 0
        last_agent_log_path: Path | None = None
        last_agent_stderr_path: Path | None = None
        last_result: CommandResult | None = None
        for run_index in range(1, config.experiments.max_runs_per_experiment + 1):
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
                template=template,
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
                result = CommandResult(
                    command=result.command,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=(
                        f"{result.stderr}\nNo metric matched pattern "
                        f"{config.commands.metric_pattern!r}."
                    ).strip(),
                    status="failed",
                    metric_value=None,
                )
            last_result = result
            run_log_path = (
                self.state_dir / f"experiment-{experiment_index}-run-{run_index}.log"
            )
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
            if result.metric_value is not None and (
                best_metric is None or result.metric_value > best_metric
            ):
                best_metric = result.metric_value
            if result.status == "completed":
                experiment_status = "completed"
                break

        summary_text = self.build_fallback_summary(last_result)
        summary_path = self.logs_dir / f"experiment-{experiment_index}-summary.md"
        print(f"Writing summary for experiment {experiment_index}")
        if agent.session_id:
            summary_result = agent.run(
                self.build_summary_prompt(last_result),
                output_path=self.logs_dir
                / f"experiment-{experiment_index}-summary.agent.jsonl",
                stderr_path=self.logs_dir
                / f"experiment-{experiment_index}-summary.agent.stderr.log",
                timeout_seconds=config.session.max_duration_seconds,
            )
            if summary_result.text:
                summary_text = summary_result.text
        summary_path.write_text(summary_text, encoding="utf-8")
        update_experiment(
            connection,
            experiment_id=experiment_id,
            status=experiment_status,
            updated_at=utc_now(),
            best_metric=best_metric,
            agent_session_id=agent.session_id,
            summary=summary_text,
            summary_path=str(summary_path.relative_to(self.repo_path)),
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
        return experiment_status, best_metric, run_count

    def run_agent_phases(
        self,
        connection,
        *,
        agent: CodingAgent,
        template: str,
        experiment_id: int,
        experiment_index: int,
        run_index: int,
    ) -> list[AgentPhaseResult]:
        config = self.require_config()
        phase_results: list[AgentPhaseResult] = []
        for phase in ("planning", "execution", "issue_resolution"):
            print(f"Agent phase: {phase}")
            prompt = self.build_agent_phase_prompt(
                template, experiment_index, run_index, phase
            )
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
            output_path = (
                self.logs_dir
                / f"experiment-{experiment_index}-run-{run_index}.{phase}.agent.jsonl"
            )
            stderr_path = (
                self.logs_dir
                / f"experiment-{experiment_index}-run-{run_index}.{phase}.agent.stderr.log"
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

    def build_agent_phase_prompt(
        self,
        template: str,
        experiment_index: int,
        run_index: int,
        phase: AgentPhase,
    ) -> str:
        config = self.require_config()
        phase_instructions = {
            "planning": (
                "Inspect the repository and produce a concise implementation plan for "
                "the most promising change to make this experiment a success. Do not edit files yet."
            ),
            "execution": (
                "Implement the planned change in this same session. Make concrete "
                "modifications to repository files so the workspace reflects the "
                "experiment before evaluation. Leave the workspace runnable, but do "
                "not run the final evaluation command."
            ),
            "issue_resolution": (
                "Review the changes for likely issues, fix anything necessary, and "
                "leave the workspace ready for evaluation. Do not run the final "
                "evaluation command."
            ),
        }
        return "\n\n".join(
            part
            for part in [
                template,
                f"Experiment {experiment_index}, attempt {run_index}, phase: {phase}.",
                phase_instructions[phase],
                f"The evaluation command is `{config.commands.run}`.",
                (
                    "Do not write the final experiment summary yet. The harness will run "
                    "the evaluation command only after all three phases succeed."
                ),
            ]
            if part
        )

    def build_setup_prompt(self, template: str) -> str:
        return "\n\n".join(
            part
            for part in [
                template,
                "Prepare this repository for repeated local optimization of its end-to-end workflow.",
                (
                    "Create or adjust a clear local run command that generally includes "
                    "both training and evaluation. Ensure it prints at least one "
                    "meaningful scalar metric to stdout, and update autoresearch.yaml "
                    "so commands.run is that command and commands.metric_pattern "
                    "matches that metric."
                ),
                (
                    "Make each run reproducible: rerunning commands.run should "
                    "generally produce the same result, especially through explicit "
                    "seeding where needed."
                ),
                (
                    "Keep commands.run free of tunable hyperparameters; change them in "
                    "tracked code or config files instead."
                ),
                (
                    "Do not optimize the repo in this step; only make the minimum setup "
                    "changes needed for reliable optimization."
                ),
            ]
            if part
        )

    def build_summary_prompt(self, result: CommandResult | None) -> str:
        return "\n\n".join(
            [
                "Summarize this experiment in plain text under the headings "
                "Hypothesis, Approach, Findings.",
                f"Evaluation status: {result.status if result else 'not run'}",
                f"Metric: {result.metric_value if result else 'n/a'}",
                f"Stdout:\n{result.stdout if result else ''}",
                f"Stderr:\n{result.stderr if result else ''}",
            ]
        )

    def build_fallback_summary(self, result: CommandResult | None) -> str:
        return "\n".join(
            [
                "Hypothesis",
                "No agent summary was captured.",
                "",
                "Approach",
                "The harness executed the configured experiment flow.",
                "",
                "Findings",
                (
                    f"Status: {result.status}, metric: {result.metric_value}"
                    if result is not None
                    else "No evaluation command completed."
                ),
            ]
        )

    def load_prompt_template(self) -> str:
        prompt_path = self.repo_path / self.require_config().agent.prompt_template
        return prompt_path.read_text(encoding="utf-8").strip()

    def require_config(self) -> AutoResearchConfig:
        if self.config is None:
            raise RuntimeError("AutoResearch config has not been loaded")
        return self.config

    def validate_config(self) -> None:
        config = self.require_config()
        if config.experiments.max_experiments > 0:
            if not config.commands.run:
                raise ValueError("commands.run must be configured")
            if not config.commands.metric_pattern:
                raise ValueError(
                    "commands.metric_pattern must be configured for agent experiments"
                )


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

    if timed_out:
        return CommandResult(
            command=command,
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            status="timed_out",
            metric_value=metric_value or parse_metric(stdout, metric_pattern),
        )

    status = "completed" if exit_code == 0 else "failed"
    return CommandResult(
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        status=status,
        metric_value=metric_value or parse_metric(stdout, metric_pattern),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="easy-autoresearch")
    parser.add_argument("repo_path", nargs="?", type=Path, default=Path("."))
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "dashboard":
        return run_dashboard_command(argv[1:])
    if argv and argv[0] == "dashboard-stop":
        return run_dashboard_stop_command(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    autoresearch = AutoResearch(
        args.repo_path,
        headless=args.headless,
        server_port=args.server_port,
    )
    autoresearch.resolve_setup_state(overwrite=args.overwrite)
    autoresearch.scaffold_if_needed()
    autoresearch.start_dashboard()
    try:
        autoresearch.review_scaffold_if_needed()
        autoresearch.prepare_repo_setup()
        autoresearch.review_prepared_setup_if_needed()
        return autoresearch.run_session()
    finally:
        try:
            autoresearch.stop_dashboard()
        except RuntimeError:
            pass
