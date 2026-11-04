# S21 — Docker images, Compose deployment and an isolation test — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the Collector and the API as two `linux/amd64` images on GHCR, run them on a VPS with one Compose file
(shared SQLite WAL volume, API on an internal network with no egress, reached from the host by its fixed address), and
prove the isolation with a test against that same Compose project.

**Architecture:** One multi-target `Dockerfile` (`collector`, `api`), one production `compose.yaml` that names GHCR
images, and one override `compose.build.yaml` that only swaps the image for a local build. A second committed config
file `config/quasar.compose.toml` differs from `config/quasar.toml` only in `[api].host`. A `compose`-marked pytest
module drives the real project with the Docker CLI. A separate workflow `images.yml` builds, tests and pushes.

**Tech Stack:** Docker Engine (stock Linux for the isolation test), Docker Compose v2, Python 3.12, uv, pytest,
GitHub Actions, GHCR.

**Spec:** `docs/superpowers/specs/2026-10-29-s21-docker-compose-design.md` (revision 2 final; read it in full, section
numbers below refer to it).

## Global Constraints

- **HARD STOP H1 (Task 0) gates every other task.** No Dockerfile, Compose file or workflow is written before the user
  accepts variant 1b on the H1 evidence. Any failed or unrunnable required condition goes back to the user; there is no
  automatic fallback to variant 2 or 3 (spec 5.5).
- No change of source behaviour in `src/` (`domain`, `storage`, `collector`, `api`, `config`). If a task seems to need
  one, stop and ask.
- No environment variable, `.env` file or Compose interpolation changes behaviour (INV-063, spec 5.1). Image tags are
  literal in `compose.yaml`.
- Two containers only: `collector` and `api`. No sidecar, proxy or updater (spec 3).
- The volume is never mounted read-only; read-only stays at the SQLite connection (INV-014, spec section 7 point 4).
- `api`: network `internal` only, fixed address `172.31.250.10` on subnet `172.31.250.0/24`, no `ports:`, no
  `network_mode`. `collector`: networks `internal` and `egress`. (Subnet and address are the builder's call fixed here;
  if H1 shows a clash on the VPS, change both in Task 0 before any file uses them.)
- Images: `ghcr.io/drdoom2012/quasar-market-data-collector` and `ghcr.io/drdoom2012/quasar-market-data-api`,
  platform `linux/amd64`, non-root UID/GID `10001:10001` in both.
- Tags: `sha-<full commit sha>` and `main` on every push to `main`; `v<version>` on a pushed tag `v<version>`, which
  must equal `project.version` in `pyproject.toml`. `compose.yaml` pins `v<project.version>`.
- CI: `check.yml` keeps `contents: read` only. Only the push job of `images.yml` has `packages: write`, and only on
  `push` events. Actions pinned to commit hashes; reuse the two hashes already in `check.yml` and add no third-party
  action (plain `docker` CLI for build, login and push).
- `make check` stays green and needs no Docker: the new marker `compose` is excluded by default like `live`.
- No live exchange call in any automated test: the Collector service is never started by the tests; one-off
  `docker compose run` containers seed and probe instead.
- `docs/invariants.md` evidence must name existing tests at every commit (`tests/test_invariant_register.py`).
- Nothing is pushed by the implementer; the user pushes (Task 6 records the first CI run).

## Review Focus

1. **A fresh named volume owned by root.** Docker copies ownership from the image only if `/app/data` exists in the
   image; otherwise the non-root Collector cannot create the database. Expected: seeding into a brand-new volume as UID
   10001 works. Test `test_seed_into_a_fresh_volume_as_non_root` in Task 4.
2. **The database path inside the container.** The relative `../data/quasar.db` must resolve to `/app/data/quasar.db`
   (the volume), not into the image layer. Expected: the API start line says `database=/app/data/quasar.db`. Test
   `test_api_start_line_names_the_volume_database` in Task 4.
3. **SIGTERM reaches the process.** A shell-form entrypoint makes `sh` PID 1 and `docker stop` kills the process after
   10 s, possibly mid-write. Expected: exec-form entrypoints; `docker compose stop api` returns in under 10 s and the
   container exits `0`. Tests `test_entrypoints_are_exec_form` (Task 3) and `test_api_stops_on_sigterm` (Task 4).
