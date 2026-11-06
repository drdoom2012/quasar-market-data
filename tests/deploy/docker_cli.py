"""Thin wrapper around the Docker CLI for the `compose`-marked deployment tests (S21).

`IMAGES` holds the two data images (Collector, API); the symbol overview image is `OVERVIEW_IMAGE`.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
IMAGES: dict[str, str] = {
    "collector": "quasar-market-data-collector:local",
    "api": "quasar-market-data-api:local",
}
OVERVIEW_IMAGE = "quasar-market-data-overview:local"  # not in IMAGES: the overview is not a data image
PROD = ("compose.yaml",)
TEST = ("compose.yaml", "compose.build.yaml")
API_ADDRESS = "172.31.250.10"
API_PORT = 8000
OVERVIEW_ADDRESS = "172.31.250.11"
OVERVIEW_PORT = 8765


def docker(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``docker ARGS`` from the repository root; on failure with ``check`` raise with stdout and stderr."""
    result = subprocess.run(
        ["docker", *args], cwd=REPO, input=input, capture_output=True, text=True, check=False, timeout=600
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args)} exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def resolved_config(*files: str) -> dict[str, Any]:
    """The configuration Compose resolves from ``files`` (in order), as JSON."""
    args = [arg for file in files for arg in ("-f", file)]
    config: dict[str, Any] = json.loads(docker("compose", *args, "config", "--format", "json").stdout)
    return config
