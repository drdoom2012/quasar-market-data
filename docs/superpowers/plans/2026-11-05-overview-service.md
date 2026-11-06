# Symbol overview as a third Compose service — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the symbol overview page into a third Docker image and run it as a third Compose service next to `collector` and `api`.

**Architecture:** A new Dockerfile target `overview` (standard library only, no venv) runs `python -m tools.symbol_overview` on the `internal` network at a fixed address, reaching the API over HTTP only; reached from the user's machine by an SSH tunnel like the API. One new CLI option `--host` lets the container bind `0.0.0.0` while the local default stays loopback.

**Tech Stack:** Python 3.12 stdlib, Docker / Compose, pytest (marker `compose` for Docker tests), ruff, mypy, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-05-overview-service-design.md` (read it first; section numbers below refer to it).

## Global Constraints

- Branch `feat/overview-service` from `main`; one PR, squash-merged (PR title and body are written as the squash commit message). Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`; the PR body ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Do **not** bump `project.version`, tag, or push a tag (user rule: only when the user says deploy).
- No change to `src/`, `config/`, database schema, `docs/contracts/`, import-linter contracts, `compose.local.yaml`, `.dockerignore`.
- `tools/symbol_overview` stays standard library only (`tests/tools/symbol_overview/test_boundary.py` must stay green and unedited).
- Overview service values: address `172.31.250.11`, port `8765`, network `internal` only, no `ports`, no `volumes`, no `depends_on`, no `profiles`, `init: true`, `mem_limit: 128m`, `memswap_limit: 128m`, `restart: unless-stopped`, `logging: *logging`, image `ghcr.io/drdoom2012/quasar-market-data-overview:v<project.version>`, API URL `http://172.31.250.10:8000`.
- Total `mem_limit` of all services: cap becomes **1088 MiB** (384 + 576 + 128), stated once as a named constant.
- Tests that need Docker carry `pytestmark = pytest.mark.compose` and are excluded from `make check`. Never claim a check passed that did not run; the compose tests need a stock Linux Docker Engine and may not run on this machine (report it).
- The shell tool may be unavailable intermittently; use dedicated file tools where possible.
- `compose.yaml` pins `v<project.version>` (today `v0.1.0`) for all three images. A reviewer reports that the git tag `v0.1.0` already exists (verify with `git tag --list 'v*'` before writing the PR text); then `ghcr.io/.../quasar-market-data-overview:v0.1.0` can never be produced without re-tagging a released version, and the overview image exists in GHCR only after the **next** version bump and tag, which only the user triggers. Until then `compose.yaml` on `main` references an image that does not exist: the VPS keeps using `git show v0.1.0:compose.yaml`. The existing pin test only compares the pin with `pyproject.toml`; it cannot see a missing tag. State this in the README "Release rule" and in the PR body.
- `make check` runs at the end of **every** task, before the commit (the default suite contains tests that read `compose.yaml`, the Dockerfile and the tools).
- Request logging of the tool (`BaseHTTPRequestHandler.log_message`) stays as it is; the overview healthcheck interval is therefore `60s` (less noise in the rotated log).

## Review Focus

Failure modes the spec implies but a happy path would not exercise, most likely first:

1. `docker stop` of the overview with Python as PID 1: SIGTERM is ignored without `init` → 10 s wait and SIGKILL. Expected: stops in under 10 s (Task 2 test `test_overview_stops_on_sigterm`).
2. `--host` value that cannot be bound (an address that is not local) or empty: expected a usage error, exit `2`, a message on stderr, no traceback (Task 1 tests `test_unbindable_host_is_a_usage_error`, `test_empty_host_is_a_usage_error`). A port already in use becomes the same usage error (exit 2); that is intended.
3. API unreachable when the page asks: expected status `502` and exactly `{"api_error": {"code": "transport", "detail": "connection"}}`, never an empty or green page (Task 2 test `test_unreachable_api_is_a_page_level_error`).
4. The overview must not have a route out even though it is a new service on a shared network: expected `default_route False`, all egress probes fail (Task 2 test `test_overview_has_no_route_and_no_egress`).
5. `make overview` / `mprocs` must still bind loopback only: expected default `host == "127.0.0.1"` (Task 1 test `test_defaults`, extended).

---

### Task 1: `--host` option

**Files:**
- Modify: `tools/symbol_overview/cli.py` (replace `HOST` constant by an option; the comment at line 11, the `--port` help text "port of this page on 127.0.0.1", the parser `description` "A local, read-only page ...", the start-up line)
- Modify: `tools/symbol_overview/server.py:3-4` (module docstring)
- Test: `tests/tools/symbol_overview/test_cli.py`