4. **A developer database or virtualenv baked into the image.** `data/quasar.db`, `.venv/` and caches must not be
   copied. Expected: neither image contains `/app/data/quasar.db` nor `.venv`. Test
   `test_images_contain_no_local_state` in Task 2.
5. **An override that quietly changes the network.** A build override that adds `ports:` or a network would make the
   test check a different project from the one deployed. Expected: the override may touch only `image`/`build`. Test
   `test_build_override_changes_only_the_image` in Task 3.

---

### Task 0: HARD STOP H1 — verify variant 1b on stock Linux Docker Engine

**Files:**
- Modify: `docs/superpowers/specs/2026-10-29-s21-docker-compose-design.md` (new "Appendix B. H1 results")

No code. Everything runs in a throwaway directory outside the repository.

- [ ] **Step 1: Record the platform.** On the VPS (stock Docker Engine, not OrbStack or Docker Desktop):
  `docker version`, `uname -rm`, `docker info | grep -iE 'firewall|userland|ipv6'`, `sysctl net.ipv4.ip_forward`.
  `uname -m` also answers U2 (`x86_64` expected).
- [ ] **Step 2: Run Appendix A** (files and commands as written there). Record each output line.
- [ ] **Step 3: Run the VPS-only conditions** of spec 5.5: 5 (the peer reaches the API, recorded as authorized),
  6 (`nc -vz -w 3 <public address> 8000` from another machine fails), 7 (routed access from a machine on the same
  private network or segment, or "not run" with the reason), 8 (`sudo iptables-save` or `sudo nft list ruleset`, and
  `sudo ss -ltnp`), 9 (IPv6: the internal network has no IPv6 subnet, or repeat 2 to 8 for IPv6).
- [ ] **Step 4: Write Appendix B** with a table: condition 1 to 10, expected, observed, pass / fail / not run, and the
  raw evidence below it.
- [ ] **Step 5: STOP.** Report to the user. Continue with Task 1 only after the user accepts variant 1b in writing. If
  any condition failed or a required one was not run, present the evidence and the options of spec 5.5 and wait.
- [ ] **Step 6: Commit** the appendix after the user's decision:
  `git commit -m "docs: S21 H1 results on stock Linux Docker Engine"`.

---

### Task 1: The Compose configuration file and its drift guard

**Files:**
- Create: `config/quasar.compose.toml`
- Test: `tests/config/test_compose_config.py`

**Interfaces:**
- Consumes: `parse_settings(text: str, *, base_dir: Path) -> Settings` and `ApiSettings` from
  `quasar_market_data.config.settings`.
- Produces: `config/quasar.compose.toml`, read by both containers at `/app/config/quasar.compose.toml` (Task 3).

- [ ] **Step 1: Write the failing tests**

```python
REPO = Path(__file__).resolve().parents[2]
MAIN = REPO / "config" / "quasar.toml"
COMPOSE = REPO / "config" / "quasar.compose.toml"


def test_compose_file_differs_from_the_main_file_only_in_api_host() -> None:
    main, compose = tomllib.loads(MAIN.read_text()), tomllib.loads(COMPOSE.read_text())
    assert main["api"]["host"] == "127.0.0.1" and compose["api"]["host"] == "0.0.0.0"
    del main["api"]["host"], compose["api"]["host"]
    assert compose == main


def test_compose_file_is_a_valid_settings_file() -> None:
    settings = parse_settings(COMPOSE.read_text(), base_dir=Path("/app/config"))
    assert settings.api == ApiSettings("0.0.0.0", 8000)
    assert settings.database_path == Path("/app/data/quasar.db")
```

- [ ] **Step 2: Run** `uv run pytest tests/config/test_compose_config.py -v`. Expected: FAIL (file missing).
- [ ] **Step 3: Create `config/quasar.compose.toml`** as a copy of `config/quasar.toml` with `host = "0.0.0.0"` and a
  header comment: used only inside the Compose containers; must stay identical to `quasar.toml` except `[api].host`
  (guarded by this test); the container's only non-loopback interface is the internal network.
