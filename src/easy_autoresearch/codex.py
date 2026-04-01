"""Helpers for running Codex CLI non-interactively."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .agent import AgentRunResult, CodingAgent
from .config import logs_dir


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
        command = ["codex", "exec", "--json"]
        command += ["resume", self.session_id, prompt] if self.session_id else [prompt]
        with (
            output_path.open("w", encoding="utf-8") as stdout_handle,
            stderr_path.open("w", encoding="utf-8") as stderr_handle,
        ):
            completed = subprocess.run(
                command,
                cwd=str(self.repo_path),
                text=True,
                stdout=stdout_handle,
                stderr=stderr_handle,
                timeout=timeout_seconds,
                check=False,
            )
        text_parts: list[str] = []
        for line in output_path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            payload = json.loads(line)
            self.session_id = self.session_id or _session_id(payload)
            text_parts.extend(_text_parts(payload))
        return AgentRunResult(
            exit_code=completed.returncode,
            output_path=output_path,
            stderr_path=stderr_path,
            session_id=self.session_id,
            text="\n".join(part for part in text_parts if part).strip(),
            stderr=stderr_path.read_text(encoding="utf-8"),
        )


def run_codex(
    prompt: str,
    *,
    repo_path: Path,
    output_path: Path | None = None,
    stderr_path: Path | None = None,
    timeout_seconds: int | None = None,
) -> AgentRunResult:
    return Codex(repo_path).run(
        prompt,
        output_path=output_path,
        stderr_path=stderr_path,
        timeout_seconds=timeout_seconds,
    )
