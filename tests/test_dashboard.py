from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easy_autoresearch.app.server import DashboardServer, create_dashboard_app
from easy_autoresearch.config import db_path
from easy_autoresearch.storage import (
    connect,
    create_agent_step,
    create_experiment,
    create_run,
    create_session,
    initialize_database,
)


def test_dashboard_renders_latest_session(tmp_path: Path) -> None:
    initialize_database(db_path(tmp_path))
    with connect(db_path(tmp_path)) as connection:
        session_id = create_session(
            connection,
            repo_path=str(tmp_path),
            max_duration_seconds=60,
            status="running",
            started_at="2026-04-03T10:00:00+00:00",
            created_at="2026-04-03T10:00:00+00:00",
        )
        experiment_id = create_experiment(
            connection,
            session_id=session_id,
            kind="candidate",
            description="Candidate experiment 1",
            max_runs=1,
            status="running",
            agent_provider="codex",
            created_at="2026-04-03T10:00:00+00:00",
            updated_at="2026-04-03T10:00:00+00:00",
        )
        create_run(
            connection,
            experiment_id=experiment_id,
            run_index=1,
            command="uv run pytest",
            status="running",
            started_at="2026-04-03T10:00:01+00:00",
            created_at="2026-04-03T10:00:01+00:00",
        )

    client = TestClient(create_dashboard_app(repo_path=tmp_path))
    response = client.get("/")

    assert response.status_code == 200
    assert "Sessions" in response.text
    assert "Experiments" in response.text
    assert "Runs" in response.text
    assert "Candidate experiment 1" in response.text


def test_current_session_api_returns_snapshot(tmp_path: Path) -> None:
    initialize_database(db_path(tmp_path))
    with connect(db_path(tmp_path)) as connection:
        session_id = create_session(
            connection,
            repo_path=str(tmp_path),
            max_duration_seconds=60,
            status="running",
            started_at="2026-04-03T10:00:00+00:00",
            created_at="2026-04-03T10:00:00+00:00",
        )
        experiment_id = create_experiment(
            connection,
            session_id=session_id,
            kind="candidate",
            description="Candidate experiment 1",
            max_runs=1,
            status="running",
            agent_provider="codex",
            created_at="2026-04-03T10:00:00+00:00",
            updated_at="2026-04-03T10:00:00+00:00",
        )
        create_agent_step(
            connection,
            experiment_id=experiment_id,
            run_index=0,
            phase="initial_planning",
            prompt="initial plan",
            status="running",
            started_at="2026-04-03T10:00:01+00:00",
            created_at="2026-04-03T10:00:01+00:00",
        )

    client = TestClient(create_dashboard_app(repo_path=tmp_path))
    response = client.get("/api/session/current")

    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["id"] == session_id
    assert payload["activities"][0]["activity_type"] == "agent_step"
    assert payload["activities"][0]["title"] == "initial_planning phase"


def test_dashboard_can_select_experiment_from_query_param(tmp_path: Path) -> None:
    initialize_database(db_path(tmp_path))
    with connect(db_path(tmp_path)) as connection:
        session_id = create_session(
            connection,
            repo_path=str(tmp_path),
            max_duration_seconds=60,
            status="running",
            started_at="2026-04-03T10:00:00+00:00",
            created_at="2026-04-03T10:00:00+00:00",
        )
        first_experiment_id = create_experiment(
            connection,
            session_id=session_id,
            kind="baseline",
            description="Baseline experiment",
            max_runs=1,
            status="completed",
            agent_provider=None,
            created_at="2026-04-03T10:00:00+00:00",
            updated_at="2026-04-03T10:00:00+00:00",
        )
        create_run(
            connection,
            experiment_id=first_experiment_id,
            run_index=1,
            command="baseline",
            status="completed",
            started_at="2026-04-03T10:00:01+00:00",
            created_at="2026-04-03T10:00:01+00:00",
        )
        second_experiment_id = create_experiment(
            connection,
            session_id=session_id,
            kind="candidate",
            description="Selected candidate",
            max_runs=1,
            status="running",
            agent_provider="codex",
            created_at="2026-04-03T10:01:00+00:00",
            updated_at="2026-04-03T10:01:00+00:00",
        )
        create_run(
            connection,
            experiment_id=second_experiment_id,
            run_index=2,
            command="candidate",
            status="running",
            started_at="2026-04-03T10:01:01+00:00",
            created_at="2026-04-03T10:01:01+00:00",
        )

    client = TestClient(create_dashboard_app(repo_path=tmp_path))
    response = client.get(f"/?experiment_id={second_experiment_id}")

    assert response.status_code == 200
    assert "Selected candidate" in response.text
    assert "Run 2" in response.text