- [ ] **Step 4: Run** the tests, then `make check`. Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: config/quasar.compose.toml for the containers, guarded against drift (S21)"`

---

### Task 2: Images

**Files:**
- Create: `Dockerfile`, `.dockerignore`
- Create: `tests/deploy/__init__.py`, `tests/deploy/docker_cli.py`, `tests/deploy/test_images.py`
- Modify: `pyproject.toml` (marker `compose`, `addopts`), `Makefile` (targets `images`, `compose-test`)

**Interfaces:**
- Produces: build targets `collector` and `api` in `Dockerfile`; local tags `quasar-market-data-collector:local` and
  `quasar-market-data-api:local` (used by `compose.build.yaml` in Task 3).
- Produces in `tests/deploy/docker_cli.py`: `docker(*args: str, input: str | None = None, check: bool = True) ->
  subprocess.CompletedProcess[str]` (runs `docker` with text I/O, raises with stdout and stderr on failure when
  `check`); `REPO: Path`; `IMAGES: dict[str, str] = {"collector": "quasar-market-data-collector:local", "api":
  "quasar-market-data-api:local"}`.

- [ ] **Step 1: Marker and targets.** In `pyproject.toml` add marker `compose: tests that need a Docker engine (excluded
  by default)` and change `addopts` to `--strict-markers -m 'not live and not compose'`. In `Makefile`:
  `images` builds both targets with `docker build --platform linux/amd64 --target <t> -t <local tag> .`;
  `compose-test` runs `make images` then `uv run pytest -m compose tests/deploy -v`. Both are "not part of `check`"
  with a comment like the existing `live` target.
- [ ] **Step 2: Write the failing tests** in `tests/deploy/test_images.py`, all `pytestmark = pytest.mark.compose`,
  parametrized over `IMAGES`:
  - `test_image_runs_the_entrypoint_version(name, tag)`: `docker run --rm <tag> --version` prints
    `quasar-collector <version>` / `quasar-api <version>` with `version` from `importlib.metadata`.
  - `test_image_runs_as_uid_10001(name, tag)`: `docker run --rm --entrypoint id <tag> -u` and `-g` print `10001`.
  - `test_image_is_linux_amd64(name, tag)`: `docker image inspect --format '{{.Os}}/{{.Architecture}}'` is
    `linux/amd64`.
  - `test_image_sqlite_is_new_enough(name, tag)`: `python -c` calling `ensure_sqlite_version()` exits `0`.
  - `test_api_image_loads_talib()`: `python -c "import talib; print(talib.__ta_version__)"` exits `0` in the api image.
  - `test_images_contain_no_local_state(name, tag)`: `test ! -e /app/data/quasar.db && test ! -e /app/.venv` exits `0`,
    and `/app/data` exists and is owned by `10001` (`stat -c %u /app/data` prints `10001`).
- [ ] **Step 3: Run** `make images`. Expected: FAIL (no Dockerfile). Only the image tests exist in this task; do not
  run `make compose-test` here (it also collects the Compose and isolation tests of Tasks 3 and 4).
- [ ] **Step 4: Write `Dockerfile`.** A `builder` stage on `python:3.12-slim` (match `.python-version`) installs `uv`
  and runs `uv sync --locked --no-dev --no-editable` into a virtualenv at `/opt/venv` (`UV_PROJECT_ENVIRONMENT`, a
  build-time setting only), so the runtime keeps no build tools and `/app` holds no `.venv`. First check whether `ta-lib==0.8.2` has a `manylinux` `x86_64`
  wheel (`uv pip download` or the build log): if it does, no compiler is needed; if not, the builder stage compiles the
  TA-Lib C library and only its shared library is copied to the runtime stage. A `runtime` stage copies the virtualenv,
  creates user and group `10001`, creates `/app/config` and `/app/data` owned by `10001`, sets `USER 10001:10001` and
  `WORKDIR /app`. Targets `collector` (`ENTRYPOINT ["quasar-collector"]`, `CMD ["run",
  "/app/config/quasar.compose.toml"]`) and `api` (`ENTRYPOINT ["quasar-api"]`, `CMD ["serve",
  "/app/config/quasar.compose.toml"]`). Exec form only. No `ENV` that the application reads.
