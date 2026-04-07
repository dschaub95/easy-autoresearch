"""Abstract interfaces for coding agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(slots=True)
class AgentRunResult:
    exit_code: int
    output_path: Path
    stderr_path: Path
    session_id: str | None
    text: str
    stderr: str


class CodingAgent(ABC):
    def __init__(self, repo_path: Path, session_id: str | None = None) -> None:
        self.repo_path = repo_path
        self.session_id = session_id

    @abstractmethod
    def run(
        self,
        prompt: str,
        *,
        output_path: Path | None = None,
        stderr_path: Path | None = None,
        timeout_seconds: int | None = None,
        text_capture: Literal["full", "latest"] = "full",
    ) -> AgentRunResult: ...
