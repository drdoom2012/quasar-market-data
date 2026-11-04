# S21 — Docker images, Compose deployment and an isolation test — Design

- **Status:** Revision 2 final, for approval (2026-10-29); the final pass extended H1 (5.5), bound the integration test
  to the production Compose project (5.6) and removed any automatic fallback. Revision 2 is a targeted review: the network architecture
  (5.5, with a run experiment, Appendix A), the isolation test (5.6), the Collector healthcheck (5.4) and the links to
  INV-013, INV-014, INV-044 and INV-063 (5.8). Nothing else changed in substance; 5.3 changed only where the experiment
  forced it. **HARD STOP H1 (5.5) must be cleared before implementation.**
- **Inputs:** spec `2026-10-20-market-data-v1-design.md` (S21, D6, section 7 point 4), `docs/architecture.md` (deployment),
  `docs/invariants.md` (INV-013, INV-014, INV-044, INV-063), `docs/follow-ups.md` (F-16), the S20 design, `config/quasar.toml`,
  `.github/workflows/check.yml`.
- **Marking:** "the user" marks what the user decided in the question round; "designer's call" marks a choice made without
  the user, which can be overturned. "Observed" marks a result of the experiment in Appendix A, with its platform.

## 1. Intent

Today both processes run from a developer checkout (`uv run quasar-collector run`, `uv run quasar-api serve`). S21 makes
them deployable on a small VPS (2 GB RAM) as two Docker services that share one SQLite WAL volume, and shows, against a
really running Compose project, that the API service has no network route out. That is the deployment-level evidence
INV-013 has been waiting for; until now only the code-level INV-044 was enforced. Network isolation and the
application invariant are different claims (5.6.3, 5.8).

## 2. Goals

