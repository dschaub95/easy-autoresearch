"""Shared prompt text and prompt builders for easy-autoresearch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .main import CommandResult

AgentPhase = Literal["planning", "execution", "issue_resolution"]

CODEX_SYSTEM_PROMPT = ""

AGENT_PHASE_INSTRUCTIONS: dict[AgentPhase, str] = {
    "planning": (
        "Inspect the codebase and produce a concise implementation plan for "
        "the most promising change to make this experiment a success. Do not edit files at this point."
    ),
    "execution": (
        "Implement the planned changes. Try to keep the changes minimal, while balancing for maintainability. "
    ),
    "issue_resolution": (
        "Review the changes for likely issues, fix anything necessary, and "
        "ensure the run command as defined in autoresearch.yaml still works. "
    ),
}


def timed_evaluation_command(command: str) -> str:
    return f"/usr/bin/time -p {command}"


def build_agent_phase_prompt(
    *,
    experiment_index: int,
    run_index: int,
    phase: AgentPhase,
    evaluation_command: str,
    runtime_constraint_text: str | None = None,
) -> str:
    return "\n\n".join(
        part
        for part in [
            f"Experiment {experiment_index}, attempt {run_index}, phase: {phase}.",
            AGENT_PHASE_INSTRUCTIONS[phase],
            f"The evaluation command is `{evaluation_command}`.",
            (
                "For manual runtime inspection, prefer running "
                f"`{timed_evaluation_command(evaluation_command)}`."
            ),
            runtime_constraint_text,
        ]
        if part
    )


def build_initial_planning_prompt(
    template: str | None,
    *,
    evaluation_command: str,
    metric_pattern: str | None,
    summary_dir: str,
    run_logs_dir: str,
    agent_logs_dir: str,
    agent_stderr_logs_dir: str,
    database_path: str,
    previous_best_metric: float | None,
    runtime_constraint_text: str | None = None,
) -> str:
    return "\n\n".join(
        part
        for part in [
            template,
            (
                "This experiment is part of a repeated local optimization process "
                "with the aim to make meaningful changes to the codebase that "
                f"improve the result of `{evaluation_command}` in a real end-to-end "
                "run, so we can keep and commit the improvements. "
                + (
                    "Optimize exactly the scalar metric parsed from the evaluation "
                    "command's stdout by `commands.metric_pattern` "
                    f"(`{metric_pattern}`). Higher is better, and improvement means "
                    "a strict increase over the previous best metric of "
                    f"{format_metric(previous_best_metric)}."
                    if metric_pattern
                    else "Optimize exactly the scalar metric produced by the evaluation command. Higher is better."
                )
            ),
            (
                "When you need to inspect end-to-end runtime manually, prefer "
                f"`{timed_evaluation_command(evaluation_command)}`."
            ),
            (
                "Start by carefully reading all summary markdown files of previous experiments under "
                f"`{summary_dir}`."
            ),
            f"Previous best metric: {format_metric(previous_best_metric)}.",
            runtime_constraint_text,
            (
                "Use web search and relevant scientific or technical literature when "
                "it would materially improve the idea selection."
            ),
            (
                "If subagents are available, use them for literature or web research "
                "when helpful. Otherwise perform that research directly in this "
                "session."
            ),
            (
                "If you need more context about previous experiments, inspect these "
                "repo-relative paths as needed:\n"
                f"- Run stdout logs: `{run_logs_dir}`\n"
                f"- Agent transcripts: `{agent_logs_dir}`\n"
                f"- Agent stderr logs: `{agent_stderr_logs_dir}`\n"
                f"- SQLite state database: `{database_path}`"
            ),
            (
                "You are not allowed to cheat, e.g., by switching the machine learning task, the metric, or the dataset. However you are allowed "
                "to modify every other part of the pipeline. This can include switching to a completely different class of models, e.g., "
                "from a neural network to an SVM, or from a convolutional neural network to a transformer. It might also be "
                "necessary to go really deep into the weeds and write custom cuda code (or other low-level code) to balance speed and performance."
            ),
            (
                "This step is read-only. Do not edit any files yet. Come up with one new "
                "high-level idea to improve the evaluation metric."
            ),
        ]
        if part
    )


def build_setup_prompt() -> str:
    return "\n\n".join(
        [
            "Prepare this repository for repeated local optimization of its end-to-end workflow.",
            (
                "If necessary create or adjust a clear local run command that can "
                "be run as is (this has to be exactly the command that you use to run the code including sandbox related additions) "
                "and that generally includes both training and "
                "evaluation. Ensure it prints at least one meaningful scalar "
                "metric to stdout, and update autoresearch.yaml (untracked) so commands.run "
                "is that command and commands.metric_pattern matches that metric."
            ),
            (
                "Make each run reproducible: rerunning commands.run as is should "
                "generally produce the same result, especially through explicit "
                "seeding where needed."
            ),
            (
                "Keep commands.run free of tunable hyperparameters; change them in "
                "tracked code or config files instead."
            ),
            (
                "Do not optimize the repo in this step; only make the minimum setup "
                "changes needed for reliable optimization."
            ),
        ]
    )


def build_summary_prompt(result: CommandResult | None) -> str:
    return "\n\n".join(
        [
            (
                "Summarize this experiment in plain text using exactly this structure:\n"
                "Main idea\n"
                "- bullet points describing only the core experiment idea\n\n"
                "Steps taken\n"
                "- bullet points describing only the implementation steps taken\n\n"
                "Do not include any other sections. Do not include the metric in your response."
            ),
            f"Evaluation status: {result.status if result else 'not run'}",
            f"Metric: {result.metric_value if result else 'n/a'}",
            f"Stdout:\n{result.stdout if result else ''}",
            f"Stderr:\n{result.stderr if result else ''}",
        ]
    )


def format_metric(metric_value: float | None) -> str:
    return str(metric_value) if metric_value is not None else "n/a"


def format_runtime(runtime_seconds: float | None) -> str:
    if runtime_seconds is None:
        return "n/a"
    return f"{runtime_seconds:.3f}s"


def build_experiment_summary(
    summary: str,
    result: CommandResult | None,
    *,
    previous_best_metric: float | None,
    metric_improved: bool,
    changes_discarded: bool,
    baseline_runtime_seconds: float | None = None,
    runtime_cap_seconds: float | None = None,
    runtime_constraint_satisfied: bool | None = None,
) -> str:
    base_summary = summary.strip()
    metric_text = format_metric(result.metric_value if result is not None else None)
    previous_best_metric_text = format_metric(previous_best_metric)
    footer = "\n".join(
        [
            f"Resulting metric: {metric_text}",
            (
                "Resulting runtime: "
                f"{format_runtime(result.runtime_seconds if result is not None else None)}"
            ),
            f"Baseline runtime: {format_runtime(baseline_runtime_seconds)}",
            f"Runtime cap: {format_runtime(runtime_cap_seconds)}",
            (
                "Runtime constraint satisfied: "
                f"{format_yes_no(runtime_constraint_satisfied)}"
            ),
            f"Previous best metric: {previous_best_metric_text}",
            f"Metric improved: {'yes' if metric_improved else 'no'}",
            f"Changes discarded: {'yes' if changes_discarded else 'no'}",
        ]
    )
    if not base_summary:
        return footer
    return f"{base_summary}\n\n{footer}"


def build_commit_message_prompt() -> str:
    return (
        "Write the git commit message for this experiment. Output only the commit "
        "message text. Include only changes you actually made in this experiment "
        "session. Do not include explanations or any other text."
    )


def build_setup_commit_message_prompt() -> str:
    return (
        "Write the git commit message for the setup work in this repository. "
        "Output only the commit message text. Include only changes you actually "
        "made during the setup session. Do not include explanations or any other "
        "text."
    )


def build_runtime_constraint_text(
    *,
    runtime_cap_seconds: float | None,
    baseline_runtime_seconds: float | None,
) -> str | None:
    if runtime_cap_seconds is None:
        return None
    parts = [
        "Primary objective: improve the metric.",
        (
            "Hard runtime constraint: candidate runs must stay within the runtime "
            "cap. Prefer lower runtime when metric tradeoffs are otherwise similar."
        ),
    ]
    if baseline_runtime_seconds is not None:
        parts.append(
            f"Observed baseline runtime: {format_runtime(baseline_runtime_seconds)}."
        )
    if runtime_cap_seconds is not None:
        parts.append(f"Current runtime cap: {format_runtime(runtime_cap_seconds)}.")
    return " ".join(parts)


def format_yes_no(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "yes" if value else "no"
