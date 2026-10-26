# SPDX-License-Identifier: MIT
"""Connections to the vsock port of a guest.

Every guest runs under cloud-hypervisor, whose hybrid vsock is a Unix
socket on the host: `notify.vsock` in the directory of the instance.
`connect` connects to that socket, sends `CONNECT <port>\\n`, and reads
one line. The VMM answers `OK <host-port>\\n` when the guest accepts the
connection. Any other line, or the end of the stream, means the guest
refused it. After the answer, the stream is the connection to the port
of the guest.

The readiness probe of `up` goes through `connect`. A program that
talks to the service of a guest on its port connects through `connect`
as well.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass

# The longest answer to CONNECT that `connect` reads. `OK <host-port>\n`
# is far shorter.
MAX_ANSWER = 64

# The longest socket path that `connect` passes to connect(2) as it is.
# The kernel takes at most 107 bytes. A longer path is reached through
# a descriptor of its directory.
MAX_SOCKET_PATH = 100


@dataclass(frozen=True)
class Endpoint:
    """The vsock port `port` of the guest of `instance`, reached through
    the hybrid vsock socket at `vsock_socket`."""

    instance: str
    port: int
    vsock_socket: str

    def __str__(self) -> str:
        return f"vsock port {self.port} through {self.vsock_socket}"


def connect(endpoint: Endpoint, timeout: float) -> socket.socket:
    """A connection to the port of `endpoint`. Connecting, and the
    exchange of the hybrid vsock, each wait at most `timeout` seconds.
    The returned socket blocks without a timeout. A failure raises
    OSError, with a message that names the instance."""
    path = endpoint.vsock_socket
    name = endpoint.instance
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        try:
            connect_path(sock, path)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"the vsock socket {path} of {name} does not exist; the VMM of {name} does not run"
            ) from None
        except PermissionError:
            raise PermissionError(f"permission denied on the vsock socket {path} of {name}") from None
        except ConnectionRefusedError:
            raise ConnectionRefusedError(
                f"nothing listens on the vsock socket {path} of {name}; the VMM of {name} does not run"
            ) from None
        try:
            sock.sendall(f"CONNECT {endpoint.port}\n".encode())
            answer = read_answer(sock)
        except TimeoutError:
            raise TimeoutError(
                f"the VMM of {name} did not answer CONNECT {endpoint.port} on {path} within {timeout:g} seconds"
            ) from None
        if not answer.startswith(b"OK "):
            shown = answer.decode("utf-8", "replace").strip()
            raise ConnectionRefusedError(
                f"{name} refused CONNECT {endpoint.port} on {path}: "
                + (f"the VMM answered {shown!r}" if shown else "the VMM closed the connection")
            )
        sock.settimeout(None)
    except BaseException:
        sock.close()
        raise
    return sock


def connect_path(sock: socket.socket, path: str) -> None:
    """Connect `sock` to the Unix socket at `path`. A path too long for
    connect(2) is reached as `/proc/self/fd/<fd>/<name>`, with `fd` a
    descriptor of its directory."""
    if len(os.fsencode(path)) <= MAX_SOCKET_PATH:
        sock.connect(path)
        return
    directory, name = os.path.split(path)
    fd = os.open(directory, os.O_PATH | os.O_DIRECTORY)
    try:
        sock.connect(f"/proc/self/fd/{fd}/{name}")
    finally:
        os.close(fd)


def probe(endpoint: Endpoint, timeout: float) -> None:
    """Return when the guest accepts a connection on the port of
    `endpoint`, and raise OSError as `connect` does when it does not."""
    connect(endpoint, timeout).close()


def read_answer(sock: socket.socket) -> bytes:
    """The line the VMM answers CONNECT with, read a byte at a time so
    that nothing the guest sends after it is consumed. The result ends
    without its newline, and is empty or partial when the stream ends
    first."""
    answer = bytearray()
    while len(answer) < MAX_ANSWER:
        byte = sock.recv(1)
        if not byte or byte == b"\n":
            break
        answer += byte
    return bytes(answer)
