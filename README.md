# easy-autoresearch

This repository reserves the `easy-autoresearch` package name on
PyPI.

The published package is intentionally minimal and is not intended for
production use. Its only purpose is to prevent name squatting until the real
package is ready.

## What it contains

- A tiny importable Python package
- Packaging metadata in `pyproject.toml`
- A GitHub Actions release workflow that builds and publishes with `uv`

## Releasing

Run the `Release` workflow manually from the GitHub Actions tab. The workflow
will:

1. Build the distribution with `uv build`
2. Publish it to PyPI with `uv publish`

The workflow is configured for PyPI trusted publishing. Before using it, add
this GitHub repository as a trusted publisher in the PyPI project settings.
