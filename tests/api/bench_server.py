"""Process helpers of the overview refresh benchmark: start ``quasar-api serve``, wait for it, reap it.

Copied in shape from ``tests/api/bench_indicators.py``, which stays untouched: deduplicating the two is a separate
change (OI-6a plan, task 1). Not part of ``make check``; the smoke test starts one server on 127.0.0.1.
"""

import http.client
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from socket import AF_INET, SOCK_STREAM, socket
from typing import Any

from tests.config_files import config_text

START_TIMEOUT_S = 60
STOP_TIMEOUT_S = 5
OPEN_INTEREST_TABLE = (
    "\n[open_interest]\n"
    "enabled = true\n"
    'period = "5m"\n'
    "publication_delay_ms = 15000\n"
    "round_offset_ms = 200000\n"
    "budget_per_5min = 400\n"
)


class BenchError(Exception):
    pass


def free_port() -> int:
    with socket(AF_INET, SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def peak_rss_bytes(ru_maxrss: int) -> int:
    """``ru_maxrss`` is bytes on macOS and KiB on Linux."""
    return ru_maxrss if sys.platform == "darwin" else ru_maxrss * 1024


def start_api(db: Path, symbols: Sequence[str], log: Path) -> tuple["subprocess.Popen[bytes]", int]:
    """Start ``quasar-api serve`` on a free loopback port with open interest enabled; return it and the port."""
    script = Path(sys.executable).parent / "quasar-api"
    if not script.exists():
        raise BenchError(f"the console script {script} is not installed in this environment")
    port = free_port()
    config = log.parent / "quasar.toml"
    text = config_text(database_path=str(db), symbols=list(symbols), host="127.0.0.1", port=port)
    config.write_text(text + OPEN_INTEREST_TABLE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    with log.open("wb") as log_file:
        proc = subprocess.Popen(
            [str(script), "serve", str(config)], stdout=subprocess.DEVNULL, stderr=log_file, env=env
        )
    try:
        _wait_until_up(proc, port)
    except BaseException:
        if proc.poll() is None:
            reap(proc)
        raise
    return proc, port


def _wait_until_up(proc: "subprocess.Popen[bytes]", port: int) -> None:
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise BenchError(f"quasar-api exited with {proc.returncode} during startup")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/health")
            if connection.getresponse().status == 200:
                return
        except (OSError, http.client.HTTPException):
            time.sleep(0.1)
        finally:
            connection.close()
    raise BenchError(f"quasar-api did not answer /health within {START_TIMEOUT_S} s")


def reap(proc: "subprocess.Popen[bytes]") -> tuple[str, Any]:
    """Stop the server (SIGINT, SIGKILL after 5 s) and reap that one child with ``os.wait4``."""
    stopped = "sigint"
    proc.send_signal(signal.SIGINT)
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while True:
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            break
        if time.monotonic() >= deadline:
            stopped = "sigkill"
            proc.kill()
            pid, status, usage = os.wait4(proc.pid, 0)
            break
        time.sleep(0.02)
    proc.returncode = os.waitstatus_to_exitcode(status)  # Popen must not wait for the reaped child again
    return stopped, usage
