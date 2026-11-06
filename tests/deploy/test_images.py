"""The collector and api images: entrypoints, platform, user, SQLite, TA-Lib, no local state (S21 spec 5.1).

`IMAGES` is the two data images, so these parametrized tests do not see the symbol overview image; its own checks
are in `test_overview_image.py`.
"""

from importlib.metadata import version

import pytest

from tests.deploy.docker_cli import IMAGES, docker

pytestmark = pytest.mark.compose

CASES = sorted(IMAGES.items())
ENTRYPOINTS = {"collector": "quasar-collector", "api": "quasar-api"}


def run(tag: str, *command: str, entrypoint: str | None = None) -> str:
    args = ["run", "--rm"] + (["--entrypoint", entrypoint] if entrypoint else []) + [tag, *command]
    return docker(*args).stdout.strip()


@pytest.mark.parametrize(("name", "tag"), CASES)
def test_image_runs_the_entrypoint_version(name: str, tag: str) -> None:
    assert run(tag, "--version") == f"{ENTRYPOINTS[name]} {version('quasar-market-data')}"


@pytest.mark.parametrize(("name", "tag"), CASES)
def test_image_runs_as_uid_10001(name: str, tag: str) -> None:
    assert run(tag, "-u", entrypoint="id") == "10001"
    assert run(tag, "-g", entrypoint="id") == "10001"


@pytest.mark.parametrize(("name", "tag"), CASES)
def test_image_is_linux_amd64(name: str, tag: str) -> None:
    assert docker("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", tag).stdout.strip() == "linux/amd64"


@pytest.mark.parametrize(("name", "tag"), CASES)
def test_image_sqlite_is_new_enough(name: str, tag: str) -> None:
    code = "from quasar_market_data.storage.connection import ensure_sqlite_version; ensure_sqlite_version()"
    run(tag, "-c", code, entrypoint="python")


def test_api_image_loads_talib() -> None:
    assert run(IMAGES["api"], "-c", "import talib; print(talib.__ta_version__)", entrypoint="python")


@pytest.mark.parametrize(("name", "tag"), CASES)
def test_images_contain_no_local_state(name: str, tag: str) -> None:
    run(tag, "-c", "test ! -e /app/data/quasar.db && test ! -e /app/.venv && test -d /app/data", entrypoint="sh")
    assert run(tag, "-c", "%u", "/app/data", entrypoint="stat") == "10001"
