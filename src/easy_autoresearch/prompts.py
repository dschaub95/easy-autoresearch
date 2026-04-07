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
        "the most promising change to make this experiment a success. Do not edit files yet."
    ),
    "execution": ("Implement the planned changes. "),
    "issue_resolution": (
        "Review the changes for likely issues, fix anything necessary, and "
        "ensure the run command as defined in autoresearch.yaml still works. "
    ),
}


def build_agent_phase_prompt(
    *,
    experiment_index: int,
    run_index: int,
    phase: AgentPhase,
    evaluation_command: str,
) -> str:
    return "\n\n".join(
        [
            f"Experiment {experiment_index}, attempt {run_index}, phase: {phase}.",
            AGENT_PHASE_INSTRUCTIONS[phase],
            f"The evaluation command is `{evaluation_command}`.",
        ]
    )


def build_initial_planning_prompt(
    template: str | None,
    *,
    experiment_index: int,
    evaluation_command: str,
    summary_dir: str,
    run_logs_dir: str,
    agent_logs_dir: str,
    agent_stderr_logs_dir: str,
    database_path: str,
) -> str:
    return "\n\n".join(
        part
        for part in [
            template,
            f"Experiment {experiment_index}, initial planning.",
            (
                "This step is read-only. Do not edit any files yet. Choose one new "
                "high-level idea to improve the evaluation metric."
            ),
            f"Start by carefully reading all summary markdown files under `{summary_dir}`.",
            f"The evaluation command is `{evaluation_command}`.",
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
        ]
        if part
    )


def build_setup_prompt() -> str:
    return "\n\n".join(
        [
            "Prepare this repository for repeated local optimization of its end-to-end workflow.",
            (
                "If necessary create or adjust a clear local run command that generally includes "
                "both training and evaluation. Ensure it prints at least one "
                "meaningful scalar metric to stdout, and update autoresearch.yaml "
                "so commands.run is that command and commands.metric_pattern "
                "matches that metric."
            ),
            (
                "Make each run reproducible: rerunning commands.run should "
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


def build_experiment_summary(summary: str, result: CommandResult | None) -> str:
    base_summary = summary.strip()
    metric_value = result.metric_value if result is not None else None
    metric_text = metric_value if metric_value is not None else "n/a"
    if not base_summary:
        return f"Resulting metric: {metric_text}"
    return f"{base_summary}\n\nResulting metric: {metric_text}"