def test_dashboard_can_select_run_and_show_run_activity(tmp_path: Path) -> None:
    initialize_database(db_path(tmp_path))
    with connect(db_path(tmp_path)) as connection:
        session_id = create_session(
            connection,
            repo_path=str(tmp_path),
            max_duration_seconds=60,
            status="running",
            started_at="2026-04-03T10:00:00+00:00",
            created_at="2026-04-03T10:00:00+00:00",
        )
        experiment_id = create_experiment(
            connection,
            session_id=session_id,
            kind="candidate",
            description="Candidate with activity",
            max_runs=2,
            status="running",
            agent_provider="codex",
            created_at="2026-04-03T10:00:00+00:00",
            updated_at="2026-04-03T10:00:00+00:00",
        )
        first_run_id = create_run(
            connection,
            experiment_id=experiment_id,
            run_index=1,
            command="candidate-1",
            status="completed",
            started_at="2026-04-03T10:00:01+00:00",
            created_at="2026-04-03T10:00:01+00:00",
        )
        create_run(
            connection,
            experiment_id=experiment_id,
            run_index=2,
            command="candidate-2",
            status="running",
            started_at="2026-04-03T10:01:01+00:00",
            created_at="2026-04-03T10:01:01+00:00",
        )
        create_agent_step(
            connection,
            experiment_id=experiment_id,
            run_index=1,
            phase="planning",
            prompt="plan",
            status="completed",
            started_at="2026-04-03T10:00:02+00:00",
            created_at="2026-04-03T10:00:02+00:00",
        )

    client = TestClient(create_dashboard_app(repo_path=tmp_path))
    response = client.get(f"/?experiment_id={experiment_id}&run_id={first_run_id}")

    assert response.status_code == 200
    assert "Candidate with activity" in response.text
    assert "Run activity" in response.text
    assert "planning" in response.text


def test_dashboard_server_falls_forward_to_next_open_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class FakeProcess:
        pid = 4321

    def fake_find_available_port(
        *, host: str, start_port: int, max_attempts: int = 100
    ) -> int:
        observed["find"] = (host, start_port, max_attempts)
        return start_port + 1

    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed["popen"] = (args, kwargs)
        return FakeProcess()

    monkeypatch.setattr(
        "easy_autoresearch.app.server.find_available_port",
        fake_find_available_port,
    )
    monkeypatch.setattr(
        "easy_autoresearch.app.server.subprocess.Popen",
        fake_popen,
    )
    monkeypatch.setattr(DashboardServer, "_reuse_existing", lambda self: False)
    monkeypatch.setattr(DashboardServer, "_is_healthy", lambda self, host, port: True)
    monkeypatch.setattr(
        DashboardServer,
        "_write_state",
        lambda self, *, pid, host, port: observed.setdefault(
            "state", (pid, host, port)
        ),
    )

    server = DashboardServer(repo_path=tmp_path, host="127.0.0.1", port=8765)
    server.start()

    assert observed["find"] == ("127.0.0.1", 8765, 100)
    assert observed["state"] == (4321, "127.0.0.1", 8766)
    assert server.url == "http://127.0.0.1:8766"


def test_dashboard_server_stop_waits_for_exit_before_clearing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / ".autoresearch" / "dashboard.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        '{"pid": 4321, "host": "127.0.0.1", "port": 8766}',
        encoding="utf-8",
    )
    observed: dict[str, object] = {}
    process_running = iter([True, True, False])

    def fake_kill(pid: int, sig: int) -> None:
        observed["kill"] = (pid, sig)

    def fake_is_process_running(self, pid: int) -> bool:
        observed.setdefault("checks", []).append(pid)
        return next(process_running)

    monkeypatch.setattr("easy_autoresearch.app.server.os.kill", fake_kill)
    monkeypatch.setattr(
        DashboardServer,
        "_is_process_running",
        fake_is_process_running,
    )

    server = DashboardServer(repo_path=tmp_path, host="127.0.0.1", port=8765)
    assert server.stop() is True
    assert observed["kill"] == (4321, 15)
    assert observed["checks"] == [4321, 4321, 4321]
    assert not state_path.exists()


def test_dashboard_server_kills_spawned_process_when_health_check_never_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(
        "easy_autoresearch.app.server.find_available_port",
        lambda **_: 8766,
    )
    monkeypatch.setattr(
        "easy_autoresearch.app.server.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(DashboardServer, "_reuse_existing", lambda self: False)
    monkeypatch.setattr(DashboardServer, "_is_healthy", lambda self, host, port: False)
    monkeypatch.setattr(DashboardServer, "_is_process_running", lambda self, pid: True)
    monkeypatch.setattr(
        "easy_autoresearch.app.server.os.kill",
        lambda pid, sig: observed.setdefault("kill", (pid, sig)),
    )
    monkeypatch.setattr("easy_autoresearch.app.server.time.sleep", lambda _: None)

    server = DashboardServer(repo_path=tmp_path, host="127.0.0.1", port=8765)

    with pytest.raises(RuntimeError, match="Dashboard server did not start"):
        server.start()

    assert observed["kill"] == (4321, 15)
