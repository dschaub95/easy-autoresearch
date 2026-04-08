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
from easy_autoresearch.git import GitWorktreeError
from easy_autoresearch.main import CommandResult, main


def write_config_updates(
    repo_path: Path,
    *,
    run: str | None = None,
    metric_pattern: str | None = None,
    max_experiments: int | None = None,
    max_runs_per_experiment: int | None = None,
    runtime: int | float | str | None = None,
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
    if runtime is not None or "constraints" not in config:
        config.setdefault("constraints", {})
    if runtime is not None:
        config["constraints"]["runtime"] = runtime
    config_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def command_results(*results: CommandResult) -> Iterator[CommandResult]:
    yield from results


class NoOpSetupAgent:
    def __init__(self, session_id: str = "setup-session") -> None:
        self.session_id = session_id

    def run(self, prompt: str, **kwargs) -> AgentRunResult:
        repo_path = next(
            parent
            for parent in kwargs["output_path"].parents
            if (parent / CONFIG_FILENAME).exists()
        )
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


@pytest.fixture(autouse=True)
def fake_git_tracking(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state: dict[str, object] = {
        "discard_calls": 0,
        "restore_calls": 0,
        "commit_messages": [],
    }

    monkeypatch.setattr("easy_autoresearch.main.ensure_clean_tracking", lambda _: None)
    monkeypatch.setattr(
        "easy_autoresearch.main.has_uncommitted_changes", lambda _: True
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.current_head_sha", lambda _: "base-commit-sha"
    )

    def fake_discard(_: Path) -> None:
        state["discard_calls"] = int(state["discard_calls"]) + 1

    def fake_save(_: Path, snapshot_dir: Path) -> None:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / "tracked.patch").write_text("", encoding="utf-8")

    def fake_restore(_: Path, __: Path) -> None:
        state["restore_calls"] = int(state["restore_calls"]) + 1

    def fake_commit(_: Path, message: str) -> str:
        messages = state["commit_messages"]
        assert isinstance(messages, list)
        messages.append(message)
        return f"commit-{len(messages)}"

    monkeypatch.setattr(
        "easy_autoresearch.main.discard_uncommitted_changes", fake_discard
    )
    monkeypatch.setattr("easy_autoresearch.main.save_worktree_snapshot", fake_save)
    monkeypatch.setattr(
        "easy_autoresearch.main.restore_worktree_snapshot", fake_restore
    )
    monkeypatch.setattr("easy_autoresearch.main.commit_all_changes", fake_commit)
    return state


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
    assert config["constraints"] == {"runtime": None}

    with sqlite3.connect(db_path(repo_path)) as connection:
        sessions = connection.execute(
            "SELECT status, setup_commit_sha FROM sessions"
        ).fetchall()
        experiments = connection.execute(
            "SELECT kind, status, best_metric, agent_provider FROM experiments"
        ).fetchall()
        runs = connection.execute(
            "SELECT status, exit_code, metric_value, log_path FROM runs"
        ).fetchall()

    assert sessions == [("completed", "commit-1")]
    assert experiments == [
        ("baseline", "completed", 3.0, None),
        ("candidate", "completed", 3.0, "codex"),
    ]
    assert runs == [
        ("completed", 0, 3.0, ".autoresearch/logs/runs/experiment-1-run-1.log"),
        ("completed", 0, 3.0, ".autoresearch/logs/runs/experiment-1-run-1.log"),
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

    prompt = autoresearch.build_setup_prompt()

    assert (
        "Keep commands.run free of tunable hyperparameters; change them in tracked "
        "code or config files instead." in prompt
    )
    assert "template" not in prompt


def test_prepare_repo_setup_uses_latest_agent_message_for_setup_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_git_tracking: dict[str, object],
) -> None:
    repo_path = tmp_path / "target-repo"
    autoresearch = main_module.AutoResearch(repo_path, assume_yes=True, headless=True)
    autoresearch.scaffold_repo()

    class FakeSetupAgent:
        def __init__(self) -> None:
            self.session_id = "setup-session"
            self.calls: list[tuple[str, str]] = []

        def run(self, prompt: str, **kwargs) -> AgentRunResult:
            text_capture = kwargs.get("text_capture", "full")
            self.calls.append((prompt, text_capture))
            config_file = repo_path / CONFIG_FILENAME
            if config_file.exists():
                config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
                config["commands"]["metric_pattern"] = r"^metric:\s+([\d.]+)"
                config_file.write_text(
                    yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
                )
            kwargs["output_path"].write_text('{"text":"ok"}\n', encoding="utf-8")
            kwargs["stderr_path"].write_text("", encoding="utf-8")
            text = (
                "thinking\nfinal setup commit"
                if "commit message" in prompt.lower() and text_capture == "full"
                else (
                    "final setup commit"
                    if "commit message" in prompt.lower()
                    else "setup"
                )
            )
            return AgentRunResult(
                exit_code=0,
                output_path=kwargs["output_path"],
                stderr_path=kwargs["stderr_path"],
                session_id=self.session_id,
                text=text,
                stderr="",
            )

    fake_agent = FakeSetupAgent()
    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent",
        lambda config, repo_path: fake_agent,
    )

    autoresearch.prepare_repo_setup()

    assert fake_agent.calls == [
        (autoresearch.build_setup_prompt(), "full"),
        (main_module.build_setup_commit_message_prompt(), "latest"),
    ]
    assert fake_git_tracking["commit_messages"] == ["final setup commit"]