- [ ] **Step 5: Write `.dockerignore`**: exclude `.git`, `.venv`, `data/`, every cache directory from `.gitignore`,
  `.superpowers/`, `.claude/`, `spikes/`, `tests/`, `docs/`. Keep `README.md`: `pyproject.toml` names it as the
  package readme and hatchling fails the build without it.
- [ ] **Step 6: Run** `make images && uv run pytest -m compose tests/deploy/test_images.py -v`, then `make check`.
  Expected: PASS. These tests need a Docker engine but not stock Linux, so OrbStack is fine here. Record the image sizes
  (`docker image ls`) for the README.
- [ ] **Step 7: Commit** — `git commit -m "feat: collector and api images, linux/amd64, non-root (S21)"`

---

### Task 3: The production Compose project

**Files:**
- Create: `compose.yaml`, `compose.build.yaml`
- Test: `tests/deploy/test_compose_file.py`

**Interfaces:**
- Consumes: `docker`, `REPO`, `IMAGES` (Task 2); `config/quasar.compose.toml` (Task 1).
- Produces in `tests/deploy/docker_cli.py`: `resolved_config(*files: str) -> dict[str, Any]` (JSON of
  `docker compose -f … config --format json`); `PROD = ("compose.yaml",)`, `TEST = ("compose.yaml",
  "compose.build.yaml")`; `API_ADDRESS = "172.31.250.10"`, `API_PORT = 8000`.

- [ ] **Step 1: Write the failing tests** (`pytestmark = pytest.mark.compose`; they need the Docker CLI, not the
  daemon):
  - `test_api_is_on_the_internal_network_only()`: in `resolved_config(*PROD)`, `services.api.networks` has exactly the
    key `internal` with `ipv4_address == API_ADDRESS`; no `ports`; no `network_mode`.
  - `test_internal_network_is_internal_with_the_fixed_subnet()`: `networks.internal.internal is True`, IPAM subnet
    `172.31.250.0/24`, and IPv6 not enabled.
  - `test_collector_is_on_internal_and_egress()`: networks exactly `{internal, egress}`; `egress` is not internal.
  - `test_both_services_mount_the_volume_rw_and_the_config_ro()`: a volume at `/app/data` with no `read_only`, and
    `./config/quasar.compose.toml` at `/app/config/quasar.compose.toml` with `read_only: true`, in both services.
  - `test_restart_limits_and_log_rotation()`: both `restart == "unless-stopped"`, a memory limit set, logging driver
    `json-file` with `max-size` and `max-file`; the two limits together are at most 1 GiB.
  - `test_collector_healthcheck_is_the_process_check()`: `test == ["CMD", "grep", "-qa", "quasar-collector",
    "/proc/1/cmdline"]`; the api has no healthcheck.
  - `test_entrypoints_are_exec_form()`: neither service overrides `entrypoint` or `command` with a string.
  - `test_production_images_pin_the_project_version()`: both `image` values are
    `ghcr.io/drdoom2012/quasar-market-data-<name>:v<project.version>` (version read from `pyproject.toml`).
  - `test_build_override_changes_only_the_image()`: `resolved_config(*TEST)` equals `resolved_config(*PROD)` after
    removing `image` and `build` from each service; with the override, `image` equals `IMAGES[name]` and `build.target`
    equals the service name.
- [ ] **Step 2: Run** `uv run pytest -m compose tests/deploy/test_compose_file.py -v`. Expected: FAIL.
- [ ] **Step 3: Write `compose.yaml`** to satisfy the tests: networks `internal` (`internal: true`, fixed subnet) and
  `egress`; volume `quasar-data`; memory limits `512m` each as a starting point (Step 5 replaces them with measured
  values); `json-file` logging with `max-size: 10m`, `max-file: "3"`; healthcheck interval `30s`, with a comment that it
  proves only that the main process exists (spec 5.4). A header comment gives the deploy commands and the SSH tunnel
  `ssh -L 8000:172.31.250.10:8000 <vps>`.
