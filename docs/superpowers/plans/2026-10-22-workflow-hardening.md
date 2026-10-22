# Workflow Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Make `make check` an authoritative CI gate, add a minimal local pre-commit hook, replace prompt-only rules with an honest invariant register backed by real negative tests, fix `CLAUDE.md`, and start a three-column metrics table.

**Architecture:** No source-architecture change. Only CI config, one Claude Code hook, tests, and docs are touched. `src/` is not modified.

**Spec:** Approved in conversation (final minimal plan, 2026-10-22). Also `CLAUDE.md`, `docs/architecture.md`, `docs/adr/0001-*.md` (read-only: must not change), `docs/superpowers/plans/2026-10-20-slices-s1-s4.md` (slice numbering).

## Global Constraints

- Branch `market-data-init`. One logical commit per task. Do **not** push, do not log into GitHub, do not change remote settings, do not create worktrees.
- Do not modify `docs/adr/`, `docs/architecture.md`, `src/`, or `uv.lock`. Do not add dependencies to `pyproject.toml`. No new frameworks, no custom orchestration.
- CI is the authoritative gate; the local hook is a convenience only. No stamp/cache files, no Stop hook, no Edit/Write hooks.
- ENFORCED invariants require a real negative test (a test that fails when the invariant is violated), running in `make check`.
- Never claim a check passed unless it ran; record real command output. GitHub Actions must not be described as working: it cannot be run here.
- Test style: pytest, mypy strict (`tests/` is type-checked), ruff line length 120. Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- `make check` currently passes (40 tests, ~0.7 s).

## Task 1: GitHub Actions CI

**Files:**
- Create: `.github/workflows/check.yml`
- Modify: `README.md` (one sentence in the Commands section)

**Requirements:**
- Triggers: `pull_request` and `push` to `main`.
- `permissions: contents: read`; one job on `ubuntu-latest`; no secrets, no network access to exchanges.
- Steps: `actions/checkout`, `astral-sh/setup-uv`, then `make check`. Python version is the one in `.python-version` (let `uv` read it; do not hard-code a second copy).
- Dependencies must be locked: set env `UV_LOCKED: "1"` (verify in uv docs/`uv --help` that this env var is honored by `uv sync`/`uv run`; if not, use `uv sync --locked` and `uv run --locked` equivalents without editing the Makefile).
- Pin both third-party actions by full commit SHA with a trailing `# vX.Y.Z` comment. Resolve SHAs from public data with `git ls-remote https://github.com/<owner>/<repo> 'refs/tags/<tag>*'` (dereferenced `^{}` entry for annotated tags). Use a current major release tag; do not invent SHAs.
- README: one sentence stating CI runs `make check` on pull requests and pushes to `main`, with no claim that it has been observed running.

**Verification (record real output in your report):**
1. Validate the YAML parses (e.g. `ruby -ryaml -e 'YAML.load_file(".github/workflows/check.yml")'`; do not install anything).
2. Run `UV_LOCKED=1 make check` locally: passes.
3. Show that locked mode really rejects a stale lock: in a scratch copy under the scratchpad directory (not in the repo), edit `pyproject.toml` dependencies so the lock is stale and show `UV_LOCKED=1 uv sync` fails. If that cannot be demonstrated, say so.
4. State explicitly in the report that the workflow itself has NOT been run on GitHub.

**Commit:** `ci: run make check on GitHub Actions with locked dependencies`

## Task 2: Minimal PreToolUse hook before `git commit`

**Files:**
- Create: `.claude/settings.json`
- Create: `scripts/hooks/pre_commit_check.sh` (executable)
- Create: `tests/test_pre_commit_hook.py`
- Modify: `README.md` (short "Local hook" note: convenience only, CI is authoritative)

**Requirements:**
- `.claude/settings.json` registers exactly one hook: `PreToolUse`, matcher `Bash`, command running the script via the project dir (use `$CLAUDE_PROJECT_DIR`). Before writing, check the current Claude Code hooks documentation for the exact settings schema and the stdin/exit-code contract (WebFetch/WebSearch on code.claude.com docs). Do not rely on memory.
- The script reads the hook JSON from stdin. If it does not contain the text `git commit`, exit 0 immediately (no work). If it does, run the check command and, on failure, print the tail of its output to stderr and exit with the blocking code from the docs (expected: 2). On success exit 0. Plain shell; no `jq`; false positives (e.g. `git commit` appearing in other text) only cost one `make check` and are acceptable.
- The check command defaults to `make check` and is overridable through env var `QUASAR_CHECK_CMD` (the test seam; the test must never invoke the real `make check`, which would recurse).
- Script must `cd` to the project root before running the command.
- No stamp/cache, no other hook events.