def test_parse_duration_to_seconds_supports_human_readable_values() -> None:
    assert main_module.parse_duration_to_seconds("30s") == 30
    assert main_module.parse_duration_to_seconds("5m") == 300
    assert main_module.parse_duration_to_seconds("1h30m") == 5400
    assert main_module.parse_duration_to_seconds("2m15s") == 135


def test_parse_duration_to_seconds_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="duration strings"):
        main_module.parse_duration_to_seconds("5 min")
    with pytest.raises(ValueError, match="largest to smallest"):
        main_module.parse_duration_to_seconds("30s1m")


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_git_tracking: dict[str, object],
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
    (repo_path / ".autoresearch" / "prompts" / "codex-system.md").write_text(
        "template marker",
        encoding="utf-8",
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
                "Refine evaluation workflow"
                if "Write the git commit message" in prompt
                else (
                    "Main idea\n- Improve the metric with a targeted code change.\n\nSteps taken\n- Updated the implementation.\n- Re-ran the evaluation command."
                    if "Summarize this experiment in plain text" in prompt
                    else "working"
                )
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
            "SELECT kind, status, best_metric, previous_best_metric, metric_improved, "
            "changes_discarded, commit_sha, agent_provider, agent_session_id, summary_path, max_runs "
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
        ("baseline", "failed", 1.0, None, None, None, None, None, None, None, 1),
        (
            "candidate",
            "failed",
            2.0,
            1.0,
            1,
            0,
            "commit-2",
            "codex",
            "setup-session",
            ".autoresearch/logs/summaries/experiment-1.md",
            1,
        ),
        ("baseline", "failed", 2.0, None, None, None, None, None, None, None, 1),
        (
            "candidate",
            "failed",
            2.0,
            2.0,
            0,
            1,
            None,
            "codex",
            "sess-123",
            ".autoresearch/logs/summaries/experiment-1.md",
            2,
        ),
        (
            "candidate",
            "completed",
            3.0,
            2.0,
            1,
            0,
            "commit-3",
            "codex",
            "sess-123",
            ".autoresearch/logs/summaries/experiment-2.md",
            2,
        ),
    ]
    assert runs == [
        ("failed", 1, 1.0, ".autoresearch/logs/runs/experiment-1-run-1.log"),
        ("failed", 1, 2.0, ".autoresearch/logs/runs/experiment-1-run-1.log"),
        ("failed", 1, 2.0, ".autoresearch/logs/runs/experiment-1-run-1.log"),
        ("failed", 1, 1.0, ".autoresearch/logs/runs/experiment-1-run-1.log"),
        ("failed", 1, 2.0, ".autoresearch/logs/runs/experiment-1-run-2.log"),
        ("completed", 0, 3.0, ".autoresearch/logs/runs/experiment-2-run-1.log"),
    ]
    assert [row[:4] for row in agent_steps[-4:-1]] == [
        (1, "planning", "completed", "sess-123"),
        (1, "execution", "completed", "sess-123"),
        (1, "issue_resolution", "completed", "sess-123"),
    ]
    assert agent_steps[-1][:4] == (1, "commit_message", "completed", "sess-123")
    assert agent_steps[-5][:4] == (0, "initial_planning", "completed", "sess-123")
    assert "Experiment 2, initial planning." in agent_steps[-5][4]
    assert "template marker" in agent_steps[-5][4]
    assert (
        "Start by carefully reading all summary markdown files under `.autoresearch/logs/summaries`."
        in agent_steps[-5][4]
    )
    assert "- Run stdout logs: `.autoresearch/logs/runs`" in agent_steps[-5][4]
    assert "- Agent transcripts: `.autoresearch/logs/agent`" in agent_steps[-5][4]
    assert (
        "- Agent stderr logs: `.autoresearch/logs/agent-stderr`" in agent_steps[-5][4]
    )
    assert "- SQLite state database: `.autoresearch/state.db`" in agent_steps[-5][4]
    assert all(
        "Experiment 2, attempt 1, phase:" in row[4] for row in agent_steps[-4:-1]
    )
    assert all(row[5] == "working" for row in agent_steps[-4:-1])
    assert agent_steps[-5][5] == "working"
    assert agent_steps[-5][6].endswith("logs/agent/experiment-2.initial_planning.jsonl")
    assert agent_steps[-4][6].endswith("logs/agent/experiment-2-run-1.planning.jsonl")
    assert agent_steps[-3][6].endswith("logs/agent/experiment-2-run-1.execution.jsonl")
    assert agent_steps[-2][6].endswith(
        "logs/agent/experiment-2-run-1.issue_resolution.jsonl"
    )
    summary_text = (
        repo_path / ".autoresearch" / "logs" / "summaries" / "experiment-2.md"
    ).read_text(encoding="utf-8")
    assert "Main idea" in summary_text
    assert "Steps taken" in summary_text
    assert "Resulting metric: 3.0" in summary_text
    assert "Previous best metric: 2.0" in summary_text
    assert "Metric improved: yes" in summary_text
    assert "Changes discarded: no" in summary_text
    assert fake_agent.calls == 14
    assert sum("template marker" in prompt for prompt in fake_agent.prompts) == 2
    assert "Previous best metric: 2.0." in agent_steps[-5][4]
    assert (
        "Output only the commit message text. Include only changes you actually "
        "made in this experiment session." in agent_steps[-1][4]
    )
    assert fake_git_tracking["commit_messages"] == [
        "setup",
        "setup",
        "Refine evaluation workflow",
    ]


