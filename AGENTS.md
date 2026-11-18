# Agent guide

Commands:

- `uv sync`
- `uv run pytest` (default set: no Docker, database or network)
- `uv run pytest -m db` (needs the compose Postgres and `FACTORY_TEST_DATABASE_URL`)
- `uv run ruff check . && uv run ruff format --check .`
- `uv run pyright`

The Shared interfaces in `ref/plan.md` are binding: use the names, signatures and paths as given.
