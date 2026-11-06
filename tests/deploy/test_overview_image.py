"""The symbol overview image: serves the page, reports an unreachable API, runs unprivileged, linux/amd64.

Needs `make images` first (nothing is built here), like the isolation test. The container gets no API: the overview
is pointed at a closed port on its own loopback, which is the page-level error case (overview spec, section 5.1).
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from tests.deploy.docker_cli import OVERVIEW_IMAGE, OVERVIEW_PORT, docker

pytestmark = pytest.mark.compose


def fetch(url: str) -> tuple[int, str, str]:
    """(status, content type, body) of a GET; an HTTP error status is a result here, not an exception."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.headers.get("Content-Type", ""), error.read().decode("utf-8")


class Running:
    def __init__(self, container: str, base_url: str) -> None:
        self.container = container
        self.base_url = base_url


@pytest.fixture(scope="module")
def running_overview() -> Iterator[Running]:
    container = docker(
        "run", "-d", "--rm", "--init", "-p", f"127.0.0.1::{OVERVIEW_PORT}", OVERVIEW_IMAGE,
        "--host", "0.0.0.0", "--api-url", "http://127.0.0.1:1",
    ).stdout.strip()  # fmt: skip
    try:
        first_line = docker("port", container, f"{OVERVIEW_PORT}/tcp").stdout.splitlines()[0]
        base_url = f"http://127.0.0.1:{first_line.rsplit(':', 1)[1]}"
        deadline = time.monotonic() + 30
        while True:
            try:
                if fetch(f"{base_url}/")[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(
                    f"the overview did not answer GET / within 30 s\n{docker('logs', container, check=False).stderr}"
                )
            time.sleep(0.5)
        yield Running(container, base_url)
    finally:
        docker("stop", "-t", "10", container, check=False)


def test_the_page_is_served(running_overview: Running) -> None:
    status, content_type, _ = fetch(f"{running_overview.base_url}/")
    assert status == 200
    assert content_type.startswith("text/html")


def test_unreachable_api_is_a_page_level_error(running_overview: Running) -> None:
    status, _, body = fetch(f"{running_overview.base_url}/overview.json")
    assert status == 502
    assert json.loads(body) == {"api_error": {"code": "transport", "detail": "connection"}}


def test_runs_as_the_unprivileged_user(running_overview: Running) -> None:
    assert docker("exec", running_overview.container, "id", "-u").stdout.strip() == "10001"


def test_image_is_linux_amd64() -> None:
    assert docker("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", OVERVIEW_IMAGE).stdout.strip() == (
        "linux/amd64"
    )
