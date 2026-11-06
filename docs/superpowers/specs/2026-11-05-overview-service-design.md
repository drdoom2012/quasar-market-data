# Symbol overview as a third Compose service — design

- **Date:** 2026-11-05.
- **Status:** APPROVED by the user on 2026-11-05 including memory option B (section 6; answer "B"); the other **my call** items stay my defaults. The direction (third service, not part of the API image) and the plan in chat were approved by the user on 2026-11-05 ("podle doporučení", "ano"), including the change of `docs/architecture.md` section 9 and the `--host` option. Everything marked **my call** is a default the user can overturn.
- **Size:** a small project: one Docker target, one Compose service, one command-line option, CI and test updates, documentation. No change to the Collector, the API, the database, or any contract.

## 1. Intent

The symbol overview page (`tools/symbol_overview/`, design `2026-11-01-symbol-overview-design.md`) is today a local tool: the user runs `make overview` or `python -m tools.symbol_overview` on their machine, against a local API or against the VPS API through an SSH tunnel. The user wants the page to be **part of the Docker deliverable**: built into an image, pushed to GHCR with the other images, and run on the VPS by `docker compose up -d`, so the monitoring page is there whenever the deployment is.

## 2. Goals, non-goals, anti-goals

**Goals**
- A third image target `overview` in the `Dockerfile`, built and pushed with `collector` and `api`.
- A third service `overview` in `compose.yaml`, reachable from the user's machine the same way as the API: an SSH tunnel to a fixed address on the `internal` network.
- The page keeps working exactly as today: same code, same behaviour, same API calls.

**Non-goals**
- No authentication, no TLS, no published port, no public exposure. (The original design lists authentication as a non-goal; the API has none either.)
- No change to what the page shows or to the API endpoints it calls.
- No new dependency: the tool stays standard library only (`tests/tools/symbol_overview/test_boundary.py`).
- No change to the images `collector` and `api` and to their contents.

**Anti-goals (a failure even if it technically works)**
- The overview gains access to the SQLite volume, to the exchange, or to any route out. It reads the API over HTTP only.
- Restarting, deploying or crashing the overview affects the Collector or the API (INV: Collector and API stay operationally independent; the overview is a consumer of the API).
- `make overview` on a developer machine stops binding to loopback only. The default of the new option stays `127.0.0.1`.
- A silent change of the recorded 1 GiB memory budget of the deployment (section 6).

## 3. Constraints taken from the repository

- The tool lives in `tools/`, outside the `quasar_market_data` wheel; the Dockerfile copies only `src/`. `tools/` is not excluded by `.dockerignore`.
- `tools/symbol_overview/cli.py` fixes `HOST = "127.0.0.1"` ("the tool is for the person at this machine; it is not a service"); the README says "The tool is not part of the images or of `compose.yaml`". Both statements are changed by this project (section 5.2, 5.6).
- `compose.yaml` runs an `internal: true` network `172.31.250.0/24`; a published port does not work on it, so the host reaches a service by its fixed address (the API is `172.31.250.10`).
- `tests/deploy/test_compose_file.py` (marker `compose`, needs Docker) asserts the shape of the Compose project for `SERVICES = ("api", "collector")`, including: total `mem_limit` of the services at most 1 GiB (today 384 MiB + 576 MiB = 960 MiB), restart policy, log rotation, `memswap_limit == mem_limit`, exec-form entrypoints, production images pin `v<project.version>`, and `test_build_override_changes_only_the_image`.
- Releases: `images.yml` pushes images only for `main` and `v*` tags; the tag `v<version>` in `compose.yaml` exists in GHCR only after a tag push. The user's rule: tag and bump the version only when the user says to deploy. Open interest and funding rate are deployed together; this project does not change that.

## 4. What the user decided

- Frontend = the existing symbol overview page. It must be in the Docker image.
- A separate third service, not part of the API image (my recommendation, accepted).

## 5. Design

### 5.1 Dockerfile target `overview`