**Interfaces:**
- Produces: `build_parser()` has `--host` (default `"127.0.0.1"`, `args.host: str`); `main(argv)` passes `args.host` to `make_server(Overviewer(fetch), host, port)`; an `OSError` from `make_server` is reported with `parser.error(...)` (exit 2).

- [ ] **Step 1: Create the branch and commit the documents**

```bash
git switch -c feat/overview-service
git add docs/superpowers/specs/2026-11-05-overview-service-design.md docs/superpowers/plans/2026-11-05-overview-service.md
git commit -m "docs: design and plan for the overview as a third Compose service"
```

- [ ] **Step 2: Write the failing tests** in `tests/tools/symbol_overview/test_cli.py`:
  - Extend `test_defaults` to `(args.api_url, args.port, args.host) == ("http://localhost:8000", 8765, "127.0.0.1")`.
  - `test_host_is_passed_to_the_server(monkeypatch)`: monkeypatch `tools.symbol_overview.cli.make_server` with a fake that records `(host, port)` and returns an object whose `serve_forever` raises `KeyboardInterrupt`, `server_close` is a no-op and `server_address == ("0.0.0.0", 0)`; `main(["--host", "0.0.0.0", "--port", "0"]) == 0` and the recorded host is `"0.0.0.0"`.
  - `test_unbindable_host_is_a_usage_error(capsys)`: no fake; the real `make_server` is called with `main(["--host", "192.0.2.1", "--port", "0"])` (a documentation-range address is not local, so the bind raises `OSError`); expect `SystemExit` code `2` **and** `"cannot listen on"` in `capsys.readouterr().err` (without the stderr assertion the test would pass before the implementation, because argparse also exits 2 for the unknown option).
  - `test_empty_host_is_a_usage_error()`: `main(["--host", ""])` raises `SystemExit` code `2` (an empty string would silently bind every interface).

- [ ] **Step 3: Run them and see them fail**

Run: `uv run pytest tests/tools/symbol_overview/test_cli.py -v`
Expected: all new/changed tests FAIL (`unrecognized arguments: --host`, `AttributeError: host`, missing `cannot listen on`).