- [ ] **Step 4: Write `compose.build.yaml`** with only `build: {context: ., target: <service>, platforms:
  [linux/amd64]}` and `image: <local tag>` per service.
- [ ] **Step 5: Measure the memory limits.** A manual step, not a test, because the Collector needs the exchange.
  `docker compose -f compose.yaml -f compose.build.yaml up -d` on a fresh volume (so the 30-day backfill really runs),
  with the starting limits of Step 3 temporarily raised to `1g` each so the measurement is not capped by them.
  - **Peak, primary source:** the cgroup v2 counter `memory.peak` of each container (`docker compose exec <service> cat
    /sys/fs/cgroup/memory.peak`), read at the end. It is the kernel's high-water mark since container start, so short
    spikes between samples are not missed. If the file does not exist (cgroup v1 or a kernel older than 5.19), say so
    and rely on the sampling below.
  - **Sampling, for the time profile:** in parallel, every 2 s for the whole run,
    `docker stats --no-stream --format '{{.Name}} {{.MemUsage}}'` appended with a timestamp to a log in the scratch
    directory. Record the highest sampled value per service and when it occurred. A sampled maximum is a lower bound of
    the true peak, never the peak itself.
  - **Phases covered:** the Collector's whole first backfill, then at least three rounds; for the API, while the
    Collector runs, 8 concurrent repetitions of the `make bench-indicators` worst-case `/v1/indicators` request through
    `172.31.250.10:8000`, repeated 5 times.
  - **Limit:** about twice the higher of `memory.peak` and the sampled maximum, rounded up to 64 MiB, at most 1 GiB for
    both together. If that rule cannot be met inside 1 GiB, stop and report to the user.
  - **Comment above the limits** in `compose.yaml`: date, machine and engine, cgroup version, both peak figures per
    service, the phases covered, and the limitations: one run on one machine, not the VPS architecture unless measured
    there; `memory.peak` includes page cache charged to the container, so it overstates the process heap; a longer
    backfill, more symbols or more concurrent requests are not measured.
  - Restore the computed limits, `docker compose down` (keep the volume only if you want it).
- [ ] **Step 6: Run** `make images && uv run pytest -m compose tests/deploy/test_images.py
  tests/deploy/test_compose_file.py -v`, then `make check`. Expected: PASS. The isolation tests do not exist yet; do not
  run `make compose-test` here.
- [ ] **Step 7: Commit** — `git commit -m "feat: production compose.yaml with internal-only api and a build override (S21)"`

---

### Task 4: The isolation test against the running production project

**Files:**
- Create: `tests/deploy/probe.py`, `tests/deploy/seed_db.py`, `tests/deploy/test_isolation.py`

**Interfaces:**
- Consumes: `docker`, `resolved_config`, `TEST`, `API_ADDRESS`, `API_PORT`, `IMAGES` (Tasks 2, 3).
- Produces:
  - `tests/deploy/probe.py` — stdlib only, run with `python - <mode>` from stdin; modes `egress` (prints one JSON object
    `{"tcp_ip": str, "tcp_name": str, "udp_dns": str, "resolve": str, "default_route": bool, "ipv6_global": bool}`,
    each string `"ok"` or `"fail:<errno name>"`) and `http HOST PORT PATH` (prints the status code or `fail:<error>`).
    Targets: `1.1.1.1:443`, `fapi.binance.com:443`, a DNS query for `example.com` to `1.1.1.1:53`, resolving
    `fapi.binance.com`. Logic as in spec Appendix A `probe.py`; `ipv6_global` from `/proc/net/if_inet6` (scope `00`).
  - `tests/deploy/seed_db.py` — `python - <db_path>`: `open_writer`, `migrate(conn, applied_at_ms=SEED_AT_MS)`, then
    `CandleWriter(conn).write(...)` of `SEED_COUNT = 12` consecutive `5m` candles of `BTC/USDT:USDT` from
    `SEED_START_MS = 1_767_225_600_000`, built with `parse_klines` and `require_valid` (row shape as in
    `tests/collector/fetch_fakes.kline_row`, copied, since `tests/` is not in the image). Package imports only.
