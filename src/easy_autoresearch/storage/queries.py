"""Read-side queries used by the observability dashboard."""

from __future__ import annotations

from pathlib import Path

from easy_autoresearch.config import db_path
from easy_autoresearch.storage.connection import connect


def latest_session(repo_path: Path) -> dict[str, object] | None:
    with connect(db_path(repo_path)) as connection:
        active = connection.execute(
            """
            SELECT *
            FROM sessions
            WHERE status = 'running'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if active is not None:
            return dict(active)
        latest = connection.execute(
            """
            SELECT *
            FROM sessions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(latest) if latest is not None else None


def session_snapshot(repo_path: Path, session_id: int) -> dict[str, object]:
    with connect(db_path(repo_path)) as connection:
        session_row = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session_row is None:
            raise LookupError(f"Session {session_id} not found")
        session = dict(session_row)

        experiments = [
            dict(row)
            for row in connection.execute(
                """
                SELECT *
                FROM experiments
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        ]
        for experiment in experiments:
            experiment["runs"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM runs
                    WHERE experiment_id = ?
                    ORDER BY id ASC
                    """,
                    (experiment["id"],),
                ).fetchall()
            ]
            experiment["agent_steps"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM agent_steps
                    WHERE experiment_id = ?
                    ORDER BY id ASC
                    """,
                    (experiment["id"],),
                ).fetchall()
            ]

        activities = recent_activity(repo_path, session_id)

    return {
        "session": session,
        "experiments": experiments,
        "activities": activities,
    }


def recent_activity(
    repo_path: Path,
    session_id: int,
) -> list[dict[str, object]]:
    with connect(db_path(repo_path)) as connection:
        activity_rows = connection.execute(
            """
            SELECT
                'run' AS activity_type,
                runs.id AS activity_id,
                runs.status AS status,
                runs.created_at AS created_at,
                runs.finished_at AS finished_at,
                runs.run_index AS run_index,
                NULL AS phase,
                experiments.kind AS experiment_kind
            FROM runs
            JOIN experiments ON experiments.id = runs.experiment_id
            WHERE experiments.session_id = ?
            UNION ALL
            SELECT
                'agent_step' AS activity_type,
                agent_steps.id AS activity_id,
                agent_steps.status AS status,
                agent_steps.created_at AS created_at,
                agent_steps.finished_at AS finished_at,
                agent_steps.run_index AS run_index,
                agent_steps.phase AS phase,
                experiments.kind AS experiment_kind
            FROM agent_steps
            JOIN experiments ON experiments.id = agent_steps.experiment_id
            WHERE experiments.session_id = ?
            ORDER BY created_at DESC, activity_id DESC
            LIMIT 50
            """,
            (session_id, session_id),
        ).fetchall()
    activities: list[dict[str, object]] = []
    for row in activity_rows:
        title = (
            f"{row['experiment_kind']} run {row['run_index']}"
            if row["activity_type"] == "run"
            else f"{row['phase']} phase for run {row['run_index']}"
        )
        activities.append(
            {
                "activity_type": row["activity_type"],
                "activity_id": row["activity_id"],
                "status": row["status"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
                "run_index": row["run_index"],
                "phase": row["phase"],
                "experiment_kind": row["experiment_kind"],
                "title": title,
            }
        )
    return activities
