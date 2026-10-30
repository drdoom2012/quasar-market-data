"""Shape of the production Compose project and of the build override (S21 spec 5.3, 5.4).

These tests read the configuration Compose resolves; they need the Docker CLI, not a running engine.
"""

import copy
import tomllib
from typing import Any

import pytest

from tests.deploy.docker_cli import API_ADDRESS, IMAGES, PROD, REPO, TEST, resolved_config

pytestmark = pytest.mark.compose

SERVICES = ("api", "collector")
GIB = 1024**3


@pytest.fixture(scope="module")
def prod() -> dict[str, Any]:
    return resolved_config(*PROD)


def network_keys(config: dict[str, Any], service: str) -> set[str]:
    return set(config["services"][service].get("networks", {}))


def test_api_is_on_the_internal_network_only(prod: dict[str, Any]) -> None:
    api = prod["services"]["api"]
    assert set(api["networks"]) == {"internal"}
    assert api["networks"]["internal"]["ipv4_address"] == API_ADDRESS
    assert "ports" not in api
    assert "network_mode" not in api


def test_internal_network_is_internal_with_the_fixed_subnet(prod: dict[str, Any]) -> None:
    internal = prod["networks"]["internal"]
    assert internal["internal"] is True
    assert [entry["subnet"] for entry in internal["ipam"]["config"]] == ["172.31.250.0/24"]
    assert not internal.get("enable_ipv6", False)


def test_collector_is_on_internal_and_egress(prod: dict[str, Any]) -> None:
    assert network_keys(prod, "collector") == {"internal", "egress"}
    assert not prod["networks"]["egress"].get("internal", False)


@pytest.mark.parametrize("service", SERVICES)
def test_both_services_mount_the_volume_rw_and_the_config_ro(prod: dict[str, Any], service: str) -> None:
    mounts = {mount["target"]: mount for mount in prod["services"][service]["volumes"]}
    data = mounts["/app/data"]
    assert data["type"] == "volume"
    assert not data.get("read_only", False)
    config = mounts["/app/config/quasar.compose.toml"]
    assert config["type"] == "bind"
    assert config["source"] == str(REPO / "config" / "quasar.compose.toml")
    assert config["read_only"] is True


def test_restart_limits_and_log_rotation(prod: dict[str, Any]) -> None:
    total = 0
    for service in SERVICES:
        definition = prod["services"][service]
        assert definition["restart"] == "unless-stopped"
        limit = int(definition["mem_limit"])
        assert limit > 0
        total += limit
        logging = definition["logging"]
        assert logging["driver"] == "json-file"
        assert {"max-size", "max-file"} <= set(logging["options"])
    assert total <= GIB


def test_collector_stop_grace_outlasts_its_shutdown_grace(prod: dict[str, Any]) -> None:
    # Docker's default 10 s would SIGKILL the Collector inside its own 10 s shutdown grace plus the 2 s post-check.
    assert prod["services"]["collector"]["stop_grace_period"] == "30s"


def test_collector_healthcheck_is_the_process_check(prod: dict[str, Any]) -> None:
    assert prod["services"]["collector"]["healthcheck"]["test"] == [
        "CMD",
        "grep",
        "-qa",
        "quasar-collector",
        "/proc/1/cmdline",
    ]
    assert "healthcheck" not in prod["services"]["api"]


@pytest.mark.parametrize("service", SERVICES)
def test_entrypoints_are_exec_form(prod: dict[str, Any], service: str) -> None:
    # Compose resolves an unset entrypoint or command to null; the image's exec-form values then apply.
    definition = prod["services"][service]
    assert definition.get("entrypoint") is None
    assert definition.get("command") is None


def test_production_images_pin_the_project_version(prod: dict[str, Any]) -> None:
    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    for service in SERVICES:
        expected = f"ghcr.io/drdoom2012/quasar-market-data-{service}:v{version}"
        assert prod["services"][service]["image"] == expected


def test_build_override_changes_only_the_image(prod: dict[str, Any]) -> None:
    test = resolved_config(*TEST)
    for service in SERVICES:
        assert test["services"][service]["image"] == IMAGES[service]
        assert test["services"][service]["build"]["target"] == service
    stripped_prod, stripped_test = copy.deepcopy(prod), copy.deepcopy(test)
    for config in (stripped_prod, stripped_test):
        for service in SERVICES:
            config["services"][service].pop("image", None)
            config["services"][service].pop("build", None)
    assert stripped_test == stripped_prod