**Tests (TDD, `tests/test_pre_commit_hook.py`, subprocess against the script):**
- non-commit payload (e.g. `ls`) → exit 0 and the check command is not run (use a check command that would fail or write a marker file).
- commit payload with `QUASAR_CHECK_CMD=true` → exit 0.
- commit payload with `QUASAR_CHECK_CMD="echo boom >&2; false"` (or similar) → blocking exit code and `boom` present in stderr.
- Payload shape must match what the docs say the real hook receives.

**Verification:** `uv run pytest tests/test_pre_commit_hook.py`, then `make check`. Also feed one real-shaped payload to the script with the default command and show it runs `make check` and exits 0. State what could not be verified (e.g. that Claude Code actually invokes the hook in a live session).

**Commit:** `chore: add minimal PreToolUse hook that runs make check before git commit`

## Task 3: Negative tests for the import-linter contracts

**Files:**
- Create: `tests/test_import_contracts_negative.py`
- Modify: `README.md` Boundary enforcement section (replace the sentence that these were only manually probed with a pointer to the automated test; keep the P1–P3 history)

**Why:** `docs/invariants.md` (Task 4) may only call the import-linter contracts ENFORCED if a test fails when they are violated. Today only manual probes (README P1–P3) exist.

**Requirements:**
- Do not change `pyproject.toml` contracts or `src/`. The test must exercise the **real** contract configuration: copy `src/quasar_market_data` and `pyproject.toml` into `tmp_path`, inject a violating module in the copy, run `lint-imports` against the copy with `--config` pointing at the copied `pyproject.toml`, `cwd` and `PYTHONPATH` set so the copy (not the repo's package) is imported, and assert on exit code and on the contract name in the output.
- Cases (parametrized): (a) positive control — unmodified copy passes (exit 0, "2 kept"); (b) `domain/_probe.py` containing `import sqlite3` → fails, output names "domain is pure"; (c) `collector/_probe.py` importing `quasar_market_data.api` → fails, output names "collector and api are independent"; (d) `domain/_probe.py` importing `quasar_market_data.collector` → fails on "domain is pure".
- Locate the `lint-imports` executable robustly (next to `sys.executable`, fall back to `shutil.which`), without installing anything. The test must not touch the repo working tree.
- Run time should stay small; report it.

**Verification:** `uv run pytest tests/test_import_contracts_negative.py`; prove the test is real by temporarily weakening a contract in the **copy** only (or removing a forbidden module in a scratch config) and showing the corresponding case fails; do not leave any repo change from that experiment. Then `make check`.

**Commit:** `test: add negative tests proving import-linter contracts fail on violations`

## Task 4: Invariant register (ENFORCED / PLANNED / NOT_APPLICABLE)

**Files:**
- Create: `docs/invariants.md`
- Create: `tests/test_invariant_register.py`

**Register format:** one markdown table with columns `ID | Invariant | Status | Mechanism | Evidence / Target / Reason`. Above the table define the statuses:
- `ENFORCED` — a check runs in CI (`make check`) **and** a named negative test exists that fails when the invariant is violated. Evidence cell = `path/to/test.py::test_name`. A passing positive test alone does not qualify.
- `PLANNED` — not enforced yet; evidence cell names the slice/task that will enforce it, taken from `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` or the S1–S4 plan (e.g. S3 Task 6, S4, S7, S8). If no slice names it, write `unscheduled`. Do not invent slice numbers.
- `NOT_APPLICABLE` — decided out of scope; evidence cell gives the reason.

**Initial rows (derive text from `CLAUDE.md` "Non-Negotiable Rules"; one row per rule, split a rule if only part is enforceable):**
- Collector and API do not import each other → ENFORCED, import-linter `independence`; evidence `tests/test_import_contracts_negative.py::<case>` (use the real test name from Task 3).
- Domain does not import collector, api, ccxt, fastapi, talib, sqlite3 → ENFORCED, import-linter `forbidden` + AST test; evidence `tests/test_import_contracts_negative.py::<case>` and/or `tests/test_domain_purity.py::test_find_violations_counts`.
- Domain imports only the stdlib allowlist and makes no I/O calls (`open`, `eval`, ...) → ENFORCED, AST test; evidence `tests/test_domain_purity.py::test_find_violations_counts`.
- Exact decimals, no float for canonical data → PLANNED (decimal text task in the S1–S4 plan, Task 6).
- Persist only confirmed closed candles → PLANNED.
- Writer accepts only `ValidatedCandle` → PLANNED (S7 per the plan's Global Constraints).
- Never silently overwrite conflicting data → PLANNED.
- Never fabricate missing candles/prices/volumes/indicators → PLANNED.
- Fail closed on missing/stale/inconsistent data → PLANNED.
- Ingestion is restart-safe and idempotent → PLANNED.
- API handlers never fetch exchange data; API is read-only on SQLite, Collector is the only writer → PLANNED.
- Standard tests never hit live exchanges / production storage → PLANNED (the `live` marker only excludes tests that are marked; it does not stop an unmarked network test).
- No trading functionality or private trading credentials → NOT_APPLICABLE as a mechanism: scope prohibition, kept by review; state that reason.
- Accepted ADRs are not changed without approval → NOT_APPLICABLE as a mechanism (process rule, human approval).

**Register test (`tests/test_invariant_register.py`, TDD):** a small parser for the table plus tests that check **register integrity only** (state this in the module docstring: it does not prove any invariant holds). Checks: IDs unique and non-empty; status in the three allowed values; every ENFORCED row's evidence is `path::name` where the file exists and defines `def name`; every PLANNED row has a non-empty target; every NOT_APPLICABLE row has a non-empty reason; at least one row of each of ENFORCED and PLANNED exists. Include unit tests of the parser/validator with bad inline tables (unknown status, ENFORCED pointing to a missing test, duplicate ID, empty target) proving each is rejected.

**Verification:** `uv run pytest tests/test_invariant_register.py`, then `make check`. Show that changing an ENFORCED evidence reference to a non-existent test makes the register test fail (in a scratch copy or by an in-test negative case, not left in the repo).

**Commit:** `docs: add invariant register with ENFORCED/PLANNED/NOT_APPLICABLE statuses and integrity test`

## Task 5: Fix CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

**Requirements:**
- First read the file's actual ending (`tail -c 400 CLAUDE.md`) and the whole file; it is reported to end mid-sentence ("If a referenced document does not exist, report i"). Complete the sentence sensibly ("report it before proceeding." or similar wording consistent with the rest) without altering any rule.
- Fix the malformed links of the form `[CLAUDE.md](http://CLAUDE.md)`, `[architecture.md](http://architecture.md)`, `[README.md](http://README.md)` to plain text or relative links (`docs/architecture.md`, `README.md`), and the first heading.
- Add one bullet to the References list: `docs/invariants.md` — invariant register with enforcement status.
- Change nothing else: no rule is added, removed or reworded (the user has approved only this fix). Show `git diff` in the report.

**Verification:** `git diff --stat` touches only `CLAUDE.md`; `make check` passes; confirm by reading that the file now ends cleanly and no `http://` pseudo-links remain (`grep -n 'http://' CLAUDE.md`).

**Commit:** `docs: fix truncated sentence and malformed links in CLAUDE.md, reference invariant register`

## Task 6: Minimal metrics

**Files:**
- Create: `docs/metrics.md`

**Requirements:** exactly three metrics, tokens explicitly not measured.
- Definitions: *CI first-pass* = the first CI run on a PR's first pushed head is green (yes/no). *Review cycles* = number of review rounds that requested changes before approval. *Confirmed defects* = defects in the PR's code later confirmed by a human or a failing test, with link and date; reviewer suspicions that were not confirmed do not count.
- One table, one row per pull request, columns `PR | CI first-pass | Review cycles | Confirmed defects`; entered by hand at merge. Leave it empty except a note "no data yet; first row is the first PR that has CI". Do not fabricate or back-fill S1–S4.
- A short "Not measured" section: tokens, wall-clock time, per-agent cost (revisit only if a decision needs them).

**Verification:** `make check` passes (docs only); the file contains no invented numbers.

**Commit:** `docs: add minimal agentic-development metrics (CI first-pass, review cycles, confirmed defects)`