- Module fixture `project` (scope `module`): fails the module with a clear message unless `docker info` reports a stock
  engine (`OperatingSystem` contains neither `OrbStack` nor `Docker Desktop`); runs `docker compose -p s21-test -f
  compose.yaml -f compose.build.yaml up -d api`; seeds with `docker compose -p s21-test … run --rm --no-deps -T
  --entrypoint python collector - /app/data/quasar.db` with `seed_db.py` on stdin; waits until `GET /health` on
  `API_ADDRESS:API_PORT` answers `200` (at most 30 s); teardown `down -v`.

- [ ] **Step 1: Write the failing tests** (`pytestmark = pytest.mark.compose`):
  - Positive: `test_host_reaches_the_api_and_gets_seeded_candles()` — from the test process, `GET /v1/candles` for the
    seeded symbol over `[SEED_START_MS, SEED_START_MS + 12 * 300_000)` returns `200` and 12 candles with the seeded open
    times (field names per `docs/contracts/api-v1.md` 6.1); `GET /health/ingest` returns `200` and `series_count == 1`.
  - Positive: `test_probe_succeeds_from_the_collector()` — `egress` probe in a one-off `collector` container (`run --rm
    --no-deps -T --entrypoint python collector -`): `tcp_ip`, `tcp_name`, `udp_dns`, `resolve` are `"ok"`. On failure
    the message says the negative checks would prove nothing.
  - `test_seed_into_a_fresh_volume_as_non_root()` — the seed step of the fixture exited `0` (kept as a test so a
    failure names it), and `stat -c %u /app/data/quasar.db` in the api container prints `10001`.
  - `test_api_start_line_names_the_volume_database()` — `docker compose logs api` contains
    `database=/app/data/quasar.db` and `config path=/app/config/quasar.compose.toml`.
  - Networks: `test_running_api_has_exactly_the_internal_network()` — `docker inspect` of the api container: one
    network, its name ends with `_internal`, `IPAddress == API_ADDRESS`; `docker network inspect` says `Internal:
    true`; `NetworkSettings.Ports` has no host binding.
  - Routing and egress: `test_api_has_no_route_and_no_egress()` — `egress` probe via `docker compose exec -T api python
    -`: `default_route is False`, `ipv6_global is False`, `tcp_ip`, `tcp_name`, `udp_dns` and `resolve` all start with
    `"fail:"`, and `udp_dns == "fail:ENETUNREACH"` (an immediate error, not a timeout).
  - `test_unauthorized_containers_cannot_reach_the_api()` — `http API_ADDRESS API_PORT /health` from a throwaway
    container on the default bridge and from one on a fresh user-defined network (created and removed by the test)
    prints `fail:…`.
  - `test_engine_installed_the_isolation_rules()` — `sudo -n iptables-save` (or `sudo -n nft list ruleset` when
    `docker info` reports the nftables backend) contains a drop of traffic leaving the project's internal bridge to
    destinations outside `172.31.250.0/24` and a drop of traffic entering it from outside that subnet; on any other
    shape, fail and attach the dump plus the engine version and backend.
  - `test_api_stops_on_sigterm()` — runs last: `docker compose stop -t 10 api` takes under 10 s and the container's
    exit code is `0`.
- [ ] **Step 2: Run** `make compose-test` on a stock Linux engine (the VPS, a Linux machine, or wait for CI in Task 5).
  Expected: FAIL (helpers missing). On OrbStack the module fails at the fixture with the stock-engine message; that is
  correct and is not a pass.
- [ ] **Step 3: Write `probe.py` and `seed_db.py`** to the interfaces above.
- [ ] **Step 4: Run** `make compose-test` on a stock Linux engine. This is the first task in which every `compose`
  test exists, so the full suite (images, Compose file, isolation) must pass here. Expected: PASS. If
  `test_unauthorized_containers_cannot_reach_the_api` or `test_engine_installed_the_isolation_rules` fails, stop: that
  is an H1 condition failing on this engine; report to the user.
- [ ] **Step 5: Run** `make check`. Expected: PASS (the `compose` tests are deselected).
- [ ] **Step 6: Commit** — `git commit -m "test: isolation test against the running production compose project (S21)"`

