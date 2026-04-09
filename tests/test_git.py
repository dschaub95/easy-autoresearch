import subprocess
from pathlib import Path

import pytest

from easy_autoresearch.git import (
    GitWorktreeError,
    commit_all_changes,
    has_uncommitted_changes,
    session_branch_name,
    switch_to_session_branch,
)


def run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    )


def init_repo(repo_path: Path) -> None:
    run_git(repo_path, "init")
    run_git(repo_path, "config", "user.name", "Test User")
    run_git(repo_path, "config", "user.email", "test@example.com")


def test_has_uncommitted_changes_ignores_autoresearch_state_and_config(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    init_repo(repo_path)

    (repo_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    run_git(repo_path, "add", "tracked.txt")
    run_git(repo_path, "commit", "-m", "initial")

    (repo_path / ".autoresearch").mkdir()
    (repo_path / ".autoresearch" / "state.db").write_text("db\n", encoding="utf-8")
    (repo_path / "autoresearch.yaml").write_text("temp: true\n", encoding="utf-8")

    assert has_uncommitted_changes(repo_path) is False


def test_commit_all_changes_excludes_autoresearch_state_and_config(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    init_repo(repo_path)

    (repo_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    (repo_path / ".autoresearch").mkdir()
    (repo_path / ".autoresearch" / "notes.txt").write_text("base\n", encoding="utf-8")
    (repo_path / "autoresearch.yaml").write_text("value: old\n", encoding="utf-8")
    run_git(
        repo_path, "add", "tracked.txt", ".autoresearch/notes.txt", "autoresearch.yaml"
    )
    run_git(repo_path, "commit", "-m", "initial")

    (repo_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo_path / ".autoresearch" / "notes.txt").write_text(
        "updated\n", encoding="utf-8"
    )
    (repo_path / "autoresearch.yaml").write_text("value: new\n", encoding="utf-8")

    commit_all_changes(repo_path, "managed commit")

    changed_files = run_git(
        repo_path, "show", "--name-only", "--format=", "HEAD"
    ).stdout.splitlines()
    assert changed_files == ["tracked.txt"]

    status_lines = run_git(repo_path, "status", "--short").stdout.splitlines()
    assert " M .autoresearch/notes.txt" in status_lines
    assert " M autoresearch.yaml" in status_lines


def test_switch_to_session_branch_creates_and_checks_out_session_branch(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    init_repo(repo_path)

    (repo_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    run_git(repo_path, "add", "tracked.txt")
    run_git(repo_path, "commit", "-m", "initial")

    branch_name = switch_to_session_branch(repo_path, 7)

    assert branch_name == session_branch_name(7)
    current_branch = run_git(repo_path, "branch", "--show-current").stdout.strip()
    assert current_branch == "autoresearch/session-7"


def test_switch_to_session_branch_fails_when_branch_already_exists(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    init_repo(repo_path)

    (repo_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    run_git(repo_path, "add", "tracked.txt")
    run_git(repo_path, "commit", "-m", "initial")
    run_git(repo_path, "switch", "-c", "autoresearch/session-7")
    run_git(repo_path, "switch", "-")

    with pytest.raises(GitWorktreeError, match="already exists"):
        switch_to_session_branch(repo_path, 7)
