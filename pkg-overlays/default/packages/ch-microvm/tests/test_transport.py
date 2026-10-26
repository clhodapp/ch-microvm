# SPDX-License-Identifier: MIT
"""The hybrid vsock transport against a fake VMM on a Unix socket: the
accepted connection, a refused CONNECT, an answer that is not `OK `, a
VMM that closes the connection, a missing socket, a socket nothing
listens on, a socket the user may not open, a socket path too long for
connect(2), and a VMM that never answers."""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

import fixture  # noqa: F401 (puts the library on the path)
from ch_microvm.testing import FakeVmm
from ch_microvm.transport import MAX_SOCKET_PATH, Endpoint, connect, probe


class HybridCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "notify.vsock"
        self.endpoint = Endpoint(instance="probe-1", port=5000, vsock_socket=str(self.path))

    def vmm(self, **kwargs) -> FakeVmm:
        vmm = FakeVmm(self.path, **kwargs)
        self.addCleanup(vmm.close)
        return vmm

    def test_an_accepted_connection_is_the_stream_of_the_guest(self) -> None:
        def accepted(conn: socket.socket, port: int) -> None:
            conn.sendall(f"guest port {port}\n".encode())
            conn.close()

        vmm = self.vmm(accepted=accepted)
        with connect(self.endpoint, 1.0) as sock:
            self.assertIsNone(sock.gettimeout())
            # Nothing past the answer to CONNECT was consumed.
            self.assertEqual(sock.makefile("rb").readline(), b"guest port 5000\n")
        self.assertEqual(vmm.requests, [b"CONNECT 5000\n"])
        probe(self.endpoint, 1.0)

    def test_a_refused_connect(self) -> None:
        self.vmm(answer=lambda port: None)
        with self.assertRaisesRegex(
            ConnectionRefusedError, f"probe-1 refused CONNECT 5000 on {self.path}: the VMM closed the connection"
        ):
            connect(self.endpoint, 1.0)
        with self.assertRaises(OSError):
            probe(self.endpoint, 1.0)

    def test_an_answer_that_is_not_ok(self) -> None:
        for line in (b"NOPE\n", b"OKAY 1\n", b"\x00\xff garbage\n", b"OK"):
            with self.subTest(line=line):
                vmm = FakeVmm(self.path, answer=lambda port: line)
                try:
                    with self.assertRaisesRegex(ConnectionRefusedError, "probe-1 refused CONNECT 5000 .*the VMM answered"):
                        connect(self.endpoint, 1.0)
                finally:
                    vmm.close()
                    self.path.unlink()

    def test_a_missing_socket(self) -> None:
        with self.assertRaisesRegex(
            FileNotFoundError, f"the vsock socket {self.path} of probe-1 does not exist; the VMM of probe-1 does not run"
        ):
            connect(self.endpoint, 1.0)

    def test_a_socket_nothing_listens_on(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        stale.close()
        with self.assertRaisesRegex(ConnectionRefusedError, "nothing listens on the vsock socket"):
            connect(self.endpoint, 1.0)

    @unittest.skipIf(os.geteuid() == 0, "root opens any socket")
    def test_a_socket_the_user_may_not_open(self) -> None:
        self.vmm()
        self.path.chmod(0)
        with self.assertRaisesRegex(PermissionError, f"^permission denied on the vsock socket {self.path} of probe-1$"):
            connect(self.endpoint, 1.0)

    def test_a_socket_path_too_long_for_connect(self) -> None:
        deep = self.root / ("d" * 60) / ("e" * 60)
        deep.mkdir(parents=True)
        path = deep / "notify.vsock"
        self.assertGreater(len(str(path)), MAX_SOCKET_PATH)
        # The fake VMM binds through the directory, as cloud-hypervisor
        # binds the name relative to its working directory.
        fd = os.open(deep, os.O_PATH | os.O_DIRECTORY)
        try:
            vmm = FakeVmm(Path(f"/proc/self/fd/{fd}/notify.vsock"))
        finally:
            os.close(fd)
        self.addCleanup(vmm.close)
        probe(Endpoint(instance="probe-1", port=5000, vsock_socket=str(path)), 1.0)
        self.assertEqual(vmm.requests, [b"CONNECT 5000\n"])

    def test_a_vmm_that_never_answers(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def hold(port: int) -> bytes:
            release.wait(10)
            return b""

        self.vmm(answer=hold)
        with self.assertRaisesRegex(TimeoutError, "the VMM of probe-1 did not answer CONNECT 5000 .* within 0.2 seconds"):
            connect(self.endpoint, 0.2)

    def test_the_endpoint_names_its_socket(self) -> None:
        self.assertEqual(str(self.endpoint), f"vsock port 5000 through {self.path}")


if __name__ == "__main__":
    unittest.main()
