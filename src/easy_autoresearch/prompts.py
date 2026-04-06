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
    "execution": (
        "Implement the planned change in this same session. "
        "Ensure run command as defined in autoresearch.yaml still works. "
    ),
    "issue_resolution": (
        "Review the changes for likely issues, fix anything necessary, and "
        "leave the workspace ready for evaluation. "
    ),
}

SETUP_PROMPT_PARTS = (
    "Prepare this repository for repeated local optimization of its end-to-end workflow.",
    (
        "Create or adjust a clear local run command that generally includes "
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
)

INITIAL_PLANNING_PROMPT_PARTS = (
    "Experiment-level initial planning.",
    (
        "Inspect the codebase and the current session's experiment history below, "
        "then decide on one new high-level idea to improve performance as measured "
        "by the evaluation metric."
    ),
    "This step is read-only. Do not edit any files yet.",
    (
        "Use web search and relevant scientific or technical literature when it "
        "would materially improve the idea selection."
    ),
    (
        "If subagents are available, use them for literature or web research when "
        "helpful. Otherwise perform that research directly in this session."
    ),
)

SUMMARY_PROMPT_HEADING = (
    "Summarize this experiment in plain text under the headings "
    "Hypothesis, Approach, Findings."
)

SUMMARY_FALLBACK_LINES = (
    "Hypothesis",
    "No agent summary was captured.",
    "",
    "Approach",
    "The harness executed the configured experiment flow.",
    "",
    "Findings",
)


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
    session_history: str,
) -> str:
    return "\n\n".join(
        part
        for part in [
            template,
            f"Experiment {experiment_index}, initial planning.",
            *INITIAL_PLANNING_PROMPT_PARTS,
            f"The evaluation command is `{evaluation_command}`.",
            f"Current session experiment history:\n{session_history}",
        ]
        if part
    )


def build_setup_prompt() -> str:
    return "\n\n".join(SETUP_PROMPT_PARTS)


def build_summary_prompt(result: CommandResult | None) -> str:
    return "\n\n".join(
        [
            SUMMARY_PROMPT_HEADING,
            f"Evaluation status: {result.status if result else 'not run'}",
            f"Metric: {result.metric_value if result else 'n/a'}",
            f"Stdout:\n{result.stdout if result else ''}",
            f"Stderr:\n{result.stderr if result else ''}",
        ]
    )


def build_fallback_summary(result: CommandResult | None) -> str:
    findings = (
        f"Status: {result.status}, metric: {result.metric_value}"
        if result is not None
        else "No evaluation command completed."
    )
    return "\n".join([*SUMMARY_FALLBACK_LINES, findings])
