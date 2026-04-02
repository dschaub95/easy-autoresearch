"""Basic tests so the test suite has at least one collected test."""

import sqlite3
from pathlib import Path

from easy_autoresearch import __version__
from easy_autoresearch.config import db_path
from easy_autoresearch.db import initialize_database


def test_version_is_non_empty_string() -> None:
    assert isinstance(__version__, str)
    assert __version__


def test_initialize_database_creates_agent_steps_table(tmp_path: Path) -> None:
    initialize_database(db_path(tmp_path))

    with sqlite3.connect(db_path(tmp_path)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert "agent_steps" in tables