- [ ] **Step 4: Implement** in `cli.py`: remove the `HOST` constant; add `parser.add_argument("--host", default="127.0.0.1", help="address of this page (default %(default)s; the container passes 0.0.0.0)")`; after parsing, `parser.error("--host must not be empty")` for `""`; in `main`, wrap `make_server(...)` in `try/except OSError as error: parser.error(f"cannot listen on {args.host}:{args.port}: {error}")`; use `args.host` in the start-up line; change the `--port` help to "port of this page on --host" and the parser description so it no longer says the page is only local. Update the `server.py` module docstring so it says the command line defaults to `127.0.0.1` instead of "fixes" it.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/tools/symbol_overview -v`
Expected: all PASS (including `test_boundary.py`).

- [ ] **Step 6: Run the whole default suite**

Run: `make check`
Expected: lint, typecheck, imports and tests PASS.

- [ ] **Step 7: Commit**

```bash
git add tools/symbol_overview/cli.py tools/symbol_overview/server.py tests/tools/symbol_overview/test_cli.py
git commit -m "feat: --host option of the symbol overview, loopback stays the default"
```

---

### Task 2: Image, Compose service, build and CI wiring, deployment tests

**Files:**
- Modify: `Dockerfile` (new stage `overview`, header comment)
- Modify: `compose.yaml` (service `overview`, header tunnel line)
- Modify: `compose.build.yaml` (third entry)
- Modify: `Makefile` (`images` target and its comment)
- Modify: `.github/workflows/images.yml` (push loop, header comment)
- Modify: `tests/deploy/docker_cli.py` (new `OVERVIEW_IMAGE`, `OVERVIEW_ADDRESS`, `OVERVIEW_PORT`; `IMAGES` stays the two data images; module docstring)
- Modify: `tests/deploy/test_compose_file.py`
- Modify: `tests/deploy/test_isolation.py`
- Modify: `tests/config/test_compose_pin.py` (runs in `make check`: `SERVICES` gains `"overview"`; rename `test_compose_pins_exactly_the_two_images_at_the_project_version` to say three)
- Modify: `tests/deploy/test_workflow.py` (`test_dockerfile_pins_base_images_by_digest`: the count of external references goes from 3 to 4; keep the per-base digest assertion; the push-loop assertions, if any, learn `overview`)
- Modify: `tests/deploy/test_images.py` (docstring only: it covers the two data images, `IMAGES` is unchanged, so its parametrized tests do not see the overview; the overview's own checks are in `test_overview_image.py`)
- Create: `tests/deploy/test_overview_image.py`

**Interfaces:**
- Consumes: `--host` from Task 1.
- Produces: `OVERVIEW_IMAGE = "quasar-market-data-overview:local"`, `OVERVIEW_ADDRESS = "172.31.250.11"`, `OVERVIEW_PORT = 8765` in `tests/deploy/docker_cli.py`; `make images` builds `quasar-market-data-overview:local`.

- [ ] **Step 0: Add the constants** `OVERVIEW_IMAGE`, `OVERVIEW_ADDRESS`, `OVERVIEW_PORT` to `tests/deploy/docker_cli.py` first, so that the new tests fail for the right reason (a missing service, not an `ImportError`).

- [ ] **Step 1: Write the failing Compose-file tests** in `tests/deploy/test_compose_file.py` (`SERVICES` stays `("api", "collector")` for the data services; add `ALL_SERVICES = (*SERVICES, "overview")` and `MEMORY_BUDGET = 1088 * 1024**2` with a comment naming 384 + 576 + 128 MiB and the spec, section 6):
  - `test_overview_is_on_the_internal_network_only(prod)`: `set(networks) == {"internal"}`, `ipv4_address == OVERVIEW_ADDRESS`, no `ports`, no `volumes`, no `depends_on`, no `network_mode`.
  - `test_overview_runs_under_an_init_process`: `init is True`.
  - `test_overview_healthcheck_is_a_local_page_request`: `healthcheck["test"][:3] == ["CMD", "python", "-c"]` and the script string contains `127.0.0.1:8765`.
  - Switch these existing tests from `SERVICES` to `ALL_SERVICES`: `test_restart_limits_and_log_rotation` (replace `assert total <= GIB` by `assert total <= MEMORY_BUDGET`), `test_memory_limit_is_a_hard_cap_without_extra_swap`, `test_entrypoints_are_exec_form`, `test_production_images_pin_the_project_version`. In `test_build_override_changes_only_the_image` the overview's expected image is `OVERVIEW_IMAGE` and its target `overview` (the data services keep using `IMAGES`); strip `image` and `build` from all three before comparing. Leave `test_both_services_mount_the_volume_rw_and_the_config_ro` and the Collector-specific tests on `SERVICES`. Also change `tests/config/test_compose_pin.py` and `tests/deploy/test_workflow.py` as listed in Files.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -m compose tests/deploy/test_compose_file.py -v -o addopts="--strict-markers"` and `uv run pytest tests/config/test_compose_pin.py tests/deploy/test_workflow.py -v`
Expected: FAIL (`KeyError: 'overview'`; the pin and digest-count tests fail). The compose-marked file needs the Docker CLI; if it is not installed, record that these could not run and continue.

