from pathlib import Path

from easy_autoresearch.main import parse_metric, run_command


def test_parse_metric_returns_float() -> None:
    assert parse_metric("score: 1.25\n", r"^score:\s+([\d.]+)") == 1.25


def test_run_command_captures_success_and_metric(tmp_path: Path) -> None:
    result = run_command(
        "python -c \"print('score: 2.5')\"",
        cwd=tmp_path,
        timeout_seconds=5,
        metric_pattern=r"^score:\s+([\d.]+)",
    )

    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.metric_value == 2.5


def test_run_command_marks_failure(tmp_path: Path) -> None:
    result = run_command(
        "python -c \"import sys; sys.exit(3)\"",
        cwd=tmp_path,
        timeout_seconds=5,
    )

    assert result.status == "failed"
    assert result.exit_code == 3
