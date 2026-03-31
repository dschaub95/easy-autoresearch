# easy-autoresearch

Scaffolding for an autonomous repository-optimization loop aimed at Codex first.

Current scope:

- scaffold a target repository with local config, prompts, and SQLite state
- run a session loop that records `sessions`, `experiments`, and `runs`
- retry across multiple experiments and runs according to config limits
- keep the Codex integration boundary explicit, but still stubbed

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
session:
  max_duration_seconds: 3600
experiments:
  max_experiments: 1
  max_runs_per_experiment: 1
codex:
  command: codex
  prompt_template: .autoresearch/prompts/codex-system.md
editable_paths: []
readonly_paths: []
```

## Development

```bash
uv run pytest
uv run ruff check . --fix
```
