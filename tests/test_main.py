import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from easy_autoresearch.config import (
    CONFIG_FILENAME,
    DB_FILENAME,
    PROMPTS_DIRNAME,
    STATE_DIRNAME,
    db_path,
)
from easy_autoresearch.main import CommandResult, main


def write_config_updates(
    repo_path: Path,
    *,
    baseline: str | None = None,
    metric_pattern: str | None = None,
    max_experiments: int | None = None,
    max_runs_per_experiment: int | None = None,
) -> None:
    config_file = repo_path / CONFIG_FILENAME
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    if baseline is not None:
        config["commands"]["baseline"] = baseline
    config["commands"]["metric_pattern"] = metric_pattern
    if max_experiments is not None:
        config["experiments"]["max_experiments"] = max_experiments
    if max_runs_per_experiment is not None:
        config["experiments"]["max_runs_per_experiment"] = max_runs_per_experiment
    config_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def command_results(*results: CommandResult) -> Iterator[CommandResult]:
    yield from results


def test_run_scaffolds_and_starts_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_path = tmp_path / "target-repo"
    results = command_results(
        CommandResult(
            command="baseline",
            exit_code=0,
            stdout="metric: 3.0\n",
            stderr="",
            status="completed",
            metric_value=3.0,
        )
    )
    monkeypatch.setattr("easy_autoresearch.main.run_command", lambda *args, **kwargs: next(results))

    exit_code = main([str(repo_path)])

    assert exit_code == 0
    assert (repo_path / CONFIG_FILENAME).exists()
    assert (repo_path / STATE_DIRNAME / DB_FILENAME).exists()
    assert (repo_path / STATE_DIRNAME / PROMPTS_DIRNAME / "codex-system.md").exists()

    config = yaml.safe_load((repo_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert config["project"]["name"] == "target-repo"
    assert config["commands"]["baseline"] == "uv run pytest"
    assert config["codex"]["prompt_template"] == ".autoresearch/prompts/codex-system.md"

    with sqlite3.connect(db_path(repo_path)) as connection:
        sessions = connection.execute("SELECT status FROM sessions").fetchall()
        experiments = connection.execute(
            "SELECT kind, status, best_metric FROM experiments"
        ).fetchall()
        runs = connection.execute(
            "SELECT status, exit_code, metric_value, log_path FROM runs"
        ).fetchall()

    assert sessions == [("completed",)]
    assert experiments == [("baseline", "completed", 3.0)]
    assert runs == [("completed", 0, 3.0, ".autoresearch/experiment-1-run-1.log")]


def test_run_uses_multiple_experiments_and_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_path = tmp_path / "project"
    bootstrap_results = command_results(
        CommandResult(
            command="bootstrap",
            exit_code=1,
            stdout="",
            stderr="",
            status="failed",
            metric_value=None,
        )
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command",
        lambda *args, **kwargs: next(bootstrap_results),
    )
    main([str(repo_path)])
    write_config_updates(
        repo_path,
        baseline="python -c \"print('metric: 3.0')\"",
        metric_pattern=r"^metric:\s+([\d.]+)",
        max_experiments=2,
        max_runs_per_experiment=2,
    )

    continued_results = command_results(
        CommandResult(
            command="attempt-1",
            exit_code=1,
            stdout="metric: 1.0\n",
            stderr="",
            status="failed",
            metric_value=1.0,
        ),
        CommandResult(
            command="attempt-2",
            exit_code=1,
            stdout="metric: 2.0\n",
            stderr="",
            status="failed",
            metric_value=2.0,
        ),
        CommandResult(
            command="attempt-3",
            exit_code=0,
            stdout="metric: 3.0\n",
            stderr="",
            status="completed",
            metric_value=3.0,
        ),
    )
    monkeypatch.setattr("builtins.input", lambda _: "c")
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command",
        lambda *args, **kwargs: next(continued_results),
    )

    exit_code = main([str(repo_path)])

    assert exit_code == 0
    with sqlite3.connect(db_path(repo_path)) as connection:
        sessions = connection.execute("SELECT status FROM sessions").fetchall()
        experiments = connection.execute(
            "SELECT kind, status, best_metric FROM experiments ORDER BY id"
        ).fetchall()
        runs = connection.execute(
            "SELECT status, exit_code, metric_value, log_path FROM runs ORDER BY id"
        ).fetchall()

    assert sessions == [("failed",), ("completed",)]
    assert experiments == [
        ("baseline", "failed", None),
        ("baseline", "failed", 2.0),
        ("candidate", "completed", 3.0),
    ]
    assert runs == [
        ("failed", 1, None, ".autoresearch/experiment-1-run-1.log"),
        ("failed", 1, 1.0, ".autoresearch/experiment-1-run-1.log"),
        ("failed", 1, 2.0, ".autoresearch/experiment-1-run-2.log"),
        ("completed", 0, 3.0, ".autoresearch/experiment-2-run-1.log"),
    ]


def test_run_overwrites_existing_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_path = tmp_path / "project"
    initial_results = command_results(
        CommandResult(
            command="initial",
            exit_code=1,
            stdout="",
            stderr="",
            status="failed",
            metric_value=None,
        ),
        CommandResult(
            command="overwrite",
            exit_code=0,
            stdout="metric: 7.0\n",
            stderr="",
            status="completed",
            metric_value=7.0,
        ),
    )
    monkeypatch.setattr("easy_autoresearch.main.run_command", lambda *args, **kwargs: next(initial_results))
    main([str(repo_path)])
    stale_log = repo_path / STATE_DIRNAME / "stale.txt"
    stale_log.write_text("stale", encoding="utf-8")
    write_config_updates(
        repo_path,
        baseline="python -c \"print('metric: 7.0')\"",
        metric_pattern=r"^metric:\s+([\d.]+)",
    )

    exit_code = main(["--overwrite", str(repo_path)])

    assert exit_code == 0
    assert not stale_log.exists()
    config = yaml.safe_load((repo_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert config["commands"]["baseline"] == "uv run pytest"


def test_main_defaults_to_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command",
        lambda *args, **kwargs: CommandResult(
            command="noop",
            exit_code=0,
            stdout="",
            stderr="",
            status="completed",
            metric_value=None,
        ),
    )

    exit_code = main([])

    assert exit_code == 0
    assert (tmp_path / CONFIG_FILENAME).exists()
