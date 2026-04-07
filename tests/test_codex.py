from pathlib import Path

import pytest

from easy_autoresearch.agent import AgentRunResult, Codex, CodingAgent
from easy_autoresearch.config import logs_dir


class FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)

    def readline(self) -> str:
        if self.lines:
            return self.lines.pop(0)
        return ""

    def close(self) -> None:
        return None


class FakePopen:
    def __init__(
        self,
        command: list[str],
        *,
        stdout_lines: list[str],
        stderr_lines: list[str],
        returncode: int,
    ) -> None:
        self.command = command
        self.stdout = FakeStream(stdout_lines)
        self.stderr = FakeStream(stderr_lines)
        self._returncode = returncode

    def wait(self, timeout: int | None = None) -> int:
        return self._returncode

    def poll(self) -> int:
        return (
            self._returncode
            if not self.stdout.lines and not self.stderr.lines
            else None
        )

    def kill(self) -> None:
        return None


def test_codex_run_invokes_cli_and_writes_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed["args"] = args
        observed["kwargs"] = kwargs
        return FakePopen(
            args[0],
            stdout_lines=['{"session_id":"sess_123","text":"hello"}\n'],
            stderr_lines=["warning\n"],
            returncode=0,
        )

    monkeypatch.setattr("easy_autoresearch.agent.codex.subprocess.Popen", fake_popen)
    logs_dir(tmp_path).mkdir(parents=True)

    result = Codex(tmp_path).run("your prompt")

    assert result == AgentRunResult(
        exit_code=0,
        output_path=tmp_path / ".autoresearch" / "logs" / "run.jsonl",
        stderr_path=tmp_path / ".autoresearch" / "logs" / "run.stderr.log",
        session_id="sess_123",
        text="hello",
        stderr="warning\n",
    )
    assert observed["args"] == (
        [
            "codex",
            "exec",
            "--json",
            "-s",
            "workspace-write",
            "-C",
            str(tmp_path.resolve()),
            "your prompt",
        ],
    )
    assert observed["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert observed["kwargs"]["text"] is True


def test_codex_run_passes_model_flag_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed["args"] = args
        return FakePopen(
            args[0],
            stdout_lines=['{"text":"hello"}\n'],
            stderr_lines=[],
            returncode=0,
        )

    monkeypatch.setattr("easy_autoresearch.agent.codex.subprocess.Popen", fake_popen)
    logs_dir(tmp_path).mkdir(parents=True)

    Codex(tmp_path, model="gpt-5.4").run("your prompt")

    assert observed["args"] == (
        [
            "codex",
            "exec",
            "--json",
            "-s",
            "workspace-write",
            "-C",
            str(tmp_path.resolve()),
            "-m",
            "gpt-5.4",
            "your prompt",
        ],
    )


def test_codex_run_passes_custom_sandbox_flag_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed["args"] = args
        return FakePopen(
            args[0],
            stdout_lines=['{"text":"hello"}\n'],
            stderr_lines=[],
            returncode=0,
        )

    monkeypatch.setattr("easy_autoresearch.agent.codex.subprocess.Popen", fake_popen)
    logs_dir(tmp_path).mkdir(parents=True)

    Codex(tmp_path, sandbox_mode="read-only").run("your prompt")

    assert observed["args"] == (
        [
            "codex",
            "exec",
            "--json",
            "-s",
            "read-only",
            "-C",
            str(tmp_path.resolve()),
            "your prompt",
        ],
    )


def test_codex_reuses_session_id_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        command = args[0]
        commands.append(command)
        stdout_lines = (
            ['{"event":{"sessionId":"sess_123"},"text":"step"}\n']
            if len(commands) == 1
            else ['{"text":"next"}\n']
        )
        return FakePopen(
            command, stdout_lines=stdout_lines, stderr_lines=[], returncode=0
        )

    monkeypatch.setattr("easy_autoresearch.agent.codex.subprocess.Popen", fake_popen)
    logs_dir(tmp_path).mkdir(parents=True)
    codex = Codex(tmp_path)

    first = codex.run("first prompt")
    second = codex.run("second prompt")

    assert first.session_id == "sess_123"
    assert second.text == "next"
    assert codex.session_id == "sess_123"
    root = str(tmp_path.resolve())
    assert commands == [
        [
            "codex",
            "exec",
            "--json",
            "-s",
            "workspace-write",
            "-C",
            root,
            "first prompt",
        ],
        [
            "codex",
            "exec",
            "--json",
            "-s",
            "workspace-write",
            "-C",
            root,
            "resume",
            "sess_123",
            "second prompt",
        ],
    ]


def test_codex_run_accumulates_text_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        return FakePopen(
            args[0],
            stdout_lines=[
                '{"text":"thinking"}\n',
                '{"content":"draft message"}\n',
                '{"text":"final commit message"}\n',
            ],
            stderr_lines=[],
            returncode=0,
        )

    monkeypatch.setattr("easy_autoresearch.agent.codex.subprocess.Popen", fake_popen)
    logs_dir(tmp_path).mkdir(parents=True)

    result = Codex(tmp_path).run("normal prompt")

    assert result.text == "thinking\ndraft message\nfinal commit message"


def test_codex_is_a_coding_agent() -> None:
    assert issubclass(Codex, CodingAgent)


def test_codex_run_supports_custom_log_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "logs" / "codex.jsonl"
    stderr_path = tmp_path / "logs" / "codex.stderr.log"
    output_path.parent.mkdir(parents=True)

    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        return FakePopen(
            args[0],
            stdout_lines=['{"text":"done"}\n'],
            stderr_lines=["stderr\n"],
            returncode=2,
        )

    monkeypatch.setattr("easy_autoresearch.agent.codex.subprocess.Popen", fake_popen)

    result = Codex(tmp_path).run(
        "different prompt",
        output_path=output_path,
        stderr_path=stderr_path,
        timeout_seconds=30,
    )

    assert result.exit_code == 2
    assert result.output_path == output_path
    assert result.stderr_path == stderr_path
    assert result.text == "done"
    assert result.stderr == "stderr\n"


def test_codex_run_returns_only_latest_text_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        return FakePopen(
            args[0],
            stdout_lines=[
                '{"type":"item.completed","item":{"type":"agent_message","text":"thinking"}}\n',
                '{"type":"item.completed","item":{"type":"agent_message","text":"final commit message"}}\n',
                '{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"not the commit message","text":"ignore me"}}\n',
            ],
            stderr_lines=[],
            returncode=0,
        )

    monkeypatch.setattr("easy_autoresearch.agent.codex.subprocess.Popen", fake_popen)
    logs_dir(tmp_path).mkdir(parents=True)

    result = Codex(tmp_path).run("commit prompt", text_capture="latest")

    assert result.text == "final commit message"