- A Dockerfile (or one multi-target Dockerfile) producing a `collector` image and an `api` image, both `linux/amd64`
  (designer's call: the VPS architecture is assumed, the user checks it with `uname -m`). TA-Lib's native C library is
  handled in the build (spec D6).
- A Compose project with two services, `collector` and `api`, and one shared volume for the database directory (the user:
  "two services, shared local volume"). The volume stays writable for both services, because a WAL reader needs the
  shared-memory file (spec section 7 point 4); read-only stays at the connection level (INV-014, unchanged).
- The Collector service restarts on failure and has a simple process healthcheck (the user: "process-based healthcheck is
  enough"); its limited value is stated in 5.4.
- The API service has no route to the internet and is reachable only from the VPS itself; the user reaches it over an SSH
  tunnel (the user: "only localhost, access over an SSH tunnel"). The mechanism is decided in 5.5.
- An automated test against a running Compose project shows that the API container has no TCP, UDP or DNS egress, is
  attached only to the intended network, and still serves data from the shared volume to the host (the user: "verify it
  with a test against a running Compose").
- GitHub Actions builds both images and pushes them to GHCR; the deployment on the VPS stays manual,
  `docker compose pull && docker compose up -d` (the user: build and push to GHCR, deploy by hand).
- The invariant register states exactly what the deployment proves (5.8); F-16 stays open.

## 3. Non-goals

- Automatic deployment, SSH from CI, Watchtower or any other updater (the user).
- A reverse proxy, TLS, authentication, public exposure of the API (the user: localhost plus SSH tunnel).
- A heartbeat, a status table, a Collector HTTP endpoint or any schema change (F-16 stays open).
- A third container of any kind, including a forwarding sidecar (the user: two containers).
- Monitoring, metrics scraping, log shipping.
- A second venue (S22), the soak test (S23), any change of `domain`, `storage`, `collector` or `api` behaviour.
- Kubernetes, Swarm, Compose profiles for several environments.

## 4. Anti-goals (failure even if it works)

- The API service gaining a normal network "just to make the port work" and the isolation test passing only because it
  never tried a real outbound connection.
- An isolation test that passes when the API is unreachable for any reason (a dead container, a wrong port): every
  negative result must sit next to a positive control that shows the same probe can succeed.
- Calling network isolation a proof that API handlers never call an exchange (5.6.3).
- A second copy of the universe or of the rule that can drift from `config/quasar.toml` without a test failing.
- The volume mounted read-only (it breaks WAL readers) or the API given any way to write the database.
- Secrets, tokens or exchange credentials in an image, a Compose file or CI (the project has none and must not get any).
- A healthcheck presented as evidence that the Collector ingests.
- The push job running on pull requests from forks, or the ordinary `check` workflow gaining write permissions.

## 5. Design

### 5.1 Images

- Multi-stage build on a slim Python 3.12 base (the version of `.python-version`); dependencies are installed from
  `uv.lock` with the locked flag, so an out-of-date lock fails the build as it fails `check`.
- The `ta-lib` 0.8.2 wheel bundles the C library on macOS arm64; whether a Linux amd64 wheel exists is **not verified**
  and is the first thing the build must find out. If there is no wheel, the build stage compiles the C library and the
  runtime stage copies only the result, so the compiler stays out of the final image.
- Runs as a non-root user. Both images use the same numeric UID and GID, so both processes can open the same WAL files
  (designer's call).
- One Dockerfile with two targets (designer's call); the builder may split it if the size difference matters on 2 GB.
- No `.env` file and no environment variable changes behaviour (INV-063).

### 5.2 Configuration in containers

INV-063 says the committed file is the only source of operational values. The committed `config/quasar.toml` binds the
API to `127.0.0.1`, which nothing outside the container can reach, and holds `database.path = "../data/quasar.db"`.

- Designer's call: add **`config/quasar.compose.toml`**, identical to `config/quasar.toml` except `[api] host =
  "0.0.0.0"`. Inside the container the only non-loopback interface is the internal network (5.5), so this binds the API
  to that network and nothing else. Relative paths resolve against the file, so with the file mounted at `/app/config/`
  and the volume at `/app/data/` the database path needs no change.
- A unit test fails if the two files differ in anything but `[api].host`. This is the anti-drift guard of section 4.
- Both services mount the same file, so the hash in the start line is the same for both.
- The alternative (changing the committed file to `0.0.0.0`) is rejected: a developer machine would listen on the LAN.

### 5.3 Compose project

- Services `collector` and `api`, one volume for `/app/data`, the config file mounted read-only into both.
- Two networks: `internal` (`internal: true`, a fixed subnet) and a normal network for egress.
- `collector`: on both networks (the only service allowed to reach the internet), `restart: unless-stopped`, a memory
  limit, log rotation (a 2 GB VPS and months of uptime), the healthcheck of 5.4.
- `api`: on `internal` only, with a **fixed address** on that subnet and **no `ports:` entry** (5.5: a published port on
  an internal network is not published), `restart: unless-stopped`, a memory limit, the same log rotation; depends on
  nothing (it already answers `503 not_ready` until the Collector creates the database).
- Memory limits: the API peaked at 137 MB RSS in `make bench-indicators` at 8 concurrent worst-case requests; the
  Collector was not measured. The builder sets the limits from a measurement and documents them as measured (designer's
  call; starting point 512 MB each, at most 1 GB together so the host keeps room).
- The VPS file refers to `image: ghcr.io/...:<tag>`; a second file or override provides `build:` for local use
  (designer's call).
- The subnet and the fixed address are deployment settings of the Compose file, not operational values of a process;
  INV-063 is unaffected (5.8).

### 5.4 Collector healthcheck

The user asked for a simple process healthcheck, and S21 keeps it. Its value is small and the documentation says so:

- **What the restart policy already does.** The Collector runs as the container's main process. When it exits, the
  container stops and `restart: unless-stopped` starts it again. A check "is the process alive" can therefore almost never
  fail while the container runs: a dead main process means a stopped container, not an unhealthy one.
- **What Docker does with `unhealthy`.** Plain Docker Engine and Compose do not restart an unhealthy container; the status
  is only shown in `docker ps` and `docker inspect`. The healthcheck adds a visible `healthy` label, nothing more.
- **What it cannot see.** A process that runs but does nothing useful: a stuck event loop, rounds that all end in
  `gave_up`, an exchange outage, a full disk that makes every write fail, a wrong clock. All of these stay `healthy`.
- **Where ingestion health is seen instead.** `/health/ingest` (series go `due`, then `stale`) and the Collector's own
  `round` lines in the container log. Neither names the cause, and `/health/ingest` cannot tell a stopped Collector from
  an exchange outage.

The healthcheck is therefore documented as "the main process exists", never as "the Collector ingests". F-16 stays
open; its text gains one sentence that S21 added restart policy and a process healthcheck and that neither is a health
signal of ingestion.

### 5.5 Network architecture (resolved by experiment; HARD STOP H1 before implementation)

**Question.** Can the API sit on an `internal: true` network (no egress) and still be reachable from the host, with two
containers only and without a new start option?

**Experiment.** Appendix A gives the files and commands; it uses two stand-in containers plus a third only for
variant 3. It was run on **OrbStack 2026-10, Docker Engine 29.8.2 (Linux kernel 7.2.12, iptables backend, OrbStack's own
userland proxy `pstub`)**. Probes from the Docker host's network namespace ran in a container with `--network host`.

**Observed results** (OrbStack only):

| # | Variant | Egress from the API | Reachable from the host | Result |
|---|---------|---------------------|-------------------------|--------|
| 1 | API on `internal` only, `ports: 127.0.0.1:18001:8000` | blocked: no default route; TCP and UDP `ENETUNREACH`; DNS fails | **no**: Docker did not publish the port (`docker port` empty, `Ports: {"8000/tcp": null}`), host gets `ECONNREFUSED`, with no warning from Compose | **not feasible** |
| 1b | API on `internal` only, fixed address, no `ports:`; the host connects to the container address | blocked (same as 1) | **yes**: the host's network namespace reached `172.31.250.10:8000` (`200 OK`, source address the bridge gateway) | **feasible on OrbStack** |
| 2 | API on a normal network, egress blocked by a host firewall rule (`DOCKER-USER`) | not run | — | **rejected without a run**: the guarantee would live in host configuration outside the Compose project, so a test "against a running Compose" would not prove it |
| 3 | API with `network_mode: none`, HTTP on a Unix socket in a volume | blocked: no routes at all | yes, through the socket from another container | **feasible, but needs a new API start option** (Unix socket) that conflicts with INV-063 and needs the user's approval |

Positive control: the Collector stand-in on both networks connected to `1.1.1.1:443` and `fapi.binance.com:443`,
received a UDP DNS reply and resolved `example.com`. So the API's failures are the isolation, not a broken probe or a
dead network.

**Why OrbStack does not settle it.** OrbStack is not stock Linux Docker Engine. It replaces the userland proxy, and it
routes container addresses in its own way: in the same run a container on *another* network, and macOS itself, also
reached `172.31.250.10:8000`, although the `DOCKER-INTERNAL` iptables chain (`! -s 172.31.250.0/24 -o br-… -j DROP`)
should drop traffic into the internal bridge from outside its subnet. On stock Linux the expected behaviour (**not
verified**) is: the host reaches the address, because its source is the bridge gateway inside the subnet, and other
containers and remote machines do not.

**Decision.** Variant 1b is the **candidate** (designer's call, the only variant that keeps two containers, no new
option and INV-063). It is not accepted until H1 passes. The SSH tunnel becomes `ssh -L 8000:<fixed api address>:8000
<vps>` instead of `ssh -L 8000:127.0.0.1:8000 <vps>`; the user's intent, "reachable only from the server, over SSH", is
unchanged. Variant 1 is dropped. Variants 2 and 3 are **not** fallbacks: nothing switches to them without the user's
decision.

**HARD STOP H1.** Before any implementation, rerun Appendix A (a preliminary experiment) on **stock Linux Docker
Engine**: the VPS itself, or a GitHub `ubuntu-latest` runner plus the VPS for the checks that need it, never OrbStack or
Docker Desktop. Record the engine version, the kernel, the firewall backend (iptables or nftables), whether the userland
proxy is enabled, and `net.ipv4.ip_forward`. Variant 1b is accepted only if every condition below holds. The conditions
separate three origins of access; a closed public port 8000 alone is **not** sufficient evidence, because nothing binds
port 8000 on the host in variant 1b anyway, so the real exposure is the routed path to the container address.

*A. From the host itself (must succeed):*

1. The host's network namespace reaches the API's fixed address (`200`).

*B. From other containers (must fail, except the one authorized peer):*

2. A container on the default bridge does not reach the fixed address.
3. A container on another user-defined network, including a network of another Compose project, does not reach it.
4. A container with `network_mode: none` does not reach it (trivially, as a sanity check of the probe).
5. The only container allowed to reach it is the `collector`, which shares `internal` by design; this is recorded as an
   authorized path, not as isolation.

*C. From other machines (must fail):*

6. A connection from another machine to the VPS's public address on port 8000 fails (necessary, not sufficient).
7. **Routed access to the container address fails.** From a machine that can route to the VPS (the same provider
   private network or L2 segment, if one exists), add a route for the internal subnet via the VPS and connect to the
   fixed address; it must fail. If no such machine exists, this check cannot be run and is recorded as **not run**, not as
   passed.
8. **The engine's rules show why 6 and 7 fail.** On the VPS, the installed rules (`iptables-save` or `nft list ruleset`)
   drop or reject traffic to the internal subnet that arrives on any interface other than the internal bridge, and
   `ss -ltnp` shows no listener on port 8000 on an external interface. If only the rule inspection is available (7 not
   run), the result is recorded as "supported by the rules, not tested end to end".
9. The same holds for IPv6: the internal network has no IPv6 subnet, or conditions 2 to 8 are repeated for it.

*D. Egress (must fail, with a positive control):*

10. The API has no default route, and its TCP, UDP and DNS probes fail while the same probes succeed from the
    `collector`.

**If any condition fails, or one that is required cannot be run, implementation does not start.** The builder does not
switch to variant 2, variant 3 or a modified 1b on their own. They stop and present to the user what failed, with the
evidence, and the options as they then stand (for example: variant 3 with its Unix-socket start option, variant 1b with a
documented wider exposure, a host firewall rule outside Compose, or no deployment-level isolation). The user decides.
This design does not claim any of these results for stock Linux.

### 5.6 The isolation test

A pytest module under `tests/deploy/`, marked `compose` and excluded from the default run like `live`, run by a new
`make` target and by a CI job on a stock Linux runner. It starts the Compose project from freshly built images (CI) or
from the local build (developer) and seeds the shared volume with a small valid database without calling the exchange;
the seed runs from the Collector image's own code, since the Collector is the only writer (designer's call).

**The test checks the real production Compose project, not a test copy.** It runs the same Compose file that is deployed
on the VPS. The only allowed difference is the image source (a local or CI build instead of a GHCR tag), supplied by
an override that may change `image`/`build` and nothing else; the test fails if the override touches networks, ports,
`network_mode`, addresses or volumes. It checks that project's resolved configuration, its running networks, the
container's routing, its port bindings and the isolation rules the engine actually installed for it (5.6.2). Appendix A
stays a preliminary experiment with stand-in containers; passing it does not replace this test.

#### 5.6.1 Positive checks (each negative result below depends on them)

- **Reachability from the host.** The test process on the host requests `GET /health`, `GET /v1/candles` and
  `/health/ingest` on the API's fixed address and asserts `200` with the seeded data. Without this, a dead API would
  pass every negative check.
- **The probe works.** The same egress probe run in the `collector` container succeeds for TCP, UDP DNS and name
  resolution. If the runner itself has no internet, the test fails with a clear message (not skipped), because the
  negative checks would then prove nothing.
- **The Compose file is the one under test.** The test reads the project's resolved configuration
  (`docker compose config`) and fails if the `api` service has a `ports:` entry or `network_mode`, or is attached to any
  network but `internal`.

#### 5.6.2 Network checks on the running `api` container

- **Attached networks.** `docker inspect` shows exactly one network, and `docker network inspect` shows it with
  `Internal: true`; `NetworkSettings.Ports` has no host binding.
- **Routing.** Inside the container: no default route in `/proc/net/route`, and no global IPv6 address or IPv6 default
  route (`/proc/net/if_inet6`, `/proc/net/ipv6_route`).
- **TCP.** Connections to a public IP (`1.1.1.1:443`) and to the exchange by name (`fapi.binance.com:443`) fail.
- **UDP.** A DNS query sent to `1.1.1.1:53` fails with "network unreachable" (observed on OrbStack). Silence after a
  timeout would not prove a drop, so the test asserts the immediate error, not just a missing reply.
- **DNS.** Resolving a public name (`example.com`, `fapi.binance.com`) fails. Docker's embedded resolver still answers
  names of containers on the same network; that is expected and not egress.
- **Unauthorized containers.** A throwaway container on the default bridge and one on another user-defined network
  cannot reach the API's fixed address, while the host can (5.6.1). This repeats conditions 2 and 3 of H1 against the
  production project on every CI run.
- **Installed isolation rules.** On the runner, the rules the engine installed for the project's `internal` network
  (`iptables-save` or `nft list ruleset`, with `sudo`) contain the drop rules for traffic leaving the internal bridge and
  for traffic entering it from outside its subnet. The test asserts their presence, recorded with the engine version and
  firewall backend; a different backend or rule shape fails the test with the dump attached instead of passing silently.
  Routed access from another machine (H1 condition 7) cannot be tested in CI and stays a one-time H1 check on the VPS.

#### 5.6.3 What these tests prove, and what they do not

- **They prove** that, in this Compose file on this engine, the `api` container had no route out and that the named TCP,
  UDP and DNS attempts failed at the time of the run. This is **network isolation of the deployment**.
- **They do not prove the application invariant** INV-013, "API request handlers never fetch exchange data". A handler
  that tried would fail in this deployment, but would still try, and in any run outside Compose (`uv run quasar-api
  serve` on a developer machine) nothing stops it. The code-level evidence for "never tries" remains INV-044, which is a
  name-based tripwire.
- **They do not cover:** another engine, version or firewall backend than the one tested; a changed Compose file; ICMP,
  raw sockets and protocols other than the probed ones (the missing route makes them unlikely but they are not probed);
  traffic relayed through another container on `internal` (today only the Collector, which listens on nothing); the host
  and its other processes; the Docker daemon's embedded resolver as a channel; the time after the test.

### 5.7 CI

- New workflow `.github/workflows/images.yml`, separate from `check.yml`, so `check.yml` keeps `contents: read` only.
- Pull requests: build both images for `linux/amd64` without pushing and run the Compose test. Pushes to `main` and
  version tags: also push to GHCR with `packages: write` on that job only. Pull requests from forks never get the write
  permission.
- Actions pinned to commit hashes as in `check.yml`.
- Tags: the commit SHA on every push, a moving `main`, and a version tag on release (designer's call). The VPS compose
  file pins an explicit tag, not a moving one.

### 5.8 Documentation and invariants

- **INV-013** (API handlers never fetch exchange data): stays **PLANNED** (designer's call). The Compose test is new
  evidence for a deployment-scoped claim, not for the invariant as written (5.6.3). Its "Target" column is updated to say
  that S21 added network isolation (INV-069) and that the application-level claim remains open. Rewording INV-013 into a
  deployment-scoped claim and marking it ENFORCED is the user's decision (U5).
- **INV-069** (new): "In the Compose deployment the `api` container is attached only to an `internal` network, has no
  host port binding and no default route, and its TCP, UDP and DNS egress probes fail while the same probes succeed from
  the `collector` container." Status ENFORCED by the `compose`-marked test once it runs in CI; "Not covered" copies
  5.6.3.
- **INV-044**: unchanged. It stays the code-level evidence; its "Not covered" clause gains a pointer to INV-069 for
  UDP and name resolution **in the deployment only**.
- **INV-014**: unchanged status. Its sentence "S21 stays the target for connection-level read-only in Docker Compose" is
  replaced by: S21 keeps the connection-level guarantee in the containers and adds no Docker-level read-only, because the
  volume must stay writable for WAL readers (spec section 7 point 4). The API image runs as the same UID as the Collector,
  so file permissions do not add a read-only guarantee either.
- **INV-063**: unchanged and still true. `config/quasar.compose.toml` is a second committed file, guarded against drift
  (5.2); the processes still read only their `CONFIG` file. The subnet and the fixed address live in the Compose file and
  configure the container, not the process. No environment variable is introduced. Variant 3 would add a start option and
  is therefore not approved here.
- **F-16**: stays open (5.4).
- `README.md`: a deployment section (pull, first start, upgrade, the SSH tunnel command to the fixed address, what the
  healthcheck means, and a warning that a copy of a live WAL database needs the `-wal` file or a SQLite backup).
- Spec roadmap line S21 marked implemented after the work. No new ADR: Docker Compose is already the accepted deployment
  (`docs/architecture.md`).

## 6. Constraints

- `make check` stays green and must not need Docker.
- No change of source behaviour in `domain`, `storage`, `api` or `collector`. If the work needs one, the builder stops
  and asks.
- The Collector stays the only writer and the API stays read-only (INV-014).
- The VPS has 2 GB RAM and nothing is built on it (the user).

## 7. Left to the builder

The Dockerfile layout, base image, healthcheck command, exact limits, log options, the subnet and the fixed address,
tag scheme details, `make` target names and the seeding mechanism. Not left to the builder: the network variant (5.5,
H1) and the invariant wording (5.8, U5).

## 8. Open items for the user

- **H1 (HARD STOP):** rerun Appendix A on stock Linux Docker Engine and accept or reject variant 1b against the ten
  conditions of 5.5 (host, other containers, other machines, egress). Any failure, or a required check that cannot be
  run, comes back to the user as a decision; there is no automatic fallback.
- U1: GHCR namespace, package visibility and free-tier quotas (private packages and Actions minutes have limits that
  were not checked). Designer's call until answered: private packages, names `quasar-market-data-collector` and
  `quasar-market-data-api` under the repository owner.
- U2: confirmation that the VPS is `amd64` (`uname -m`).
- U3: approval of the second config file `config/quasar.compose.toml` (5.2). Designer's call until answered.
- U4: if H1 rejects variant 1b, the choice among the options presented at that point (5.5); variant 3 is one of them and
  would need approval of a Unix-socket start option.
- U5: whether INV-013 stays PLANNED (designer's call) or is reworded as a deployment-scoped claim and marked ENFORCED.

## Appendix A. Network experiment (preliminary)

Throwaway, with stand-in containers; not part of the implementation and not a substitute for the test of 5.6, which runs
against the production Compose project. It covers H1 conditions 1 to 4, 6 and 10; conditions 5, 7, 8 and 9 need the
VPS and are listed in 5.5. Three files in an empty directory, then the commands below.

`compose.yaml`:

```yaml
name: s21-netexp
networks:
  isolated:
    internal: true
    ipam:
      config:
        - subnet: 172.31.250.0/24
  outside: {}
volumes:
  sock: {}
services:
  api:        # variant 1 (ports) and 1b (fixed address)
    image: python:3.12-alpine
    command: ["python", "-m", "http.server", "8000", "--bind", "0.0.0.0"]
    networks:
      isolated:
        ipv4_address: 172.31.250.10
    ports: ["127.0.0.1:18001:8000"]
    volumes: ["./probe.py:/probe.py:ro"]
  peer:       # Collector stand-in, positive control
    image: python:3.12-alpine
    command: ["sleep", "infinity"]
    networks: [isolated, outside]
    volumes: ["./probe.py:/probe.py:ro"]
  api_uds:    # variant 3 only
    image: python:3.12-alpine
    network_mode: none
    command: ["python", "/uds_server.py", "/sock/api.sock"]
    volumes: ["sock:/sock", "./uds_server.py:/uds_server.py:ro", "./probe.py:/probe.py:ro"]
```

`probe.py` (`egress | http HOST PORT | uds PATH`):

```python
import socket, struct, sys

def tcp(h, p):
    try:
        with socket.create_connection((h, p), timeout=3): return "CONNECTED"
    except OSError as e: return f"FAIL {type(e).__name__}: {e}"

def udp_dns(server):  # a reply proves UDP egress; silence alone proves nothing
    q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
    try:
        s.connect((server, 53)); s.send(q); return f"REPLY {len(s.recv(512))} bytes"
    except OSError as e: return f"FAIL {type(e).__name__}: {e}"
    finally: s.close()

def resolve(n):
    try: return "RESOLVED " + ",".join(sorted({a[4][0] for a in socket.getaddrinfo(n, 443)}))
    except OSError as e: return f"FAIL {type(e).__name__}: {e}"

def http(h, p):
    try:
        with socket.create_connection((h, p), timeout=3) as s:
            s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n"); return "HTTP " + s.recv(64).split(b"\r\n")[0].decode()
    except OSError as e: return f"FAIL {type(e).__name__}: {e}"

def uds(path):
    try:
        s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(path)
        s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n"); return "HTTP " + s.recv(64).split(b"\r\n")[0].decode()
    except OSError as e: return f"FAIL {type(e).__name__}: {e}"

m = sys.argv[1]
if m == "egress":
    print("tcp 1.1.1.1:443", tcp("1.1.1.1", 443)); print("tcp fapi.binance.com:443", tcp("fapi.binance.com", 443))
    print("udp dns 1.1.1.1:53", udp_dns("1.1.1.1")); print("resolve example.com", resolve("example.com"))
    routes = open("/proc/net/route").read().splitlines()[1:]
    print("default route", "PRESENT" if any(r.split()[1] == "00000000" for r in routes) else "ABSENT")
elif m == "http": print(http(sys.argv[2], int(sys.argv[3])))
elif m == "uds": print(uds(sys.argv[2]))
```

`uds_server.py`:

```python
import http.server, os, socketserver, sys
path = sys.argv[1]
if os.path.exists(path): os.remove(path)
class S(socketserver.UnixStreamServer):
    def get_request(self):
        c, _ = super().get_request(); return c, ("uds", 0)
S(path, http.server.SimpleHTTPRequestHandler).serve_forever()
```

Commands (`P` is the absolute path of `probe.py`):

```sh
docker version --format '{{.Server.Version}}'; docker info | grep -i -E 'firewall|userland'
docker compose up -d
docker port s21-netexp-api-1                                   # variant 1: is the port published?
docker inspect s21-netexp-api-1 --format '{{json .NetworkSettings.Ports}}'
docker exec s21-netexp-api-1  python /probe.py egress          # expect every probe FAIL, default route ABSENT
docker exec s21-netexp-peer-1 python /probe.py egress          # positive control: expect success
docker exec s21-netexp-api_uds-1 python /probe.py egress       # variant 3
docker run --rm --network host -v $P:/probe.py:ro python:3.12-alpine python /probe.py http 127.0.0.1 18001      # variant 1
docker run --rm --network host -v $P:/probe.py:ro python:3.12-alpine python /probe.py http 172.31.250.10 8000   # variant 1b, condition 1
docker run --rm -v $P:/probe.py:ro python:3.12-alpine python /probe.py http 172.31.250.10 8000                  # condition 2: expect FAIL
docker run --rm --network s21-netexp_outside -v $P:/probe.py:ro python:3.12-alpine python /probe.py http 172.31.250.10 8000   # condition 3: expect FAIL
docker run --rm --network none -v $P:/probe.py:ro python:3.12-alpine python /probe.py http 172.31.250.10 8000   # condition 4: expect FAIL
docker run --rm --network none -v s21-netexp_sock:/sock -v $P:/probe.py:ro python:3.12-alpine python /probe.py uds /sock/api.sock  # variant 3
# condition 4, on the VPS only, from another machine:  nc -vz -w 3 <vps public address> 8000   (expect failure)
docker compose down -v
```

Observed on OrbStack (Docker Engine 29.8.2, 2026-10-29): see the table in 5.5; in addition conditions 2 and 3
**failed** on OrbStack (a container on the default bridge and one on `outside` both got `200`), which is the main reason
H1 exists. Condition 4 was not run on OrbStack.

## Appendix B. H1 results

### B.1 Local run on stock Docker Engine (2026-10-29) — preliminary, not the VPS

- **Platform:** OrbStack Linux machine `s21-h1`, Ubuntu 24.04.5 LTS, **stock Docker Engine 29.1.3** from the Ubuntu
  `docker.io` package (its own `dockerd`, not OrbStack's engine), Compose 2.40.3, kernel 7.2.12-orbstack, **aarch64**,
  firewall backend iptables, `net.ipv4.ip_forward = 1`. "Another machine" was a second Linux machine `s21-h1-peer` on
  the same L2 segment (`192.168.139.0/24`). Appendix A files unchanged.
- **Not the target:** not the VPS, not `amd64`, an OrbStack kernel and VM network, the distribution package rather than
  `docker-ce`, no provider firewall. H1 still requires the run on the VPS (B.2).

| # | Condition | Expected | Observed | Result |
|---|-----------|----------|----------|--------|
| V1 | Port published on an internal network | — | `Ports: {"8000/tcp": null}`; `127.0.0.1:18001` refused | variant 1 not feasible (confirmed on stock engine) |
| 1 | Host → fixed address | 200 | `200 OK` (probe with `--network host`) and native `nc` succeeded | pass |
| 2 | Default-bridge container → fixed address | fail | timed out | pass |
| 3 | Other user-defined networks (`outside`, a fresh `h1-other`) → fixed address | fail | timed out (both) | pass |
| 4 | `network_mode: none` → fixed address | fail | `ENETUNREACH` | pass |
| 5 | Peer on `internal` → fixed address | 200, authorized | `200 OK` | recorded as authorized path |
| 6 | Another machine → host address `:8000` | fail | `Connection refused` (also `:18001`) | pass (necessary, not sufficient) |
| 7 | Another machine, route `172.31.250.0/24 via <host>` → fixed address | fail | timed out; `DOCKER-INTERNAL` rule 1 counter `3 pkts` on the fresh bridge | pass, end to end |
| 8 | Engine rules; listeners | drops for the internal bridge; no listener on 8000 | `DOCKER-INTERNAL ! -s 172.31.250.0/24 -o br-… -j DROP` and `! -d 172.31.250.0/24 -i br-… -j DROP`, jumped from `DOCKER-FORWARD`; `ss -ltnp`: none on 8000/18001 | pass |
| 9 | IPv6 | no IPv6 on `internal` | `EnableIPv6=false` | pass |
| 10 | API egress with positive control | API fails, peer succeeds | API: no default route, TCP and UDP `ENETUNREACH`, DNS fails; peer: TCP to `1.1.1.1` and `fapi.binance.com` connected, UDP DNS reply, resolution ok | pass |
| V3 | Unix socket, `network_mode: none` | — | `200 OK` through the socket | feasible (needs a start option; not approved) |

Note on 7: a control route to a container on a normal network (`172.18.0.2`) also timed out, dropped by Docker's
`raw PREROUTING -d 172.18.0.2 ! -i br-… -j DROP`; the evidence that the routed packets for 7 reached the host and were
dropped by the internal-network rule is the `DOCKER-INTERNAL` counter, not the timeout alone.

### B.2 VPS run — pending, required before the first deployment

Conditions 1 to 10 on the VPS itself (`amd64`, its engine version and firewall backend, provider network), per Task 0
of the plan. Condition 7 there needs a machine on the provider's private network or segment, or is recorded as not run.

### B.3 Decision (2026-10-29)

The user accepted variant 1b on the evidence of B.1 and allowed implementation to start. The VPS run of B.2 is moved
from the start of implementation to the release check before the first `docker compose pull` (plan Task 6 step 7);
a failed or unrunnable condition there stops the deployment and comes back to the user (5.5, no automatic fallback).
