"""Write-side database helpers."""

from __future__ import annotations

import sqlite3


def insert_row(
    connection: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> int:
    cursor = connection.execute(sql, params)
    return int(cursor.lastrowid)


def execute(
    connection: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> None:
    connection.execute(sql, params)


def create_session(
    connection: sqlite3.Connection,
    *,
    repo_path: str,
    max_duration_seconds: int,
    status: str,
    setup_commit_sha: str | None = None,
    started_at: str,
    created_at: str,
) -> int:
    return insert_row(
        connection,
        """
        INSERT INTO sessions (
            repo_path, max_duration_seconds, status, setup_commit_sha, started_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            repo_path,
            max_duration_seconds,
            status,
            setup_commit_sha,
            started_at,
            created_at,
        ),
    )


def finish_session(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    status: str,
    finished_at: str,
) -> None:
    execute(
        connection,
        """
        UPDATE sessions
        SET status = ?, finished_at = ?
        WHERE id = ?
        """,
        (status, finished_at, session_id),
    )


def create_experiment(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    kind: str,
    description: str,
    max_runs: int,
    status: str,
    agent_provider: str | None,
    previous_best_metric: float | None = None,
    base_commit_sha: str | None = None,
    created_at: str,
    updated_at: str,
) -> int:
    return insert_row(
        connection,
        """
        INSERT INTO experiments (
            session_id, kind, description, max_runs, status, agent_provider,
            previous_best_metric, base_commit_sha, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            kind,
            description,
            max_runs,
            status,
            agent_provider,
            previous_best_metric,
            base_commit_sha,
            created_at,
            updated_at,
        ),
    )


def update_experiment(
    connection: sqlite3.Connection,
    *,
    experiment_id: int,
    status: str,
    updated_at: str,
    best_metric: float | None = None,
    previous_best_metric: float | None = None,
    metric_improved: bool | None = None,
    changes_discarded: bool | None = None,
    agent_session_id: str | None = None,
    commit_sha: str | None = None,
    base_commit_sha: str | None = None,
    summary: str | None = None,
    summary_path: str | None = None,
    agent_log_path: str | None = None,
    agent_stderr_path: str | None = None,
) -> None:
    execute(
        connection,
        """
        UPDATE experiments
        SET status = ?, updated_at = ?, best_metric = ?, previous_best_metric = ?,
            metric_improved = ?, changes_discarded = ?, agent_session_id = ?,
            commit_sha = ?, base_commit_sha = ?, summary = ?, summary_path = ?,
            agent_log_path = ?, agent_stderr_path = ?
        WHERE id = ?
        """,
        (
            status,
            updated_at,
            best_metric,
            previous_best_metric,
            int(metric_improved) if metric_improved is not None else None,
            int(changes_discarded) if changes_discarded is not None else None,
            agent_session_id,
            commit_sha,
            base_commit_sha,
            summary,
            summary_path,
            agent_log_path,
            agent_stderr_path,
            experiment_id,
        ),
    )


def create_run(
    connection: sqlite3.Connection,
    *,
    experiment_id: int,
    run_index: int,
    command: str,
    status: str,
    started_at: str,
    created_at: str,
) -> int:
    return insert_row(
        connection,
        """
        INSERT INTO runs (
            experiment_id, run_index, command, status, stdout, stderr,
            started_at, created_at
        ) VALUES (?, ?, ?, ?, '', '', ?, ?)
        """,
        (experiment_id, run_index, command, status, started_at, created_at),
    )


def finish_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    status: str,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    metric_value: float | None,
    log_path: str | None,
    finished_at: str,
) -> None:
    execute(
        connection,
        """
        UPDATE runs
        SET status = ?, exit_code = ?, stdout = ?, stderr = ?, metric_value = ?,
            log_path = ?, finished_at = ?
        WHERE id = ?
        """,
        (
            status,
            exit_code,
            stdout,
            stderr,
            metric_value,
            log_path,
            finished_at,
            run_id,
        ),
    )


def create_agent_step(
    connection: sqlite3.Connection,
    *,
    experiment_id: int,
    run_index: int,
    phase: str,
    prompt: str,
    status: str,
    started_at: str,
    created_at: str,
) -> int:
    return insert_row(
        connection,
        """
        INSERT INTO agent_steps (
            experiment_id, run_index, phase, prompt, status, response_text, stderr,
            started_at, created_at
        ) VALUES (?, ?, ?, ?, ?, '', '', ?, ?)
        """,
        (
            experiment_id,
            run_index,
            phase,
            prompt,
            status,
            started_at,
            created_at,
        ),
    )


def finish_agent_step(
    connection: sqlite3.Connection,
    *,
    step_id: int,
    status: str,
    exit_code: int | None,
    agent_session_id: str | None,
    response_text: str,
    stderr: str,
    log_path: str | None,
    stderr_path: str | None,
    finished_at: str,
) -> None:
    execute(
        connection,
        """
        UPDATE agent_steps
        SET status = ?, exit_code = ?, agent_session_id = ?, response_text = ?,
            stderr = ?, log_path = ?, stderr_path = ?, finished_at = ?
        WHERE id = ?
        """,
        (
            status,
            exit_code,
            agent_session_id,
            response_text,
            stderr,
            log_path,
            stderr_path,
            finished_at,
            step_id,
        ),
    )
