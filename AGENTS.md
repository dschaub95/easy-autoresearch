# Agent instructions

- Always use the OpenAI developer documentation MCP server if you need to work with the OpenAI API, ChatGPT Apps SDK, Codex,… without me having to explicitly ask.
- You can find reference implementations in the .references/ directory. These might serve as inspiration, but you should not use them directly and clarify with me.
- Use `uv run` for python related commands.
- After substantive code changes, run `uv run pytest` and `uv run ruff check . --fix` before finishing.
- By default, changes should not be backwards compatible unless explicitly asked for. There might be legitimate reasons for backwards compatibility, but you should clarify with me first.