---

### Task 5: CI workflow `images.yml`

**Files:**
- Create: `.github/workflows/images.yml`
- Test: `tests/deploy/test_workflow.py` (default suite, no Docker)

**Interfaces:**
- Consumes: `make images`, `make compose-test` (Tasks 2 to 4); action hashes from `check.yml`.

- [ ] **Step 1: Write the failing tests.** No YAML parser is installed and none is added: the tests assert on the
  workflow text line by line, and the job-scoping checks find the job by its `  <name>:` line and read until the next
  line with the same indentation.
  - `test_check_workflow_keeps_read_only_permissions()`: `check.yml` contains `contents: read` and no `write`.
  - `test_images_workflow_grants_packages_write_only_to_the_push_job()`: top-level `permissions: contents: read`; the
    string `packages: write` appears once, inside the job named `push`; that job has
    `if: github.event_name == 'push'` and `needs: test`.
  - `test_images_workflow_pins_every_action_by_hash()`: every `uses:` line matches `@[0-9a-f]{40}`.
  - `test_images_workflow_uses_no_third_party_action()`: `uses:` values are only `actions/checkout` and
    `astral-sh/setup-uv`.
  - `test_tag_push_checks_the_project_version()`: the push job contains a step that fails when
    `${GITHUB_REF_NAME}` differs from `v` plus `project.version`.
