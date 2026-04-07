"""Helpers for running Codex CLI non-interactively."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from easy_autoresearch.config import logs_dir

from .base import AgentRunResult, CodingAgent


def _session_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("session_id", "sessionId"):
            if isinstance(value.get(key), str):
                return value[key]
        for nested in value.values():
            if found := _session_id(nested):
                return found
    if isinstance(value, list):
        for item in value:
            if found := _session_id(item):
                return found
    return None


def _text_parts(value: Any) -> list[str]:
    if isinstance(value, dict):
        parts: list[str] = []
        for key, nested in value.items():
            if key in {"text", "content"} and isinstance(nested, str):
                parts.append(nested)
            else:
                parts.extend(_text_parts(nested))
        return parts
    if isinstance(value, list):
        return [part for item in value for part in _text_parts(item)]
    return []


class Codex(CodingAgent):
    def __init__(
        self,
        repo_path: Path,
        session_id: str | None = None,
        model: str | None = None,
        sandbox_mode: str = "workspace-write",
        stream_output: bool = False,
    ) -> None:
        super().__init__(repo_path, session_id=session_id)
        self.model = model
        self.sandbox_mode = sandbox_mode
        self.stream_output = stream_output

    def run(
        self,
        prompt: str,
        *,
        output_path: Path | None = None,
        stderr_path: Path | None = None,
        timeout_seconds: int | None = None,
    ) -> AgentRunResult:
        output_path = output_path or logs_dir(self.repo_path) / "run.jsonl"
        stderr_path = stderr_path or logs_dir(self.repo_path) / "run.stderr.log"
        repo = str(self.repo_path.resolve())
        command = [
            "codex",
            "exec",
            "--json",
            "-s",
            self.sandbox_mode,
            "-C",
            repo,
        ]
        if self.model:
            command.extend(["-m", self.model])
        command += ["resume", self.session_id, prompt] if self.session_id else [prompt]
        text_parts: list[str] = []
        stderr_parts: list[str] = []
        queue: Queue[tuple[str, str]] = Queue()
        stdout_done = threading.Event()
        stderr_done = threading.Event()

        with (
            output_path.open("w", encoding="utf-8") as stdout_handle,
            stderr_path.open("w", encoding="utf-8") as stderr_handle,
        ):
            process = subprocess.Popen(
                command,
                cwd=repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            def read_stdout() -> None:
                stream = process.stdout
                if stream is None:
                    return
                try:
                    for line in iter(stream.readline, ""):
                        stdout_handle.write(line)
                        stdout_handle.flush()
                        queue.put(("stdout", line))
                finally:
                    stream.close()
                    stdout_done.set()

            def read_stderr() -> None:
                stream = process.stderr
                if stream is None:
                    return
                try:
                    for line in iter(stream.readline, ""):
                        stderr_handle.write(line)
                        stderr_handle.flush()
                        queue.put(("stderr", line))
                finally:
                    stream.close()
                    stderr_done.set()

            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            timed_out = False
            deadline = time.monotonic() + timeout_seconds if timeout_seconds else None
            while True:
                try:
                    stream_name, line = queue.get(timeout=0.1)
                except Empty:
                    stream_name = None
                    line = ""
                if stream_name is not None:
                    if stream_name == "stderr":
                        stderr_parts.append(line)
                        if self.stream_output and line.strip():
                            print(f"[codex stderr] {line.rstrip()}", flush=True)
                    elif line.strip():
                        payload = json.loads(line)
                        self.session_id = self.session_id or _session_id(payload)
                        line_text_parts = [
                            part for part in _text_parts(payload) if part
                        ]
                        text_parts.extend(line_text_parts)
                        if self.stream_output:
                            for part in line_text_parts:
                                print(f"[codex] {part}", flush=True)

                process_done = process.poll() is not None
                if (
                    process_done
                    and stdout_done.is_set()
                    and stderr_done.is_set()
                    and queue.empty()
                ):
                    break

                if (
                    deadline is not None
                    and time.monotonic() >= deadline
                    and not process_done
                ):
                    process.kill()
                    timed_out = True
                    break

            if timed_out:
                exit_code = process.wait()
            else:
                exit_code = process.wait()

            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
        return AgentRunResult(
            exit_code=exit_code,
            output_path=output_path,
            stderr_path=stderr_path,
            session_id=self.session_id,
            text="\n".join(part for part in text_parts if part).strip(),
            stderr="".join(stderr_parts) or stderr_path.read_text(encoding="utf-8"),
        )