- [ ] **Step 3: Implement the service in `compose.yaml`**: add `overview` after `api` with exactly the values in Global Constraints; `command` and `entrypoint` unset (the image's exec-form values apply); `healthcheck.test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=3)"]`, `interval: 60s`, `timeout: 5s`, `retries: 3`; a comment above `mem_limit` saying 128 MiB is twice the largest measured process memory (44 MiB at 600 symbols, README "Overview refresh benchmark") rounded up to 64 MiB and not yet measured in the image. Add to the header: `#   ssh -L 8765:172.31.250.11:8765 <vps>     then open http://127.0.0.1:8765/` (the page, read-only), and change its first line "Two services share one SQLite WAL volume" to name the three services (only the Collector and the API share the volume).

- [ ] **Step 4: Implement the Dockerfile stage** `overview`: `FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS overview` (same digest as the other stages), the same `groupadd`/`useradd` for 10001 without `mkdir` of config/data, `WORKDIR /app`, copy only `tools/__init__.py` and `tools/symbol_overview/` to `/app/tools/` (owned by root, readable), `ENV PYTHONDONTWRITEBYTECODE=1`, `USER 10001:10001`, `ENTRYPOINT ["python", "-m", "tools.symbol_overview"]`, `CMD ["--host", "0.0.0.0", "--port", "8765", "--api-url", "http://172.31.250.10:8000"]`. Update the header comment: three targets. Place the stage between `builder` and `runtime`, so that the last stage (`api`, the default target of a bare `docker build .`) and the layers of `collector` and `api` are unchanged.

- [ ] **Step 5: Wire build and CI**: `compose.build.yaml` third entry (`image: quasar-market-data-overview:local`, `build.context: .`, `target: overview`, `platforms: [linux/amd64]`); `Makefile` `images` adds `docker build --platform linux/amd64 --target overview -t quasar-market-data-overview:local .` and its comment says three images; `images.yml` loop `for name in collector api overview` and its header comment ("build both images" → three); the tag-and-push loop stops at the first failure, so the README release check (Task 3) inspects all three images.

- [ ] **Step 6: Run the Compose-file tests**

Run: `uv run pytest -m compose tests/deploy/test_compose_file.py -v -o addopts="--strict-markers"`
Expected: PASS.

- [ ] **Step 7: Write the failing image tests** in new `tests/deploy/test_overview_image.py` (`pytestmark = pytest.mark.compose`; fixture builds nothing, it needs `make images` first, like the isolation test):
  - fixture `running_overview`: `docker run -d --rm --init -p 127.0.0.1::8765 OVERVIEW_IMAGE --host 0.0.0.0 --api-url http://127.0.0.1:1`; read the host port with `docker port <id> 8765/tcp` (the text after the last `:` of the first line); wait up to 30 s for `GET /`; always `docker stop` in `finally`. A plain `urllib.request.urlopen` raises `HTTPError` on 502: use a helper that catches `urllib.error.HTTPError` and returns `(status, body)` from it.
  - `test_the_page_is_served`: `GET /` is `200` with `text/html`.
  - `test_unreachable_api_is_a_page_level_error`: `GET /overview.json` is `502` and the body equals `{"api_error": {"code": "transport", "detail": "connection"}}`.
  - `test_runs_as_the_unprivileged_user`: `docker exec <id> id -u` prints `10001`.
  - `test_image_is_linux_amd64`: `docker image inspect --format '{{.Os}}/{{.Architecture}}'` is `linux/amd64` (the check `test_images.py` makes for the data images).

- [ ] **Step 8: Write the failing isolation tests** in `tests/deploy/test_isolation.py`: change the fixture to `compose("up", "-d", "--no-build", "api", "overview")` and add a wait for `GET http://{OVERVIEW_ADDRESS}:{OVERVIEW_PORT}/` to answer; add
  - `test_host_reaches_the_overview_and_it_sees_the_seeded_symbol`: `GET /overview.json` through the overview's address is `200` and the parsed body lists `BTC/USDT:USDT` in its symbols (read the real JSON field names in `tests/tools/symbol_overview/test_against_real_api.py`; the assertion is structural, not a text search). The overview reached the API over the internal network by IP, no DNS. Request `/overview.json` only after the seed has finished and **do not poll it** in the wait loop (the overview keeps a snapshot, errors included, for 15 s; wait on `GET /` instead). If it answers `502`, investigate the cause; do not loosen the assertion.
  - `test_running_overview_has_exactly_the_internal_network`: same assertions as the api version, address `OVERVIEW_ADDRESS`.
  - `test_overview_has_no_route_and_no_egress`: same assertions as `test_api_has_no_route_and_no_egress`, exec in service `overview`.
  - `test_overview_stops_on_sigterm`: placed before `test_api_stops_on_sigterm`; `compose("stop", "-t", "10", "overview")` takes under 10 s.
  Keep `test_api_stops_on_sigterm` last.

- [ ] **Step 9: Run the Docker suite**

Run: `make compose-test`
Expected: all PASS on a stock Linux Docker Engine. If this machine's engine is OrbStack/Docker Desktop the isolation module fails by design ("needs a stock Linux Docker Engine"): then run `make images` and `uv run pytest -m compose tests/deploy -v -o addopts="--strict-markers" --deselect tests/deploy/test_isolation.py` (everything in `tests/deploy` except the isolation module, including `test_images.py` for the unchanged data images) and report plainly that the isolation tests did not run here and will run in CI.

- [ ] **Step 10: `make check`**

Run: `make check`
Expected: lint, typecheck, imports and the default tests PASS (this includes `tests/config/test_compose_pin.py` and `tests/deploy/test_workflow.py`).

- [ ] **Step 11: Commit**

```bash
git add Dockerfile compose.yaml compose.build.yaml Makefile .github/workflows/images.yml tests/deploy tests/config/test_compose_pin.py
git commit -m "feat: the symbol overview as a third image and Compose service"
```

---

### Task 3: Documentation

**Files:**
- Modify: `docs/architecture.md` (section 9, first bullet; section 3 table row "Deployment")
- Modify: `README.md` ("Install and run" Docker paragraph and "On the VPS" block; `make images` row; "Deployment (S21)" first paragraph ("Two images ... two services"), Network, Release rule (both occurrences of "both"/"two": "both `images.yml` jobs", "for both images", and a sentence that the overview image exists in GHCR only after the next version bump and tag, see Global Constraints), Upgrade, Health ("Both services restart unless stopped"); "Symbol overview (tool)" sentences "binds `127.0.0.1` only" and "The tool is not part of the images or of `compose.yaml`"; a sentence that with `compose.local.yaml` the overview starts too but is not reachable from the host on OrbStack/Docker Desktop and costs 128 MiB: use `make overview` locally)
- Modify: `docs/invariants.md` (INV-069 "Not covered: ... today only the Collector, which listens on nothing" is no longer true: the overview is a second container on `internal` and it listens; update it or state why the overview is not a relay; plus one new entry, if the register's format allows)

**Interfaces:**
- Consumes: the final values of Task 2 (address, port, tunnel command, memory cap).

- [ ] **Step 1: `docs/architecture.md`**: section 9 bullet 1 states that the data core is the two services `collector` and `api`, and that the symbol overview is a third service with its own image, a read-only consumer of the API over HTTP with no volume and no route out; the line "Collector can run without API; restarting/deploying API must not restart Collector" gets "or the overview" for the overview side (restarting the overview restarts neither). Section 3 "Deployment" row: "separate Collector, API and overview services".
- [ ] **Step 2: `README.md`**: all places named above; add the tunnel `ssh -L 8765:172.31.250.11:8765 <vps>` and `http://127.0.0.1:8765/`; Upgrade says "bump `project.version` and all three image tags"; the pre-`pull` release check lists three images; the page's binding sentence says "binds `127.0.0.1` by default (`--host`; the container passes `0.0.0.0`)"; the `make images` row says three images; mention that the memory budget of the services together is 1088 MiB.
- [ ] **Step 3: `docs/invariants.md`**: read the register's format and the INV-069 entry; add the next free number "the overview service has no volume and no route out" with enforcement pointing to the real test names from Task 2 (`test_overview_is_on_the_internal_network_only`, `test_overview_has_no_route_and_no_egress`), status PLANNED like INV-069 until a green `images.yml` run is observed (these tests run in `make compose-test`, never in `make check`). If the register has no natural place, do not add an entry and say so in the report.
- [ ] **Step 4: Check nothing contradicts**: search the repo (docs, README, comments in `Dockerfile`, `compose*.yaml`, `Makefile`, `.github/workflows/images.yml`, docstrings in `tests/deploy`) for "not part of the images", "two images", "Two images", "both images", "both services", "Two services", "two services", "today only the Collector", "960", "1 GiB", "binds `127.0.0.1` only" and fix each hit that is now wrong; do not edit historic design documents under `docs/superpowers/specs/` and `docs/superpowers/plans/` other than the two for this project.
- [ ] **Step 5: Run `make check`**

Run: `make check`
Expected: lint, typecheck, imports and tests PASS.

- [ ] **Step 6: Commit**

```bash
git add docs README.md
git commit -m "docs: the overview as a third service (architecture section 9, README, invariants)"
```

---

### Task 4: Reviews and PR

- [ ] **Step 1:** Independent review of the plan is done before Task 1 starts (two fresh reviewers; apply all findings to this plan).
- [ ] **Step 2:** After Task 3, two independent reviews of the implementation (the branch diff against `main`); apply all findings, re-run `make check` and, where possible, `make compose-test`.
- [ ] **Step 3:** Push the branch and open one PR; title and body written as the squash commit message: what changed, what was verified (list exactly which commands ran and which did not), remaining risks: (1) `compose.yaml` on `main` pins `overview:v<project.version>`, which exists in GHCR only after the next version bump and tag (the git tag `v0.1.0` already exists if `git tag` confirms it), so the VPS keeps using `git show v<released tag>:compose.yaml` until then; (2) the 128 MiB limit is not measured in the image (report a `docker stats` reading if the image run allowed one); (3) the compose isolation tests run in CI only if they did not run locally; (4) after the first push to `main`, check that the new GHCR package `quasar-market-data-overview` has the same visibility as the other two and that the workflow token can push it. Do not tag or bump the version.

## Self-review notes

- Spec coverage: 5.1 → Task 2 steps 3-4; 5.2 → Task 1; 5.3 (incl. `init`) → Task 2 steps 1, 3, 8; 5.4 → Task 2 steps 5, 7, 8; 5.5 → Global Constraints; 5.6 and 5.7 → Task 3 and Task 2 step 3 (no `profiles`/`depends_on`, asserted); section 6 → Task 2 step 1 (`MEMORY_BUDGET`); section 7 risks → Task 4 step 3.
- Names used across tasks: `OVERVIEW_ADDRESS`, `OVERVIEW_PORT`, `IMAGES["overview"]`, `ALL_SERVICES`, `MEMORY_BUDGET` are defined in Task 2 and used only there and in Task 3 prose.
