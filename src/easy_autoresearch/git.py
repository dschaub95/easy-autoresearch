"""Git helpers for experiment lifecycle management."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

STATE_DIR_PATHSPEC = ":(exclude).autoresearch"


class GitWorktreeError(RuntimeError):
    """Raised when the repository cannot be safely managed via git."""


def _run_git(
    repo_path: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=check,
        text=True,
        capture_output=True,
        env=env,
    )
    return completed


def _managed_pathspecs() -> list[str]:
    return ["--", ".", STATE_DIR_PATHSPEC]


def ensure_clean_tracking(repo_path: Path) -> None:
    try:
        inside_work_tree = _run_git(
            repo_path, "rev-parse", "--is-inside-work-tree"
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise GitWorktreeError(
            "Autoresearch candidate experiments require a git repository."
        ) from error
    if inside_work_tree != "true":
        raise GitWorktreeError(
            "Autoresearch candidate experiments require a git repository."
        )
    status = _run_git(
        repo_path, "status", "--porcelain", *_managed_pathspecs()
    ).stdout.strip()
    if status:
        raise GitWorktreeError(
            "Autoresearch requires a clean git worktree before starting."
        )


def has_uncommitted_changes(repo_path: Path) -> bool:
    status = _run_git(repo_path, "status", "--porcelain", *_managed_pathspecs()).stdout
    return bool(status.strip())


def current_head_sha(repo_path: Path) -> str:
    return _run_git(repo_path, "rev-parse", "HEAD").stdout.strip()


def discard_uncommitted_changes(repo_path: Path) -> None:
    _run_git(
        repo_path,
        "restore",
        "--source=HEAD",
        "--staged",
        "--worktree",
        *_managed_pathspecs(),
    )
    _run_git(repo_path, "clean", "-fd", *_managed_pathspecs())


def save_worktree_snapshot(repo_path: Path, snapshot_dir: Path) -> None:
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    patch_path = snapshot_dir / "tracked.patch"
    untracked_dir = snapshot_dir / "untracked"
    manifest_path = snapshot_dir / "untracked.txt"

    tracked_patch = _run_git(
        repo_path, "diff", "--binary", "HEAD", *_managed_pathspecs()
    ).stdout
    patch_path.write_text(tracked_patch, encoding="utf-8")

    raw_untracked = _run_git(
        repo_path,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        *_managed_pathspecs(),
    ).stdout
    untracked_files = [entry for entry in raw_untracked.split("\0") if entry]
    manifest_path.write_text("\n".join(untracked_files), encoding="utf-8")

    for relative_path in untracked_files:
        source_path = repo_path / relative_path
        destination_path = untracked_dir / relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)


def restore_worktree_snapshot(repo_path: Path, snapshot_dir: Path) -> None:
    discard_uncommitted_changes(repo_path)
    patch_path = snapshot_dir / "tracked.patch"
    if patch_path.exists() and patch_path.read_text(encoding="utf-8").strip():
        _run_git(repo_path, "apply", "--binary", str(patch_path))

    manifest_path = snapshot_dir / "untracked.txt"
    if not manifest_path.exists():
        return
    manifest = manifest_path.read_text(encoding="utf-8").splitlines()
    for relative_path in manifest:
        source_path = snapshot_dir / "untracked" / relative_path
        destination_path = repo_path / relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)


def commit_all_changes(repo_path: Path, message: str) -> str:
    _run_git(repo_path, "add", "-A", *_managed_pathspecs())
    git_env = os.environ | {
        "GIT_AUTHOR_NAME": "easy-autoresearch",
        "GIT_AUTHOR_EMAIL": "easy-autoresearch@example.com",
        "GIT_COMMITTER_NAME": "easy-autoresearch",
        "GIT_COMMITTER_EMAIL": "easy-autoresearch@example.com",
    }
    _run_git(repo_path, "commit", "-m", message, env=git_env)
    return current_head_sha(repo_path)
