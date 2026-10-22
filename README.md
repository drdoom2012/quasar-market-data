# quasar-market-data
Independent Python market data service for Quasar. Collects, validates, and stores cryptocurrency market data using CCXT/CCXT Pro, with technical indicators powered by TA-Lib and REST APIs via FastAPI.

## Commands

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is pinned in `.python-version`.

| Command | Does |
|---|---|
| `uv sync` | Create the environment and install dev dependencies |
| `make test` | `uv run pytest` |
| `make lint` | `ruff check` and `ruff format --check` on `src tests spikes` |
| `make typecheck` | `mypy` (strict) |
| `make imports` | `lint-imports` (module boundaries; configured in a later task) |
| `make check` | `lint`, `typecheck`, `imports`, `test` |

CI runs `make check` on pull requests and pushes to `main`.

## Local hook

`.claude/settings.json` registers one Claude Code `PreToolUse` hook (matcher `Bash`) that runs `scripts/hooks/pre_commit_check.sh`. When the Bash command contains the text "git commit", it runs `make check` first and blocks the call (exit 2, tail of the output on stderr) if it fails. This is a local convenience only; CI is the authoritative gate. Override the command with `QUASAR_CHECK_CMD` (used by `tests/test_pre_commit_hook.py`).

Console scripts: `quasar-collector` and `quasar-api` (`--version` only for now).

## Tool scope

| Tool | Covers | Does not cover |
|---|---|---|
| `pytest` | `tests/` only (`testpaths`), `live` marker excluded, `--strict-markers` | `spikes/` |
| `ruff check` / `ruff format --check` | `src/`, `tests/`, `spikes/` | — |
| `mypy` (strict) | `src/`, `tests/` | `spikes/` |
| `lint-imports` | package `quasar_market_data` | `tests/`, `spikes/` |

## Boundary enforcement

`domain` is pure: it may import only the stdlib modules in `ALLOWED_EXTERNAL_IMPORTS` (`tests/test_domain_purity.py`: `__future__`, `collections`, `dataclasses`, `decimal`, `enum`, `re`, `typing`) and itself. Adding a module means editing that constant, visible in review. `collector` and `api` must never import each other.

| Guard | Catches | Does not catch |
|---|---|---|
| AST allowlist test (`tests/test_domain_purity.py`) | every direct `import` / `from … import` in `domain/` (also inside functions), relative imports with level ≥ 2, calls to `open`, `__import__`, `eval`, `exec`, `compile` | dynamic imports with computed names; transitive chains (direct-import check only) |
| import-linter `forbidden` contract | `domain` reaching `collector`, `api`, `ccxt`, `fastapi`, `talib` or `sqlite3`, directly or through other modules in the graph | what happens inside third-party package internals |
| import-linter `independence` contract | `collector` ↔ `api` imports, direct or via other graph modules | — |

The import-linter contracts are proven by automated negative tests (`tests/test_import_contracts_negative.py`): each case copies the package and `pyproject.toml` into a temp directory, injects a violating module into the copy and asserts that `lint-imports` fails naming the broken contract. The original manual probes (temporary files, since removed):

- **P1** `domain/_probe_direct.py` with `import sqlite3`: both `make imports` (forbidden contract) and the AST test failed.
- **P2** `domain/_probe_indirect.py` importing a helper outside `domain` that imports `collector.cli`: `make imports` reported the chain `domain._probe_indirect -> _probe_helper -> collector.cli`; the AST test failed on the first import (`from quasar_market_data import _probe_helper`).
- **P3** `import quasar_market_data.api.cli` added to `collector/cli.py`: only `make imports` (independence contract) failed. The AST test covers `domain/` only and does not look at `collector`.

All forbidden entries (including stdlib `sqlite3` and not-installed `ccxt`, `fastapi`, `talib`) were accepted by import-linter without errors.