- A new stage `overview` based on the same pinned `python:3.12-slim-bookworm` digest, **not** on `runtime` (it needs no virtual environment: the tool is standard library only).
- Same non-root user `10001:10001` and `WORKDIR /app`.
- Copies `tools/` (the package `tools.symbol_overview` with `page.html`, and `tools/__init__.py` if it exists) to `/app/tools/`. Other files in `tools/` (for example `bench_open_interest_write.py`) are not needed; the builder decides whether to copy the whole directory or only `tools/symbol_overview` plus `__init__.py` (my call: only what the page needs).
- `ENTRYPOINT ["python", "-m", "tools.symbol_overview"]`, `CMD ["--host", "0.0.0.0", "--port", "8765", "--api-url", "http://172.31.250.10:8000"]` (exec form).
- `PYTHONDONTWRITEBYTECODE=1` or pre-compiled bytecode: the user cannot write into `/app` at run time, so Python would otherwise compile on every start (builder's call; either is acceptable).

### 5.2 Option `--host` in the tool

- `tools/symbol_overview/cli.py` gets `--host` (default `127.0.0.1`). `make overview`, `mprocs.yaml` and the README commands are unchanged and still bind loopback.
- The comment "it is not a service" and the module docstring of `server.py` ("the command line fixes `127.0.0.1`") are updated to say what is true: the default is loopback; the container passes `0.0.0.0`.
- Tests: the default host, a custom host reaches `make_server`, and an invalid host is a parser error (not a traceback).

### 5.3 Compose service `overview`

- Image `ghcr.io/drdoom2012/quasar-market-data-overview:v<project.version>` (same pin rule as the other two; bumped together at release).
- Network: `internal` only, fixed address **`172.31.250.11`** (my call). No `egress` network, no `ports`.
- Volumes: none. The overview does not mount the SQLite volume or the configuration file.
- `restart: unless-stopped`, `logging: *logging`, `mem_limit: 128m` equal to `memswap_limit` (section 6).
- `init: true` (**my call**): Python as PID 1 ignores SIGTERM unless it installs a handler, and the tool installs none (`cli.py` catches only `KeyboardInterrupt`), so without `docker-init` every `docker stop` would wait 10 s and end in SIGKILL. The same reason the Collector has `init: true`.
- Healthcheck: **my call** — a Python one-liner in exec form that does a `GET /` against `127.0.0.1:8765` inside the container (the image has no `curl`). A failing page only marks the container unhealthy; it never restarts the Collector or the API.
- `depends_on` is **not** used: the page already reports "API not reachable" as a page-level error, and a dependency would couple the start of two services that must stay independent.
- Access from the user's machine: `ssh -L 8765:172.31.250.11:8765 <vps>`, then `http://127.0.0.1:8765/`. Documented in the header of `compose.yaml` and in the README.

### 5.4 Build override, make, CI

- `compose.build.yaml`: a third entry with `image: quasar-market-data-overview:local` and `build.target: overview`.
- `Makefile` `images`: a third `docker build --target overview`.
- `images.yml`: the push loop covers `collector api overview`.
- `tests/deploy/docker_cli.py` (`IMAGES`) and `tests/deploy/test_compose_file.py` (`SERVICES`) learn about the third service. Tests that are specific to the Collector or the API (init, stop grace, healthcheck of the Collector, `/app/data` mounts, `egress`) stay specific to them; new tests for the overview assert section 5.3 (no egress, no ports, no volumes, fixed address on `internal`, entrypoint exec form, healthcheck).
- Isolation test (`tests/deploy`, INV-069): extended so that the overview is proven to have no route out, if the existing test can be extended without changing what it proves; otherwise a separate test (builder's call).
- A test that the image runs: the `overview` image starts, answers `GET /` with `200` and `GET /overview.json` with the page-level error JSON when the API is unreachable (a failed refresh is a visible error, never green). **My call:** a standalone `docker run` with `--api-url http://127.0.0.1:1` (connection refused at once, no wait for the 60 s deadline), run in the `compose`-marked suite; the Compose-project tests stay about the Compose file.

### 5.5 What does not change

Collector and API images and code, `config/quasar.compose.toml`, the database schema, the contracts (`docs/contracts/`), the import-linter contracts, `tests/tools/symbol_overview/test_boundary.py` (still standard library only).

### 5.7 Always on, one release unit (my call)

The overview is a normal service of `compose.yaml`: no `profiles:`, no `depends_on`, and it is released with the other two images (same `project.version`, same tag, the push loop of `images.yml` pushes all three or the job fails). `docker compose pull && docker compose up -d` therefore pulls three images. If the third image were missing from GHCR, `pull` would stop the whole deploy; the existing release check ("both `images.yml` jobs green for the tag, `imagetools inspect` lists `linux/amd64`") is extended to the third image, and that is the protection. The alternative (a `profiles:` entry so that the data core deploys without the page) keeps Collector and API deploys independent of the page's image, at the cost of a second command and a deployment that can silently run without the page; not chosen, because the user asked for the page to be part of the deliverable.

### 5.6 Documentation

- `docs/architecture.md` section 9: "two independent Docker services/images" becomes: the data core is two services (`collector`, `api`); the **symbol overview** is a third, read-only consumer of the API with its own image (the word "optional" is not used: see section 5.7). Section 2 and 4 stay as they are (the overview is not a market-data component).
- `README.md`: "Deployment (S21)" (three images, the tunnel to `172.31.250.11:8765`), "Symbol overview (tool)" (the sentences "binds `127.0.0.1` only" and "The tool is not part of the images or of `compose.yaml`"), the `make images` row, the upgrade rule ("bump all three image tags").
- `compose.yaml` header: the second tunnel.
- `docs/invariants.md`: add an entry only if the existing register has a natural place for "the overview has no volume and no route out"; otherwise the compose tests are the enforcement (builder's call, report it).

## 6. Memory budget (DECIDED by the user: B)

The Compose test caps the **total** of all services at 1 GiB, which is the budget of the 2 GB VPS (960 MiB used). The repository's convention is "about twice the measured peak, rounded up to 64 MiB". The overview's memory was measured at **28 to 44 MiB** (README, "Overview refresh benchmark" / design section 14, process memory at 300 to 600 symbols, loopback), which gives **64 MiB** at 28 to 32 MiB and **128 MiB** at 44 MiB.

- **A. `mem_limit: 64m`, cap stays 1 GiB** (960 + 64 = 1024 MiB, the test still passes with nothing changed). At 600 symbols the peak (44 MiB) plus interpreter and page cache is close to the limit: an OOM kill of the overview is possible, and only the overview dies (restart policy brings it back).
- **B. `mem_limit: 128m`, cap raised to 1088 MiB (= 1.0625 GiB)** (960 + 128 = 1088 MiB). *(Corrected on 2026-11-05: the first draft said "1.125 GiB", which is 1152 MiB and not the sum.)* Follows the convention; the cap is a recorded number and changing it is a visible, reviewed edit. 128 MiB of the VPS's 2 GB goes to a monitoring page.

**My call: B, with the cap raised to exactly the new sum** (not "weakened to make the test pass": the test keeps its meaning, "all services together fit the budget", and the budget is stated in one place). The user can choose A; then the target is 64 MiB with a re-measurement on the Dockerfile's image before release.

## 7. Risks and consequences

- **Release coupling.** `compose.yaml` pins `v<project.version>` for all images. A reviewer reports that the git tag `v0.1.0` already exists (to be verified with `git tag`); then `overview:v0.1.0` can never be produced, and the image exists in GHCR only after the next version bump and tag (only the user triggers it). The image `overview:v<version>` does not exist in GHCR until that tag is pushed and `images.yml` is green. Until then `docker compose pull` on the VPS fails for the new service, and `compose.yaml` on `main` must not be copied to the VPS. This is already how every release works ("copy from the tagged commit"); it is not changed by this project. This project does **not** bump the version or push a tag (user rule).
- **The page is unauthenticated.** Anyone who can reach `172.31.250.11:8765` sees symbol names and data completeness. On the VPS that is the host itself and SSH-tunnel users, the same set that can reach the API. The page shows no prices, rates or values.
- **`0.0.0.0` inside the container.** Safe only because the network is `internal` and nothing is published. `compose.local.yaml` (local use) must not publish the overview port without a decision; this project does not touch that file (my call).
- **The tool trusts `--api-url`.** It is fixed in the image's `CMD` to the API's address on `internal`; an operator can override it in `docker compose run`, which is outside the deployed project.
- **Unmeasured in the image.** Memory and the 60 s refresh over the real `internal` network have not been measured; the refresh was measured on loopback only.

## 8. Verification

- `make check` (ruff, mypy, import-linter, pytest) with the new option's tests.
- `make compose-test` (needs a stock Linux Docker Engine; CI does it): the three images build, the Compose file tests pass, the isolation test passes, the overview image starts and answers.
- Locally on macOS the compose tests may not run (they need a stock Linux engine). In that case I report what ran and what did not; I do not claim a pass for a check that did not run.

## 9. Left to the builder

The exact Dockerfile wording (copy list, bytecode), the healthcheck command, test names and fixtures, whether the isolation test is extended or complemented, the wording of the documentation changes, and the entry in `docs/invariants.md`.
