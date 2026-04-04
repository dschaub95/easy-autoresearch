import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

import easy_autoresearch.main as main_module
from easy_autoresearch.agent import AgentRunResult
from easy_autoresearch.config import (
    CONFIG_FILENAME,
    DB_FILENAME,
    PROMPTS_DIRNAME,
    STATE_DIRNAME,
    db_path,
    logs_dir,
)
from easy_autoresearch.main import CommandResult, main


def write_config_updates(
    repo_path: Path,
    *,
    run: str | None = None,
    metric_pattern: str | None = None,
    max_experiments: int | None = None,
    max_runs_per_experiment: int | None = None,
) -> None:
    config_file = repo_path / CONFIG_FILENAME
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    if run is not None:
        config["commands"]["run"] = run
    if metric_pattern is not None:
        config["commands"]["metric_pattern"] = metric_pattern
    if max_experiments is not None:
        config["experiments"]["max_experiments"] = max_experiments
    if max_runs_per_experiment is not None:
        config["experiments"]["max_runs_per_experiment"] = max_runs_per_experiment
    config_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def command_results(*results: CommandResult) -> Iterator[CommandResult]:
    yield from results


class NoOpSetupAgent:
    def __init__(self, session_id: str = "setup-session") -> None:
        self.session_id = session_id

    def run(self, prompt: str, **kwargs) -> AgentRunResult:
        repo_path = kwargs["output_path"].parents[2]
        config_file = repo_path / CONFIG_FILENAME
        if config_file.exists():
            config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            config["commands"]["metric_pattern"] = r"^metric:\s+([\d.]+)"
            config_file.write_text(
                yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
            )
        kwargs["output_path"].write_text('{"text":"setup"}\n', encoding="utf-8")
        kwargs["stderr_path"].write_text("", encoding="utf-8")
        return AgentRunResult(
            exit_code=0,
            output_path=kwargs["output_path"],
            stderr_path=kwargs["stderr_path"],
            session_id=self.session_id,
            text="setup",
            stderr="",
        )


