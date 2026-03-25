import subprocess
import sys


def format() -> None:
    """Run Ruff with `--fix` on the repository."""
    raise SystemExit(subprocess.call(["ruff", "check", "--fix", ".", *sys.argv[1:]]))