def test_runtime_constraint_is_included_in_planning_prompts(tmp_path: Path) -> None:
    repo_path = tmp_path / "project"
    autoresearch = main_module.AutoResearch(repo_path, assume_yes=True, headless=True)
    autoresearch.scaffold_repo()
    write_config_updates(
        repo_path,
        runtime=1.1,
    )
    autoresearch.config = main_module.load_config(repo_path)
    autoresearch.baseline_runtime_seconds = 10.0
    autoresearch.runtime_cap_seconds = 11.0

    initial_prompt = autoresearch.build_initial_planning_prompt(
        template="template marker",
        experiment_index=1,
        previous_best_metric=2.0,
    )
    phase_prompt = autoresearch.build_agent_phase_prompt(1, 1, "planning")

    assert "Hard runtime constraint" in initial_prompt
    assert "Current runtime cap: 11.000s." in initial_prompt
    assert "Hard runtime constraint" in phase_prompt


def test_runtime_constraint_blocks_metric_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_git_tracking: dict[str, object],
) -> None:
    repo_path = tmp_path / "project"
    baseline_results = command_results(
        CommandResult(
            command="baseline",
            exit_code=0,
            stdout="metric: 2.0\n",
            stderr="",
            status="completed",
            metric_value=2.0,
            runtime_seconds=10.0,
        ),
        CommandResult(
            command="candidate",
            exit_code=0,
            stdout="metric: 2.0\n",
            stderr="",
            status="completed",
            metric_value=2.0,
            runtime_seconds=10.0,
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
        max_experiments=1,
        max_runs_per_experiment=1,
        runtime=1.1,
    )

    evaluation_results = command_results(
        CommandResult(
            command="baseline",
            exit_code=0,
            stdout="metric: 2.0\n",
            stderr="",
            status="completed",
            metric_value=2.0,
            runtime_seconds=10.0,
        ),
        CommandResult(
            command="candidate",
            exit_code=0,
            stdout="metric: 3.0\n",
            stderr="",
            status="completed",
            metric_value=3.0,
            runtime_seconds=12.0,
        ),
    )

    class FakeAgent:
        def __init__(self) -> None:
            self.session_id = "sess-runtime"

        def run(self, prompt: str, **kwargs) -> AgentRunResult:
            text = (
                "Main idea\n- Attempted a higher-scoring but slower variant.\n\nSteps taken\n- Updated the experiment."
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

    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent", lambda config, repo_path: FakeAgent()
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command",
        lambda *args, **kwargs: next(evaluation_results),
    )
    responses = iter(["c", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    exit_code = main(["--headless", str(repo_path)])

    assert exit_code == 1
    summary_text = (
        repo_path / ".autoresearch" / "logs" / "summaries" / "experiment-1.md"
    ).read_text(encoding="utf-8")
    assert "Runtime cap: 11.000s" in summary_text
    assert "Runtime constraint satisfied: no" in summary_text
    with sqlite3.connect(db_path(repo_path)) as connection:
        experiment = connection.execute(
            "SELECT best_metric, metric_improved, changes_discarded, commit_sha "
            "FROM experiments ORDER BY id DESC LIMIT 1"
        ).fetchone()
        run = connection.execute(
            "SELECT status, metric_value, stderr FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert experiment == (None, 0, 1, None)
    assert run[0] == "failed"
    assert run[1] == 3.0
    assert "Runtime constraint violated." in run[2]
    assert fake_git_tracking["commit_messages"] == ["setup"]


def test_scaffolded_codex_system_prompt_is_empty(tmp_path: Path) -> None:
    repo_path = tmp_path / "project"

    autoresearch = main_module.AutoResearch(repo_path)
    autoresearch.scaffold_repo()

    assert (repo_path / ".autoresearch" / "prompts" / "codex-system.md").read_text(
        encoding="utf-8"
    ) == ""


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
            exit_code = 0 if self.calls < 3 else 1
            text = (
                "initial plan"
                if self.calls == 1
                else "plan"
                if self.calls == 2
                else "execution failed"
            )
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

    assert agent_steps[-3:] == [
        ("initial_planning", "completed", 0, "initial plan", ""),
        ("planning", "completed", 0, "plan", ""),
        ("execution", "failed", 1, "execution failed", "boom\n"),
    ]
    assert runs[-1] == ("failed", 1, None)


def test_non_improving_experiment_summary_marks_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_git_tracking: dict[str, object],
) -> None:
    repo_path = tmp_path / "project"
    baseline_results = command_results(
        CommandResult(
            command="baseline",
            exit_code=1,
            stdout="metric: 2.0\n",
            stderr="",
            status="failed",
            metric_value=2.0,
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
        run="python -c \"print('metric: 1.5')\"",
        metric_pattern=r"^metric:\s+([\d.]+)",
        max_experiments=1,
        max_runs_per_experiment=1,
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
            stdout="metric: 1.5\n",
            stderr="",
            status="failed",
            metric_value=1.5,
        ),
    )

    class FakeAgent:
        def __init__(self) -> None:
            self.session_id = "sess-789"

        def run(self, prompt: str, **kwargs) -> AgentRunResult:
            text = (
                "Main idea\n- Try a weaker variant.\n\nSteps taken\n- Adjusted the experiment."
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

    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent", lambda config, repo_path: FakeAgent()
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.run_command",
        lambda *args, **kwargs: next(evaluation_results),
    )
    responses = iter(["c", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    exit_code = main(["--headless", str(repo_path)])

    assert exit_code == 1
    summary_text = (
        repo_path / ".autoresearch" / "logs" / "summaries" / "experiment-1.md"
    ).read_text(encoding="utf-8")
    assert "Resulting metric: 1.5" in summary_text
    assert "Previous best metric: 2.0" in summary_text
    assert "Metric improved: no" in summary_text
    assert "Changes discarded: yes" in summary_text
    assert fake_git_tracking["commit_messages"] == ["setup"]
    with sqlite3.connect(db_path(repo_path)) as connection:
        experiment = connection.execute(
            "SELECT previous_best_metric, metric_improved, changes_discarded, commit_sha "
            "FROM experiments ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert experiment == (2.0, 0, 1, None)


def test_run_session_fails_fast_for_dirty_git_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "project"

    def failing_tracking_check(_: Path) -> None:
        raise GitWorktreeError(
            "Autoresearch requires a clean git worktree before starting."
        )

    monkeypatch.setattr(
        "easy_autoresearch.main.create_agent",
        lambda config, repo_path: NoOpSetupAgent(),
    )
    monkeypatch.setattr(
        "easy_autoresearch.main.ensure_clean_tracking",
        failing_tracking_check,
    )
    responses = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    exit_code = main(["--headless", str(repo_path)])

    assert exit_code == 1
    assert not db_path(repo_path).exists()


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


def test_yes_flag_skips_interactive_prompts(
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
    monkeypatch.setattr(
        "builtins.input",
        lambda _: pytest.fail("input should not be called when --yes is set"),
    )

    exit_code = main(["--headless", "--yes", str(repo_path)])

    assert exit_code == 0
    assert (repo_path / CONFIG_FILENAME).exists()


def test_yes_flag_still_prompts_for_existing_setup_choice(
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
    responses = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    main(["--headless", str(repo_path)])

    prompted: list[str] = []

    def fake_input(prompt: str) -> str:
        prompted.append(prompt)
        return "c"

    monkeypatch.setattr("builtins.input", fake_input)

    exit_code = main(["--headless", "--yes", str(repo_path)])

    assert exit_code == 0
    assert prompted == [
        f"Existing easy-autoresearch setup found in {repo_path.resolve()}. "
        "Continue with it or overwrite it? [c/o]: "
    ]


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
