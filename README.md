# easy-autoresearch

Scaffolding for an autonomous repository-optimization loop aimed at Codex first.

Current scope:

- scaffold a target repository with local config, prompts, and SQLite state
- run a session loop that records `sessions`, `experiments`, and `runs`
- record per-run agent conversation steps in SQLite
- retry across multiple experiments and runs according to config limits
- run candidate experiments through planning, execution, and issue-resolution
  agent phases before evaluation

This is intentionally pre-AI scaffolding. The loop runs configured subprocesses
and stores the outputs, but it does not yet ask Codex to propose code changes.

## How it works

1. **Resolve setup.** The tool uses the repo path (argument or current working directory). If `autoresearch.yaml` already exists, you can continue or overwrite; otherwise it scaffolds defaults (config, SQLite DB, prompt template).

2. **Scaffold (new repos only).** It writes `autoresearch.yaml`, initializes `.autoresearch/state.db`, and adds `.autoresearch/prompts/codex-system.md`. You can pause after a config review prompt unless `-y` is used.

   - **First-time agent setup (only after a fresh scaffold):** If this run created the scaffold, the configured coding agent runs a setup pass on the repo, then (when there are changes) generates a commit message and creates a setup commit. You can cancel after reviewing that change unless `-y` is used.

3. **Open a session.** With a valid config, the tool creates a `sessions` row in SQLite (linked to the setup commit when present) and records limits such as `session.max_duration_seconds`.

4. **Baseline experiment.** One run executes the configured `commands.run` with no agent. It captures stdout/stderr, exit status, an optional metric from `metric_pattern`, and wall-clock runtime. That runtime seeds an optional **runtime cap** when `constraints.runtime` is set (baseline-relative or absolute).

5. **Candidate experiments (loop).** If `experiments.max_experiments` is zero, this block is skipped and a successful baseline alone can complete the session. Otherwise, for each experiment up to that limit, the tool records the current HEAD as the base commit, then:

   - **Initial planning:** A single **initial_planning** agent step runs. If it fails, uncommitted work is discarded and the experiment is recorded as failed.
   - **Runs within an experiment:** For each run up to `experiments.max_runs_per_experiment`, the worktree is reset from the best-so-far snapshot (or uncommitted changes are discarded) before trying again. Each run runs three agent phases in order—**planning**, **execution**, **issue_resolution**—using the same Codex (or configured) session. If any phase fails, that run fails and the loop may retry.
   - **Evaluation:** After the three phases succeed, the same `commands.run` used for the baseline runs again. The result must yield a metric when `metric_pattern` is set, and must satisfy the runtime cap when configured. The best metric seen in this experiment is kept via a **worktree snapshot** so retries can roll back and retry from a known-good tree.
   - **End of experiment:** The tool writes an experiment summary (agent-assisted), then either **commits** all changes if the metric improved versus the session’s best so far, or **discards** uncommitted changes. Session best metric updates when a candidate improves it.

6. **Finish.** The session completes when the baseline succeeds with no candidates (`max_experiments: 0`), or after all configured candidate experiments have run. If any candidate experiment completes successfully (`commands.run` exits cleanly under the recorded constraints), the overall session is marked completed, but the remaining configured experiments still run. Every step above is persisted: sessions, experiments, runs, and per-phase agent steps in SQLite, with logs under `.autoresearch/logs/`.

At a high level (pseudocode, not literal source):

```text
resolve_setup_and_maybe_scaffold()
if fresh_scaffold:
    agent_setup_repo()
    maybe_commit_setup()

open_session()  // SQLite sessions row

baseline_result := run_evaluation_command()  // no agent; record metric + wall-clock runtime
runtime_cap := optional_cap_from(baseline_result, constraints.runtime)
session_best_metric := baseline_result.metric

if max_experiments == 0 and baseline_result.success:
    finish_session(completed); return

for each candidate_experiment in 1 .. max_experiments:
    record_base_commit()
    if not agent_step(initial_planning):
        discard_uncommitted(); continue experiment loop

    for each run in 1 .. max_runs_per_experiment:
        restore_best_snapshot_or_discard()
        run planning, execution, issue_resolution agent steps (same session)
        if any phase failed:
            continue run loop
        eval := run_evaluation_command()
        if eval.success and metric_ok(eval) and within_runtime_cap(eval):
            promote_if_best_in_experiment(eval)  // snapshot worktree when metric improves
        if eval.success:
            break run loop  // this candidate experiment succeeded

    write_summary_via_agent()
    if metric_improved_vs_session_best:
        commit_all_changes()
        session_best_metric := best_metric
    else:
        discard_uncommitted()

finish_session(completed_if_any_candidate_succeeded_else_failed_or_exhausted)
```

## CLI

```bash
uvx easy-autoresearch
uvx easy-autoresearch /path/to/repo
uvx easy-autoresearch -y /path/to/repo
uvx easy-autoresearch --overwrite /path/to/repo
uvx easy-autoresearch --headless /path/to/repo
uvx easy-autoresearch dashboard /path/to/repo
uvx easy-autoresearch dashboard-stop /path/to/repo
```

If no repo path is provided, easy-autoresearch uses the current working
directory.

On startup it checks for an existing setup:

- if no setup exists, it scaffolds the repo and starts a session
- if a setup exists, it prompts to continue or overwrite
- `-y` / `--yes` skips yes/no prompts during setup review
- `--overwrite` skips the prompt and recreates the setup automatically

When the actual research loop starts, easy-autoresearch now also starts a local
observability dashboard by default. Use `--headless` to disable the server.

Dashboard commands:

- `easy-autoresearch dashboard /path/to/repo` starts the local dashboard server
  without starting a research session
- `easy-autoresearch dashboard-stop /path/to/repo` stops a previously started
  dashboard server for that repository
- `dashboard` is non-mutating: it does not scaffold `autoresearch.yaml` or
  `.autoresearch/state.db` for a pristine repository

Scaffolding creates:

- `autoresearch.yaml`
- `.autoresearch/state.db`
- `.autoresearch/prompts/codex-system.md`

Starting a session:

- opens one `sessions` row
- runs up to `experiments.max_experiments`
- runs up to `experiments.max_runs_per_experiment` within each experiment
- for candidate experiments, runs three consecutive agent calls in the same session
  before executing the shared `commands.run`
- stores each run's output and metric in SQLite
- attempts up to `experiments.max_experiments` candidate experiments, without stopping early after the first successful one
- serves a local dashboard while the session is running

## Default Config Shape

```yaml
project:
  repo_path: /absolute/path/to/repo
  name: my-repo
commands:
  run: uv run pytest
  metric_pattern: null
session:
  max_duration_seconds: 3600
experiments:
  max_experiments: 3
  max_runs_per_experiment: 1
agent:
  provider: codex
  model: gpt-5.4-mini
  sandbox_mode: workspace-write
  prompt_template: .autoresearch/prompts/codex-system.md
constraints:
  runtime: 1.5
editable_paths: []
readonly_paths: []
```

Set `agent.model` to pass a specific Codex model via `codex exec -m <MODEL>`.

`constraints.runtime` accepts:

- `null` to disable runtime constraints
- a number such as `1.5` to enforce a baseline-relative cap
- a human-readable duration string such as `30s`, `5m`, or `1h30m`

Runtime is always measured by easy-autoresearch as wall-clock elapsed time.

## Development

```bash
uv run pytest
uv run ruff check . --fix
```

For local testing run this:

```bash
uvx --from /home/dschaub/projects/method-projects/easy-autoresearch easy-autoresearch
```
