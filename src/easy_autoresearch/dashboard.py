"""Standalone dashboard server entrypoint."""

from __future__ import annotations

import argparse
from pathlib import Path

from easy_autoresearch.app.server import run_dashboard_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="easy-autoresearch-dashboard")
    parser.add_argument("--repo-path", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_dashboard_server(
        repo_path=args.repo_path.resolve(),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
