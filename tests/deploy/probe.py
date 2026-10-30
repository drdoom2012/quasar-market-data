"""Network probe run inside a container from stdin (S21 spec 5.6); standard library only.

python - egress                -> one JSON object with the egress results
python - http HOST PORT PATH   -> the HTTP status code, or fail:<error>
"""

import errno
import json
import socket
import struct
import sys

TIMEOUT_S = 3


def failure(error: OSError) -> str:
    name = errno.errorcode.get(error.errno or 0) if isinstance(error.errno, int) else None
    return f"fail:{name or type(error).__name__}"


def tcp(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_S):
            return "ok"
    except OSError as error:
        return failure(error)


def udp_dns(server: str) -> str:
    # A reply proves UDP egress; the test requires an immediate error, because silence alone proves nothing.
    query = struct.pack(">HHHHHH", 0x5121, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT_S)
    try:
        sock.connect((server, 53))
        sock.send(query)
        sock.recv(512)
        return "ok"
    except OSError as error:
        return failure(error)
    finally:
        sock.close()


def resolve(name: str) -> str:
    try:
        socket.getaddrinfo(name, 443)
        return "ok"
    except OSError as error:
        return failure(error)


def default_route() -> bool:
    with open("/proc/net/route") as routes:
        return any(line.split()[1] == "00000000" for line in routes.read().splitlines()[1:])


def ipv6_global() -> bool:
    try:
        with open("/proc/net/if_inet6") as addresses:
            return any(line.split()[3] == "00" for line in addresses)
    except FileNotFoundError:
        return False


def http(host: str, port: int, path: str) -> str:
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_S) as sock:
            sock.sendall(f"GET {path} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
            return sock.recv(64).split(b"\r\n")[0].split()[1].decode()
    except OSError as error:
        return failure(error)


if __name__ == "__main__":
    if sys.argv[1] == "egress":
        print(
            json.dumps(
                {
                    "tcp_ip": tcp("1.1.1.1", 443),
                    "tcp_name": tcp("fapi.binance.com", 443),
                    "udp_dns": udp_dns("1.1.1.1"),
                    "resolve": resolve("fapi.binance.com"),
                    "default_route": default_route(),
                    "ipv6_global": ipv6_global(),
                }
            )
        )
    elif sys.argv[1] == "http":
        print(http(sys.argv[2], int(sys.argv[3]), sys.argv[4]))
