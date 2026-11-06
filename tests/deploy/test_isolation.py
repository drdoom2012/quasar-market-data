"""Isolation of the api and overview services, checked against the running production Compose project (S21 spec 5.6).

The project is ``compose.yaml`` with only the image source swapped by ``compose.build.yaml``. The Collector service is
never started (no exchange call); one-off ``collector`` containers seed the volume and run the positive egress control.
The overview service (a third container with no volume, reaching the API by IP) gets the same network checks.

What this proves: in this Compose file on this engine, the api container had no route out and the probed TCP, UDP
and DNS attempts failed at the time of the run, while the host reached it and unauthorized containers did not. It
does not prove that API handlers never try to fetch exchange data (INV-013; the code-level evidence is INV-044).
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.deploy.docker_cli import API_ADDRESS, API_PORT, IMAGES, OVERVIEW_ADDRESS, OVERVIEW_PORT, TEST, docker

pytestmark = pytest.mark.compose

HERE = Path(__file__).resolve().parent
PROBE = (HERE / "probe.py").read_text(encoding="utf-8")
SEED = (HERE / "seed_db.py").read_text(encoding="utf-8")
PROJECT = "s21-test"
COMPOSE = ["compose", "-p", PROJECT, *(arg for file in TEST for arg in ("-f", file))]
DB = "/app/data/quasar.db"
SUBNET = "172.31.250.0/24"
SEED_START_MS = 1_767_225_600_000
SEED_COUNT = 12
STEP_MS = 300_000
NOT_STOCK = ("OrbStack", "Docker Desktop")


@dataclass(frozen=True)
class Project:
    seed_returncode: int
    seed_output: str


def compose(*args: str, input: str | None = None, check: bool = True) -> str:
    return docker(*COMPOSE, *args, input=input, check=check).stdout


def get(path: str) -> tuple[int, Any]:
    with urllib.request.urlopen(f"http://{API_ADDRESS}:{API_PORT}{path}", timeout=5) as response:
        return response.status, json.loads(response.read())


def get_overview(path: str) -> tuple[int, Any]:
    """(status, parsed body); an HTTP error status is a result here, so a 502 shows its ``api_error`` body."""
    # 90 s: the first refresh over the 522 shipped symbols takes ~15-19 s; the tool's own deadline is 60 s.
    try:
        with urllib.request.urlopen(f"http://{OVERVIEW_ADDRESS}:{OVERVIEW_PORT}{path}", timeout=90) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read())


def container_id(service: str) -> str:
    return compose("ps", "-a", "-q", service).strip()


@pytest.fixture(scope="module")
def project() -> Iterator[Project]:
    operating_system = docker("info", "--format", "{{.OperatingSystem}}").stdout.strip()
    if any(name in operating_system for name in NOT_STOCK):
        pytest.fail(f"the isolation test needs a stock Linux Docker Engine; this engine is {operating_system!r}")
    compose("down", "-v", check=False)
    compose("up", "-d", "--no-build", "api", "overview")
    try:
        seeded = docker(
            *COMPOSE, "run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "collector", "-", DB,
            input=SEED, check=False,
        )  # fmt: skip
        deadline = time.monotonic() + 30
        while True:
            try:
                if get("/health")[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("the api did not answer /health on its fixed address within 30 s")
            time.sleep(1)
        # Wait on the page, not on /overview.json: the overview keeps a snapshot, errors included, for 15 s.
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(f"http://{OVERVIEW_ADDRESS}:{OVERVIEW_PORT}/", timeout=5) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("the overview did not answer GET / on its fixed address within 30 s")
            time.sleep(1)
        yield Project(seeded.returncode, seeded.stdout + seeded.stderr)
    finally:
        compose("down", "-v", check=False)


def egress(*run_prefix: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(docker(*run_prefix, "-", "egress", input=PROBE).stdout)
    return result


# --- positive checks: every negative check below depends on them -------------------------------------------------


def test_host_reaches_the_api_and_gets_seeded_candles(project: Project) -> None:
    end = SEED_START_MS + SEED_COUNT * STEP_MS
    status, body = get(
        f"/v1/candles?venue=binance-usdm&symbol=BTC/USDT:USDT&timeframe=5m&start={SEED_START_MS}&end={end}"
    )
    assert status == 200
    assert [candle["open_time_ms"] for candle in body["candles"]] == [
        SEED_START_MS + i * STEP_MS for i in range(SEED_COUNT)
    ]
    status, ingest = get("/health/ingest")
    assert status == 200
    assert ingest["series_count"] == 1


def test_host_reaches_the_overview_and_it_sees_the_seeded_symbol(project: Project) -> None:
    # The overview reached the API over the internal network by IP (no DNS). First request of /overview.json, after
    # the seed has finished.
    status, body = get_overview("/overview.json")
    assert status == 200, body
    assert "BTC/USDT:USDT" in [entry["symbol"] for entry in body["symbols"]]


def test_probe_succeeds_from_the_collector(project: Project) -> None:
    result = egress(*COMPOSE, "run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "collector")
    failed = {key: result[key] for key in ("tcp_ip", "tcp_name", "udp_dns", "resolve") if result[key] != "ok"}
    assert not failed, f"the positive egress control failed, so the negative checks would prove nothing: {failed}"


def test_seed_into_a_fresh_volume_as_non_root(project: Project) -> None:
    assert project.seed_returncode == 0, project.seed_output
    assert compose("exec", "-T", "api", "stat", "-c", "%u", DB).strip() == "10001"


def test_api_start_line_names_the_volume_database(project: Project) -> None:
    logs = compose("logs", "api")
    assert "config path=/app/config/quasar.compose.toml" in logs
    assert f"database={DB}" in logs


# --- network checks on the running api container -----------------------------------------------------------------


def test_running_api_has_exactly_the_internal_network(project: Project) -> None:
    settings = json.loads(docker("inspect", container_id("api")).stdout)[0]["NetworkSettings"]
    assert list(settings["Networks"]) == [f"{PROJECT}_internal"]
    assert settings["Networks"][f"{PROJECT}_internal"]["IPAddress"] == API_ADDRESS
    assert [binding for binding in (settings.get("Ports") or {}).values() if binding] == []
    network = json.loads(docker("network", "inspect", f"{PROJECT}_internal").stdout)[0]
    assert network["Internal"] is True


def test_api_has_no_route_and_no_egress(project: Project) -> None:
    result = egress(*COMPOSE, "exec", "-T", "api", "python")
    assert result["default_route"] is False
    assert result["ipv6_global"] is False
    for key in ("tcp_ip", "tcp_name", "udp_dns", "resolve"):
        assert result[key].startswith("fail:"), (key, result)
    assert result["udp_dns"] == "fail:ENETUNREACH"


def test_running_overview_has_exactly_the_internal_network(project: Project) -> None:
    settings = json.loads(docker("inspect", container_id("overview")).stdout)[0]["NetworkSettings"]
    assert list(settings["Networks"]) == [f"{PROJECT}_internal"]
    assert settings["Networks"][f"{PROJECT}_internal"]["IPAddress"] == OVERVIEW_ADDRESS
    assert [binding for binding in (settings.get("Ports") or {}).values() if binding] == []
    network = json.loads(docker("network", "inspect", f"{PROJECT}_internal").stdout)[0]
    assert network["Internal"] is True


def test_overview_has_no_route_and_no_egress(project: Project) -> None:
    result = egress(*COMPOSE, "exec", "-T", "overview", "python")
    assert result["default_route"] is False
    assert result["ipv6_global"] is False
    for key in ("tcp_ip", "tcp_name", "udp_dns", "resolve"):
        assert result[key].startswith("fail:"), (key, result)
    assert result["udp_dns"] == "fail:ENETUNREACH"


def test_unauthorized_containers_cannot_reach_the_api(project: Project) -> None:
    probe = ["run", "--rm", "-i", "--entrypoint", "python"]
    target = ["-", "http", API_ADDRESS, str(API_PORT), "/health"]
    on_bridge = docker(*probe, IMAGES["api"], *target, input=PROBE).stdout.strip()
    other = f"{PROJECT}-other"
    docker("network", "create", other)
    try:
        on_other = docker(*probe, "--network", other, IMAGES["api"], *target, input=PROBE).stdout.strip()
    finally:
        docker("network", "rm", other)
    assert on_bridge.startswith("fail:"), on_bridge
    assert on_other.startswith("fail:"), on_other


def test_engine_installed_the_isolation_rules(project: Project) -> None:
    info = json.loads(docker("info", "--format", "{{json .}}").stdout)
    engine = info.get("ServerVersion")
    backend = (info.get("FirewallBackend") or {}).get("Driver", "iptables")
    sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
    command = [*sudo, "nft", "list", "ruleset"] if backend == "nftables" else [*sudo, "iptables-save"]
    dump = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30).stdout
    network_id = json.loads(docker("network", "inspect", f"{PROJECT}_internal").stdout)[0]["Id"]
    bridge = f"br-{network_id[:12]}"
    expected = [
        f"-A DOCKER-INTERNAL ! -s {SUBNET} -o {bridge} -j DROP",
        f"-A DOCKER-INTERNAL ! -d {SUBNET} -i {bridge} -j DROP",
    ]
    missing = [rule for rule in expected if rule not in dump]
    assert backend == "iptables" and not missing, (
        f"engine {engine}, backend {backend}: expected rules {missing or expected} not found\n{dump}"
    )


def test_overview_stops_on_sigterm(project: Project) -> None:
    # docker-init as PID 1 forwards SIGTERM; Python as PID 1 would ignore it and wait for the 10 s SIGKILL.
    started = time.monotonic()
    compose("stop", "-t", "10", "overview")
    assert time.monotonic() - started < 10


def test_api_stops_on_sigterm(project: Project) -> None:
    # Runs last in this module: it stops the api.
    started = time.monotonic()
    compose("stop", "-t", "10", "api")
    elapsed = time.monotonic() - started
    exit_code = docker("inspect", "--format", "{{.State.ExitCode}}", container_id("api")).stdout.strip()
    assert elapsed < 10
    assert exit_code == "0"
