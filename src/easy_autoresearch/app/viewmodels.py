"""Template-facing view models for dashboard rendering."""

from __future__ import annotations


def _selected_experiment(
    experiments: list[dict[str, object]], selected_experiment_id: int | None
) -> dict[str, object] | None:
    if not experiments:
        return None
    if selected_experiment_id is not None:
        for experiment in experiments:
            if int(experiment["id"]) == selected_experiment_id:
                return experiment
    for experiment in experiments:
        if experiment["status"] == "running":
            return experiment
    return experiments[0]


def _selected_run(
    experiment: dict[str, object] | None, selected_run_id: int | None
) -> dict[str, object] | None:
    if experiment is None:
        return None
    runs = experiment["runs"]
    if selected_run_id is not None:
        for run in runs:
            if int(run["id"]) == selected_run_id:
                return run
    for run in runs:
        if run["status"] == "running":
            return run
    return runs[0] if runs else None


def build_dashboard_context(
    snapshot: dict[str, object],
    *,
    selected_experiment_id: int | None = None,
    selected_run_id: int | None = None,
) -> dict[str, object]:
    experiments = snapshot["experiments"]
    activities = snapshot["activities"]
    active_phase = None
    for experiment in experiments:
        for step in experiment["agent_steps"]:
            if step["status"] == "running":
                active_phase = step["phase"]
                break
        if active_phase is not None:
            break
    selected = _selected_experiment(experiments, selected_experiment_id)
    selected_run = _selected_run(selected, selected_run_id)
    selected_run_activities: list[dict[str, object]] = []
    if selected is not None and selected_run is not None:
        selected_run_activities = [
            step
            for step in selected["agent_steps"]
            if int(step["run_index"]) == int(selected_run["run_index"])
        ]

    return {
        "session": snapshot["session"],
        "experiments": experiments,
        "activities": activities[:50],
        "active_phase": active_phase,
        "selected_experiment": selected,
        "selected_experiment_id": int(selected["id"]) if selected is not None else None,
        "selected_run": selected_run,
        "selected_run_id": int(selected_run["id"])
        if selected_run is not None
        else None,
        "selected_run_activities": selected_run_activities,
    }
