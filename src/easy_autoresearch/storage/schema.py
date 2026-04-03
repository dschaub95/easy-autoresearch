"""SQLite schema and database initialization."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from easy_autoresearch.storage.connection import connect

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
    agent_provider TEXT,
    agent_session_id TEXT,
    summary TEXT,
    summary_path TEXT,
    agent_log_path TEXT,
    agent_stderr_path TEXT,
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

CREATE TABLE IF NOT EXISTS agent_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    run_index INTEGER NOT NULL,
    phase TEXT NOT NULL,
    agent_session_id TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    prompt TEXT NOT NULL,
    response_text TEXT NOT NULL,
    stderr TEXT NOT NULL,
    log_path TEXT,
    stderr_path TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    created_at TEXT NOT NULL
);
"""


def ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    existing_columns = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})")
    }
    if column_name not in existing_columns:
        connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
        )


def initialize_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as connection:
        connection.executescript(SCHEMA)
        ensure_column(connection, "experiments", "agent_provider", "TEXT")
        ensure_column(connection, "experiments", "agent_session_id", "TEXT")
        ensure_column(connection, "experiments", "summary", "TEXT")
        ensure_column(connection, "experiments", "summary_path", "TEXT")
        ensure_column(connection, "experiments", "agent_log_path", "TEXT")
        ensure_column(connection, "experiments", "agent_stderr_path", "TEXT")
