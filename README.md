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
```

If no repo path is provided, easy-autoresearch uses the current working
directory.

On startup it checks for an existing setup:

- if no setup exists, it scaffolds the repo and starts a session
- if a setup exists, it prompts to continue or overwrite
- `--overwrite` skips the prompt and recreates the setup automatically

Scaffolding creates:

- `autoresearch.yaml`
- `.autoresearch/state.db`
- `.autoresearch/prompts/codex-system.md`

Starting a session:

- opens one `sessions` row
- runs up to `experiments.max_experiments`
- runs up to `experiments.max_runs_per_experiment` within each experiment
- for candidate experiments, runs three consecutive agent calls in the same session
  before executing `commands.agent_run`
- stores each run's output and metric in SQLite
- stops early when an experiment completes successfully

## Default Config Shape

```yaml
project:
  repo_path: /absolute/path/to/repo
  name: my-repo
commands:
  baseline: uv run pytest
  metric_pattern: null
  agent_run: uv run pytest
  agent_metric_pattern: null
session:
  max_duration_seconds: 3600
experiments:
  max_experiments: 1
  max_runs_per_experiment: 1
agent:
  provider: codex
  model: null
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