- [ ] **Step 2: Run** `uv run pytest tests/deploy/test_workflow.py -v`. Expected: FAIL.
- [ ] **Step 3: Write `images.yml`.** Triggers: `pull_request`, `push` to `main`, `push` of tags `v*`. Top-level
  `permissions: contents: read`. Job `test` (`ubuntu-latest`, `timeout-minutes: 30`): checkout
  (`persist-credentials: false`), setup-uv, `make compose-test`. Job `push` (`needs: test`, `if: github.event_name ==
  'push'`, `permissions: {contents: read, packages: write}`): checkout, `make images`, tag check on tag pushes,
  `docker login ghcr.io -u "${{ github.actor }}" --password-stdin` fed from `${{ secrets.GITHUB_TOKEN }}`, then tag
  and push both images as `sha-${{ github.sha }}` and `main` (pushes to `main`) or as `${GITHUB_REF_NAME}`, which is
  `v<project.version>`, and `sha-${{ github.sha }}` (tag pushes). The `v<version>` tag that `compose.yaml` pins exists
  in GHCR **only** after a tag push whose `push` job succeeded; a push to `main` never creates it. Builds run once in
  `push` again rather than sharing artifacts (simplicity over speed; the builder's call).
- [ ] **Step 4: Run** the tests and `make check`. Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "ci: build, test and push the images to GHCR (S21)"`

---

### Task 6: Documentation, invariants and the first CI run

**Files:**
- Modify: `README.md`, `docs/invariants.md`, `docs/follow-ups.md`,
  `docs/superpowers/specs/2026-10-20-market-data-v1-design.md` (roadmap line S21)

- [ ] **Step 1: README** — new section "Deployment (S21)": what the two images are; the release rule (`compose.yaml`
  pins `v<project.version>`, which exists in GHCR only after the tag `v<project.version>` was pushed and its
  `images.yml` run succeeded; a push to `main` publishes only `main` and `sha-…`); first start on the VPS (only after
  the release check of Step 7: copy `compose.yaml` and `config/quasar.compose.toml` from the tagged commit,
  `docker login ghcr.io` with a read-only token if the packages are private, `docker compose pull && docker compose up
  -d`); upgrade (bump `project.version` and the tag in `compose.yaml` in one commit, push it and tag `v<version>` on
  that commit, wait for the green `images.yml` run of the tag, then pull and up on the VPS); the SSH tunnel
  `ssh -L 8000:172.31.250.10:8000 <vps>`; what the healthcheck means and does not mean (spec 5.4, one paragraph); that
  a copy of the live database needs the `-wal` file or `sqlite3 .backup`; `make images` and `make compose-test`, and
  that the isolation test needs a stock Linux engine. Add both targets to the command table.
- [ ] **Step 2: Invariants** (spec 5.8):
  - Add **INV-069** as **PLANNED** with the wording of spec 5.8, mechanism `compose.yaml` plus
    `tests/deploy/test_isolation.py`, target "ENFORCED after the first green `images.yml` run is observed", and the
    "Not covered" list of spec 5.6.3.
  - INV-013: stays PLANNED; target text says S21 added deployment-level network isolation (INV-069) and the application
    claim stays open (U5).
  - INV-014: replace the "S21 stays the target …" sentence with the spec 5.8 wording.
  - INV-044: append to "Not covered" a pointer to INV-069 for UDP and name resolution in the deployment only.
- [ ] **Step 3: Follow-ups** — F-16: add that S21 added restart policy and a process healthcheck, neither of which is
  an ingestion health signal; status stays open. New entry: the routed-access check of H1 condition 7 if Appendix B
  recorded it as not run.
- [ ] **Step 4: Roadmap** — S21 line: "implemented (plan `2026-10-29-s21-docker-compose.md`)".
- [ ] **Step 5: S21 acceptance run.** On a stock Linux engine: `make compose-test` (the full `compose` suite) and
  `make check`. Expected: both PASS. S21 is not complete without this run; record the engine version and the date in
  the commit message body.
- [ ] **Step 6: Commit** — `git commit -m "docs: S21 deployment, INV-069 and the invariant links"`
- [ ] **Step 7: Hand over to the user for the push and the first release.** The implementer pushes nothing. The user
  pushes `main`, then tags the commit whose `compose.yaml` pins `v<project.version>` (today `v0.1.0`) and pushes the
  tag. **Release check, before any `docker compose pull` on the VPS** (all must hold; otherwise do not deploy and
  report):
  1. `gh run list --workflow images.yml` shows a successful run for the `main` push and a successful run for the tag
     `v<project.version>`, with both jobs `test` and `push` green in the tag run.
  2. For both images, `docker buildx imagetools inspect ghcr.io/drdoom2012/quasar-market-data-<name>:v<project.version>`
     succeeds and lists `linux/amd64`.
  3. The tag's commit is the one whose `compose.yaml` pins that tag: `git show v<project.version>:compose.yaml | grep
     ':v<project.version>'` matches both `image:` lines.
  4. H1 on the VPS (spec Appendix B.2, moved here by the user's decision B.3): conditions 1 to 10 of spec 5.5 run on
     the VPS and recorded in B.2; every required condition passes. A failure or an unrunnable required condition stops
     the deployment and goes to the user.
- [ ] **Step 8: First deployment check on the VPS** (the user, after Step 7): `docker compose pull` succeeds; `docker
  compose ps` shows both services running and `collector` `healthy` (a process check only, spec 5.4); over
  `ssh -L 8000:172.31.250.10:8000 <vps>`, `curl -s localhost:8000/health` returns `200`; after one round,
  `/health/ingest` shows stored series.
- [ ] **Step 9: INV-069.** If Step 7 held (both jobs green, so the isolation test ran in CI on a stock engine), change
  INV-069 to **ENFORCED** with the `tests/deploy/test_isolation.py` test names as evidence, note the run URL in the
  README's CI paragraph, `make check`, and commit `docs: INV-069 enforced by the first green images.yml run`. If a run
  failed, report it and do not edit the register.

---

## Spec coverage

| Spec | Task |
|------|------|
| 5.5 H1 | 0 |
| 5.2 compose config and drift guard | 1 |
| 5.1 images, TA-Lib, non-root, no env | 2 |
| 5.3 Compose, limits, logs, fixed address, override | 3 |
| 5.4 healthcheck and its limits | 3 (config), 6 (docs, F-16) |
| 5.6 isolation test on the production project | 4 |
| 5.7 CI, GHCR, permissions, tags | 5 |
| 5.8 invariants, README, roadmap | 6 |
| U1 (GHCR names, private) | Global Constraints; README in 6 |
| U2 (amd64) | 0 step 1 |
| U3 (second config file) | 1 |
| U4, U5 | not implemented; user decisions |
