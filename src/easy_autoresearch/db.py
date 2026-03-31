"""SQLite storage for sessions, experiments, and runs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_path TEXT NOT NULL,
    max_duration_seconds INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    max_runs INTEGER NOT NULL,
    status TEXT NOT NULL,
    best_metric REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    run_index INTEGER NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    stdout TEXT NOT NULL,
    stderr TEXT NOT NULL,
    metric_value REAL,
    log_path TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    created_at TEXT NOT NULL
);
"""

def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def initialize_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as connection:
        connection.executescript(SCHEMA)


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
    started_at: str,
    created_at: str,
) -> int:
    return insert_row(
        connection,
        """
        INSERT INTO sessions (
            repo_path, max_duration_seconds, status, started_at, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (repo_path, max_duration_seconds, status, started_at, created_at),
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
    created_at: str,
    updated_at: str,
) -> int:
    return insert_row(
        connection,
        """
        INSERT INTO experiments (
            session_id, kind, description, max_runs, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, kind, description, max_runs, status, created_at, updated_at),
    )


def update_experiment(
    connection: sqlite3.Connection,
    *,
    experiment_id: int,
    status: str,
    updated_at: str,
    best_metric: float | None = None,
) -> None:
    execute(
        connection,
        """
        UPDATE experiments
        SET status = ?, updated_at = ?, best_metric = ?
        WHERE id = ?
        """,
        (status, updated_at, best_metric, experiment_id),
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
        (status, exit_code, stdout, stderr, metric_value, log_path, finished_at, run_id),
    )
