"""Programmatic server startup for the observability dashboard."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from easy_autoresearch.config import dashboard_state_path

from .routes import build_router


def create_dashboard_app(*, repo_path: Path) -> FastAPI:
    app = FastAPI(title="easy-autoresearch dashboard")
    package_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(package_dir / "templates"))
    app.mount(
        "/static",
        StaticFiles(directory=str(package_dir / "static")),
        name="static",
    )
    app.include_router(build_router(repo_path=repo_path, templates=templates))
    return app


def run_dashboard_server(*, repo_path: Path, host: str, port: int) -> None:
    uvicorn.run(
        create_dashboard_app(repo_path=repo_path),
        host=host,
        port=port,
        log_level="warning",
    )


def find_available_port(
    *,
    host: str,
    start_port: int,
    max_attempts: int = 100,
) -> int:
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(
        f"No available port found for {host} in range {start_port}-{start_port + max_attempts - 1}"
    )


class DashboardServer:
    def __init__(self, *, repo_path: Path, host: str = "127.0.0.1", port: int = 8765):
        self.repo_path = repo_path
        self.host = host
        self.port = port
        self.process: subprocess.Popen[str] | None = None
        self.state_path = dashboard_state_path(repo_path)
        self.reused_existing = False

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _health_url(self, host: str, port: int) -> str:
        return f"http://{host}:{port}/health"

    def _is_healthy(self, host: str, port: int) -> bool:
        try:
            with urlopen(self._health_url(host, port), timeout=0.3) as response:
                return response.status == 200
        except (OSError, URLError):
            return False

    def _read_state(self) -> dict[str, object] | None:
        if not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _write_state(self, *, pid: int, host: str, port: int) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({"pid": pid, "host": host, "port": port}),
            encoding="utf-8",
        )

    def _clear_state(self) -> None:
        if self.state_path.exists():
            self.state_path.unlink()

    def _is_process_running(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _reuse_existing(self) -> bool:
        state = self._read_state()
        if not state:
            return False
        host = str(state.get("host") or self.host)
        port = int(state.get("port") or self.port)
        if self._is_healthy(host, port):
            self.host = host
            self.port = port
            return True
        self._clear_state()
        return False

    def _launch_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "easy_autoresearch.main",
            "serve-dashboard",
            "--repo-path",
            str(self.repo_path),
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]

    def start(self) -> None:
        if self._reuse_existing():
            self.reused_existing = True
            return
        self.reused_existing = False
        self.port = find_available_port(host=self.host, start_port=self.port)
        self.process = subprocess.Popen(
            self._launch_command(),
            cwd=str(self.repo_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        for _ in range(50):
            if self._is_healthy(self.host, self.port):
                self._write_state(
                    pid=self.process.pid,
                    host=self.host,
                    port=self.port,
                )
                return
            time.sleep(0.1)
        if self.process is not None and self._is_process_running(self.process.pid):
            try:
                os.kill(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        raise RuntimeError("Dashboard server did not start")

    def stop(self) -> bool:
        state = self._read_state()
        if not state:
            return False
        pid = int(state.get("pid") or 0)
        if pid <= 0:
            self._clear_state()
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._clear_state()
            return False
        except PermissionError as error:
            raise RuntimeError(
                f"Permission denied while stopping dashboard pid {pid}"
            ) from error
        for _ in range(50):
            if not self._is_process_running(pid):
                self._clear_state()
                return True
            time.sleep(0.1)
        raise RuntimeError(f"Dashboard pid {pid} did not stop after SIGTERM")
        return True
