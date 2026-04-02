from pathlib import Path

import pytest

from easy_autoresearch.agent import AgentRunResult, CodingAgent
from easy_autoresearch.codex import Codex, run_codex
from easy_autoresearch.config import logs_dir


def test_run_codex_invokes_cli_and_writes_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed["args"] = args
        observed["kwargs"] = kwargs
        kwargs["stdout"].write('{"session_id":"sess_123","text":"hello"}\n')
        kwargs["stderr"].write("warning\n")

        class CompletedProcess:
            returncode = 0

        return CompletedProcess()

    monkeypatch.setattr("easy_autoresearch.codex.subprocess.run", fake_run)
    logs_dir(tmp_path).mkdir(parents=True)

    result = run_codex("your prompt", repo_path=tmp_path)

    assert result == AgentRunResult(
        exit_code=0,
        output_path=tmp_path / ".autoresearch" / "logs" / "run.jsonl",
        stderr_path=tmp_path / ".autoresearch" / "logs" / "run.stderr.log",
        session_id="sess_123",
        text="hello",
        stderr="warning\n",
    )
    assert observed["args"] == (["codex", "exec", "--json", "your prompt"],)
    assert observed["kwargs"]["cwd"] == str(tmp_path)
    assert observed["kwargs"]["text"] is True
    assert observed["kwargs"]["check"] is False


def test_run_codex_passes_model_flag_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed["args"] = args
        kwargs["stdout"].write('{"text":"hello"}\n')
        kwargs["stderr"].write("")

        class CompletedProcess:
            returncode = 0

        return CompletedProcess()

    monkeypatch.setattr("easy_autoresearch.codex.subprocess.run", fake_run)
    logs_dir(tmp_path).mkdir(parents=True)

    run_codex("your prompt", repo_path=tmp_path, model="gpt-5.4")

    assert observed["args"] == (
        ["codex", "exec", "--json", "-m", "gpt-5.4", "your prompt"],
    )


def test_codex_reuses_session_id_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        command = args[0]
        commands.append(command)
        kwargs["stdout"].write(
            '{"event":{"sessionId":"sess_123"},"text":"step"}\n'
            if len(commands) == 1
            else '{"text":"next"}\n'
        )

        class CompletedProcess:
            returncode = 0

        return CompletedProcess()

    monkeypatch.setattr("easy_autoresearch.codex.subprocess.run", fake_run)
    logs_dir(tmp_path).mkdir(parents=True)
    codex = Codex(tmp_path)

    first = codex.run("first prompt")
    second = codex.run("second prompt")

    assert first.session_id == "sess_123"
    assert second.text == "next"
    assert codex.session_id == "sess_123"
    assert commands == [
        ["codex", "exec", "--json", "first prompt"],
        ["codex", "exec", "--json", "resume", "sess_123", "second prompt"],
    ]


def test_codex_is_a_coding_agent() -> None:
    assert issubclass(Codex, CodingAgent)


def test_run_codex_supports_custom_log_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "logs" / "codex.jsonl"
    stderr_path = tmp_path / "logs" / "codex.stderr.log"
    output_path.parent.mkdir(parents=True)

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["stdout"].write('{"text":"done"}\n')
        kwargs["stderr"].write("stderr\n")

        class CompletedProcess:
            returncode = 2

        return CompletedProcess()

    monkeypatch.setattr("easy_autoresearch.codex.subprocess.run", fake_run)

    result = run_codex(
        "different prompt",
        repo_path=tmp_path,
        output_path=output_path,
        stderr_path=stderr_path,
        timeout_seconds=30,
    )

    assert result.exit_code == 2
    assert result.output_path == output_path
    assert result.stderr_path == stderr_path
    assert result.text == "done"
    assert result.stderr == "stderr\n"
