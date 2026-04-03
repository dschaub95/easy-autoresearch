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

## CLI

```bash
uvx easy-autoresearch
uvx easy-autoresearch /path/to/repo
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
- stops early when an experiment completes successfully
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
  max_experiments: 1
  max_runs_per_experiment: 1
agent:
  provider: codex
  model: gpt-5.4-mini
  sandbox_mode: workspace-write
  prompt_template: .autoresearch/prompts/codex-system.md
editable_paths: []
readonly_paths: []
```

Set `agent.model` to pass a specific Codex model via `codex exec -m <MODEL>`.

## Development

```bash
uv run pytest
uv run ruff check . --fix
```

For local testing run this:

```bash
uvx --from /home/dschaub/projects/method-projects/easy-autoresearch easy-autoresearch
```