def test_run_scaffolds_and_starts_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "target-repo"
    results = command_results(
        CommandResult(
            command="baseline",
            exit_code=0,
            stdout="metric: 3.0\n",
            stderr="",
            status="completed",
            metric_value=3.0,
        ),
        CommandResult(
            command="candidate",
            exit_code=0,
            stdout="metric: 3.0\n",
            stderr="",
            status="completed",
            metric_value=3.0,
        ),
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command", lambda *args, **kwargs: next(results)
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent",
        lambda config, repo_path: NoOpSetupAgent(),
    )
    responses = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    exit_code = main(["--headless", str(repo_path)])

    assert exit_code == 0
    assert (repo_path / CONFIG_FILENAME).exists()
    assert (repo_path / STATE_DIRNAME / DB_FILENAME).exists()
    assert logs_dir(repo_path).is_dir()
    assert (repo_path / STATE_DIRNAME / PROMPTS_DIRNAME / "codex-system.md").exists()

    config = yaml.safe_load((repo_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert config["project"]["name"] == "target-repo"
    assert config["commands"]["run"] == "uv run pytest"
    assert config["agent"]["model"] == "gpt-5.4-mini"
    assert config["agent"]["sandbox_mode"] == "workspace-write"
    assert config["agent"]["prompt_template"] == ".autoresearch/prompts/codex-system.md"

    with sqlite3.connect(db_path(repo_path)) as connection:
        sessions = connection.execute("SELECT status FROM sessions").fetchall()
        experiments = connection.execute(
            "SELECT kind, status, best_metric, agent_provider FROM experiments"
        ).fetchall()
        runs = connection.execute(
            "SELECT status, exit_code, metric_value, log_path FROM runs"
        ).fetchall()

    assert sessions == [("completed",)]
    assert experiments == [
        ("baseline", "completed", 3.0, None),
        ("candidate", "completed", 3.0, "codex"),
    ]
    assert runs == [
        ("completed", 0, 3.0, ".autoresearch/experiment-1-run-1.log"),
        ("completed", 0, 3.0, ".autoresearch/experiment-1-run-1.log"),
    ]


def test_setup_can_be_cancelled_for_config_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "target-repo"
    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent",
        lambda config, repo_path: NoOpSetupAgent(),
    )
    monkeypatch.setattr("builtins.input", lambda _: "n")

    exit_code = main(["--headless", str(repo_path)])

    assert exit_code == 0
    with sqlite3.connect(db_path(repo_path)) as connection:
        sessions = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()

    assert sessions == (0,)


def test_build_setup_prompt_forbids_hardcoded_hyperparameters_in_run_command(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "target-repo"
    autoresearch = main_module.AutoResearch(repo_path)

    prompt = autoresearch.build_setup_prompt("template")

    assert (
        "Keep commands.run free of tunable hyperparameters; change them in tracked "
        "code or config files instead." in prompt
    )


def test_run_starts_dashboard_server_and_prints_selected_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_path = tmp_path / "target-repo"
    results = command_results(
        CommandResult(
            command="baseline",
            exit_code=0,
            stdout="metric: 3.0\n",
            stderr="",
            status="completed",
            metric_value=3.0,
        ),
        CommandResult(
            command="candidate",
            exit_code=0,
            stdout="metric: 3.0\n",
            stderr="",
            status="completed",
            metric_value=3.0,
        ),
    )
    observed: dict[str, object] = {}

    class FakeDashboardServer:
        def __init__(
            self, *, repo_path: Path, host: str = "127.0.0.1", port: int = 8765
        ):
            observed["init"] = (repo_path, host, port)
            self.url = "http://127.0.0.1:8766"

        def start(self) -> None:
            observed["started"] = True

        def stop(self) -> None:
            observed["stopped"] = True

    monkeypatch.setattr(
        "easy_autoresearch.main.run_command", lambda *args, **kwargs: next(results)
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent",
        lambda config, repo_path: NoOpSetupAgent(),
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.DashboardServer",
        FakeDashboardServer,
    )
    responses = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    exit_code = main([str(repo_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert observed["init"] == (repo_path.resolve(), "127.0.0.1", 8765)
    assert observed["started"] is True
    assert observed["stopped"] is True
    assert "Dashboard available at http://127.0.0.1:8766" in captured.out


def test_dashboard_starts_immediately_after_scaffold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "target-repo"
    events: list[str] = []

    class FakeDashboardServer:
        def __init__(
            self, *, repo_path: Path, host: str = "127.0.0.1", port: int = 8765
        ):
            self.url = "http://127.0.0.1:8766"

        def start(self) -> None:
            events.append("dashboard_started")

        def stop(self) -> None:
            events.append("dashboard_stopped")

    def fake_config_prompt(_: str) -> str:
        events.append("config_prompt")
        return "n"

    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent",
        lambda config, repo_path: NoOpSetupAgent(),
    )
    monkeypatch.setattr("easy_autoresearch.main.DashboardServer", FakeDashboardServer)
    monkeypatch.setattr("builtins.input", fake_config_prompt)

    exit_code = main([str(repo_path)])

    assert exit_code == 0
    assert events == ["dashboard_started", "config_prompt", "dashboard_stopped"]


def test_dashboard_command_starts_server_without_running_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_path = tmp_path / "target-repo"
    observed: dict[str, object] = {}

    class FakeDashboardServer:
        def __init__(
            self, *, repo_path: Path, host: str = "127.0.0.1", port: int = 8765
        ):
            observed["init"] = (repo_path, host, port)
            self.url = "http://127.0.0.1:8766"
            self.reused_existing = False

        def start(self) -> None:
            observed["started"] = True

        def stop(self) -> None:
            observed["stopped"] = True

    monkeypatch.setattr("easy_autoresearch.main.DashboardServer", FakeDashboardServer)
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command",
        lambda *args, **kwargs: pytest.fail("run_command should not be called"),
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent",
        lambda *args, **kwargs: pytest.fail("create_agent should not be called"),
    )

    exit_code = main(["dashboard", str(repo_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert observed["init"] == (repo_path.resolve(), "127.0.0.1", 8765)
    assert observed["started"] is True
    assert "Dashboard available at http://127.0.0.1:8766" in captured.out
    assert not (repo_path / CONFIG_FILENAME).exists()
    assert not db_path(repo_path).exists()


def test_dashboard_command_reports_reused_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_path = tmp_path / "target-repo"

    class FakeDashboardServer:
        def __init__(
            self, *, repo_path: Path, host: str = "127.0.0.1", port: int = 8765
        ):
            self.url = "http://127.0.0.1:8766"
            self.reused_existing = True

        def start(self) -> None:
            return

        def stop(self) -> None:
            return

    monkeypatch.setattr("easy_autoresearch.main.DashboardServer", FakeDashboardServer)

    exit_code = main(["dashboard", str(repo_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Dashboard already running at http://127.0.0.1:8766" in captured.out


def test_dashboard_stop_command_stops_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_path = tmp_path / "target-repo"
    observed: dict[str, object] = {}

    class FakeDashboardServer:
        def __init__(
            self, *, repo_path: Path, host: str = "127.0.0.1", port: int = 8765
        ):
            observed["init"] = (repo_path, host, port)

        def stop(self) -> bool:
            observed["stopped"] = True
            return True

    monkeypatch.setattr("easy_autoresearch.main.DashboardServer", FakeDashboardServer)

    exit_code = main(["dashboard-stop", str(repo_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert observed["init"] == (repo_path.resolve(), "127.0.0.1", 8765)
    assert observed["stopped"] is True
    assert f"Dashboard stopped for {repo_path.resolve()}" in captured.out


def test_dashboard_stop_command_reports_missing_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_path = tmp_path / "target-repo"

    class FakeDashboardServer:
        def __init__(
            self, *, repo_path: Path, host: str = "127.0.0.1", port: int = 8765
        ):
            return

        def stop(self) -> bool:
            return False

    monkeypatch.setattr("easy_autoresearch.main.DashboardServer", FakeDashboardServer)

    exit_code = main(["dashboard-stop", str(repo_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"No running dashboard found for {repo_path.resolve()}" in captured.out


def test_run_uses_coding_agent_for_candidate_experiments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "project"
    baseline_results = command_results(
        CommandResult(
            command="baseline",
            exit_code=1,
            stdout="metric: 1.0\n",
            stderr="",
            status="failed",
            metric_value=1.0,
        ),
        CommandResult(
            command="candidate",
            exit_code=1,
            stdout="metric: 2.0\n",
            stderr="",
            status="failed",
            metric_value=2.0,
        ),
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command",
        lambda *args, **kwargs: next(baseline_results),
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent",
        lambda config, repo_path: NoOpSetupAgent(),
    )
    responses = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    main(["--headless", str(repo_path)])
    write_config_updates(
        repo_path,
        run="python -c \"print('metric: 3.0')\"",
        metric_pattern=r"^metric:\s+([\d.]+)",
        max_experiments=2,
        max_runs_per_experiment=2,
    )

    evaluation_results = command_results(
        CommandResult(
            command="run",
            exit_code=1,
            stdout="metric: 2.0\n",
            stderr="",
            status="failed",
            metric_value=2.0,
        ),
        CommandResult(
            command="run",
            exit_code=1,
            stdout="metric: 1.0\n",
            stderr="",
            status="failed",
            metric_value=1.0,
        ),
        CommandResult(
            command="run",
            exit_code=1,
            stdout="metric: 2.0\n",
            stderr="",
            status="failed",
            metric_value=2.0,
        ),
        CommandResult(
            command="run",
            exit_code=0,
            stdout="metric: 3.0\n",
            stderr="",
            status="completed",
            metric_value=3.0,
        ),
    )

    class FakeAgent:
        def __init__(self) -> None:
            self.session_id = "sess-123"
            self.prompts: list[str] = []
            self.calls = 0

        def run(self, prompt: str, **kwargs) -> AgentRunResult:
            self.prompts.append(prompt)
            self.calls += 1
            text = (
                "Hypothesis\nImprove the metric.\n\nApproach\nMake a change.\n\nFindings\nMetric improved."
                if "Summarize this experiment in plain text" in prompt
                else "working"
            )
            kwargs["output_path"].write_text('{"text":"ok"}\n', encoding="utf-8")
            kwargs["stderr_path"].write_text("", encoding="utf-8")
            return AgentRunResult(
                exit_code=0,
                output_path=kwargs["output_path"],
                stderr_path=kwargs["stderr_path"],
                session_id=self.session_id,
                text=text,
                stderr="",
            )

    fake_agent = FakeAgent()
    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent", lambda config, repo_path: fake_agent
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command",
        lambda *args, **kwargs: next(evaluation_results),
    )
    responses = iter(["c", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    exit_code = main(["--headless", str(repo_path)])

    assert exit_code == 0
    with sqlite3.connect(db_path(repo_path)) as connection:
        sessions = connection.execute("SELECT status FROM sessions").fetchall()
        experiments = connection.execute(
            "SELECT kind, status, best_metric, agent_provider, agent_session_id, summary_path, max_runs "
            "FROM experiments ORDER BY id"
        ).fetchall()
        agent_steps = connection.execute(
            "SELECT run_index, phase, status, agent_session_id, prompt, response_text, log_path, stderr_path "
            "FROM agent_steps ORDER BY id"
        ).fetchall()
        runs = connection.execute(
            "SELECT status, exit_code, metric_value, log_path FROM runs ORDER BY id"
        ).fetchall()

    assert sessions == [("failed",), ("completed",)]
    assert experiments == [
        ("baseline", "failed", 1.0, None, None, None, 1),
        (
            "candidate",
            "failed",
            2.0,
            "codex",
            "setup-session",
            ".autoresearch/logs/experiment-1-summary.md",
            1,
        ),
        ("baseline", "failed", 2.0, None, None, None, 1),
        (
            "candidate",
            "failed",
            2.0,
            "codex",
            "sess-123",
            ".autoresearch/logs/experiment-1-summary.md",
            2,
        ),
        (
            "candidate",
            "completed",
            3.0,
            "codex",
            "sess-123",
            ".autoresearch/logs/experiment-2-summary.md",
            2,
        ),
    ]
    assert runs == [
        ("failed", 1, 1.0, ".autoresearch/experiment-1-run-1.log"),
        ("failed", 1, 2.0, ".autoresearch/experiment-1-run-1.log"),
        ("failed", 1, 2.0, ".autoresearch/experiment-1-run-1.log"),
        ("failed", 1, 1.0, ".autoresearch/experiment-1-run-1.log"),
        ("failed", 1, 2.0, ".autoresearch/experiment-1-run-2.log"),
        ("completed", 0, 3.0, ".autoresearch/experiment-2-run-1.log"),
    ]
    assert [row[:4] for row in agent_steps[-3:]] == [
        (1, "planning", "completed", "sess-123"),
        (1, "execution", "completed", "sess-123"),
        (1, "issue_resolution", "completed", "sess-123"),
    ]
    assert all("Experiment 2, attempt 1, phase:" in row[4] for row in agent_steps[-3:])
    assert all(row[5] == "working" for row in agent_steps[-3:])
    assert agent_steps[-3][6].endswith("experiment-2-run-1.planning.agent.jsonl")
    assert agent_steps[-2][6].endswith("experiment-2-run-1.execution.agent.jsonl")
    assert agent_steps[-1][6].endswith(
        "experiment-2-run-1.issue_resolution.agent.jsonl"
    )
    assert "Hypothesis" in (
        repo_path / ".autoresearch" / "logs" / "experiment-2-summary.md"
    ).read_text(encoding="utf-8")
    assert fake_agent.calls == 11


def test_run_skips_repo_command_when_agent_phase_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "project"
    baseline_results = command_results(
        CommandResult(
            command="baseline",
            exit_code=1,
            stdout="metric: 1.0\n",
            stderr="",
            status="failed",
            metric_value=1.0,
        ),
        CommandResult(
            command="candidate",
            exit_code=1,
            stdout="metric: 1.0\n",
            stderr="",
            status="failed",
            metric_value=1.0,
        ),
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command",
        lambda *args, **kwargs: next(baseline_results),
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent",
        lambda config, repo_path: NoOpSetupAgent(),
    )
    responses = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    main(["--headless", str(repo_path)])
    write_config_updates(
        repo_path,
        run="python -c \"print('metric: 1.0')\"",
        metric_pattern=r"^metric:\s+([\d.]+)",
        max_experiments=1,
        max_runs_per_experiment=1,
    )

    evaluation_calls: list[str] = []

    def fake_run_command(*args, **kwargs) -> CommandResult:
        evaluation_calls.append(args[0])
        return CommandResult(
            command=args[0],
            exit_code=1,
            stdout="metric: 1.0\n",
            stderr="",
            status="failed",
            metric_value=1.0,
        )

    class FailingAgent:
        def __init__(self) -> None:
            self.session_id = "sess-456"
            self.calls = 0

        def run(self, prompt: str, **kwargs) -> AgentRunResult:
            self.calls += 1
            exit_code = 0 if self.calls == 1 else 1
            text = "plan" if self.calls == 1 else "execution failed"
            kwargs["output_path"].write_text('{"text":"ok"}\n', encoding="utf-8")
            kwargs["stderr_path"].write_text(
                "boom\n" if exit_code else "", encoding="utf-8"
            )
            return AgentRunResult(
                exit_code=exit_code,
                output_path=kwargs["output_path"],
                stderr_path=kwargs["stderr_path"],
                session_id=self.session_id,
                text=text,
                stderr="boom\n" if exit_code else "",
            )

    failing_agent = FailingAgent()
    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent", lambda config, repo_path: failing_agent
    )
    monkeypatch.setattr("easy_autoresearch.main.run_command", fake_run_command)
    responses = iter(["c", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    exit_code = main([str(repo_path)])

    assert exit_code == 1
    assert evaluation_calls == ["python -c \"print('metric: 1.0')\""]
    with sqlite3.connect(db_path(repo_path)) as connection:
        agent_steps = connection.execute(
            "SELECT phase, status, exit_code, response_text, stderr FROM agent_steps ORDER BY id"
        ).fetchall()
        runs = connection.execute(
            "SELECT status, exit_code, metric_value FROM runs ORDER BY id"
        ).fetchall()

    assert agent_steps[-2:] == [
        ("planning", "completed", 0, "plan", ""),
        ("execution", "failed", 1, "execution failed", "boom\n"),
    ]
    assert runs[-1] == ("failed", 1, None)


def test_run_overwrites_existing_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            command="initial-candidate",
            exit_code=1,
            stdout="metric: 7.0\n",
            stderr="",
            status="failed",
            metric_value=7.0,
        ),
        CommandResult(
            command="overwrite",
            exit_code=0,
            stdout="metric: 7.0\n",
            stderr="",
            status="completed",
            metric_value=7.0,
        ),
        CommandResult(
            command="overwrite-candidate",
            exit_code=0,
            stdout="metric: 7.0\n",
            stderr="",
            status="completed",
            metric_value=7.0,
        ),
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command",
        lambda *args, **kwargs: next(initial_results),
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent",
        lambda config, repo_path: NoOpSetupAgent(),
    )
    responses = iter(["y", "y", "y", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    main([str(repo_path)])
    stale_log = repo_path / STATE_DIRNAME / "stale.txt"
    stale_log.write_text("stale", encoding="utf-8")
    write_config_updates(
        repo_path,
        run="python -c \"print('metric: 7.0')\"",
        metric_pattern=r"^metric:\s+([\d.]+)",
    )

    exit_code = main(["--headless", "--overwrite", str(repo_path)])

    assert exit_code == 0
    assert not stale_log.exists()
    config = yaml.safe_load((repo_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert config["commands"]["run"] == "uv run pytest"


def test_main_defaults_to_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command",
        lambda *args, **kwargs: CommandResult(
            command="noop",
            exit_code=0,
            stdout="metric: 1.0\n",
            stderr="",
            status="completed",
            metric_value=1.0,
        ),
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent",
        lambda config, repo_path: NoOpSetupAgent(),
    )
    responses = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    exit_code = main(["--headless"])

    assert exit_code == 0
    assert (tmp_path / CONFIG_FILENAME).exists()
