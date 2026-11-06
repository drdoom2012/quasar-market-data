"""``compose.yaml`` pins the image tags of the project version (S21 release rule), checked without Docker.

``tests/deploy/test_compose_file.py`` checks the same on the resolved Compose configuration, but it needs the Docker
CLI and runs only in ``make compose-test``. This copy runs in ``make check`` and in the ``check`` workflow, so a
version bump that forgets ``compose.yaml`` (or the reverse) fails before it is pushed. It cannot tell whether the
tag exists in GHCR: that happens only after the git tag ``v<version>`` was pushed and ``images.yml`` succeeded.
"""

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVICES = ("api", "collector", "overview")


def project_version() -> str:
    version: str = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    return version


def pinned_images() -> list[str]:
    text = (REPO / "compose.yaml").read_text(encoding="utf-8")
    return sorted(re.findall(r"^\s*image:\s*(\S+)\s*$", text, flags=re.MULTILINE))


def test_compose_pins_exactly_the_three_images_at_the_project_version() -> None:
    expected = sorted(f"ghcr.io/drdoom2012/quasar-market-data-{name}:v{project_version()}" for name in SERVICES)
    assert pinned_images() == expected
