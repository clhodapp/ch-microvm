# SPDX-License-Identifier: MIT
"""The fixture the tests share: the fakes of `ch_microvm.testing` in a
temporary directory, and a configuration of three kinds. `probe` has
two instances, shares and a vsock port. `other` has a vsock port.
`quiet` has neither shares nor a vsock port."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from ch_microvm.instances import Holder, Instances  # noqa: E402
from ch_microvm.testing import FakeProc, FakeSystemd, fake_runner  # noqa: E402
from ch_microvm.transport import Endpoint  # noqa: E402

UID = 1234
PATH = "/fake/systemd/bin:/fake/coreutils/bin"


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.proc = FakeProc(self.root / "proc")
        self.systemd = FakeSystemd(self.root)
        self.state = self.root / "state"
        self.runner = fake_runner(self.root / "runner-probe", virtiofsd=True, notify=True)
        self.quiet_runner = fake_runner(self.root / "runner-quiet", virtiofsd=False, notify=False)
        # The state root of every kind. No instance directory exists
        # until a test or `up` creates it.
        self.instances_root = self.root / "instances"
        common = {"stateRoot": str(self.instances_root), "runtimeMaxSec": None}
        self.config = {
            "kinds": {
                "probe": common
                | {
                    "runner": str(self.runner),
                    "shares": ["in", "out"],
                    "vsockPort": 5000,
                    "maxInstances": 2,
                    "runtimeMaxSec": 3600,
                },
                "other": common
                | {
                    "runner": str(self.runner),
                    "shares": [],
                    "vsockPort": 6000,
                    "maxInstances": 1,
                },
                "quiet": common
                | {
                    "runner": str(self.quiet_runner),
                    "shares": [],
                    "vsockPort": None,
                    "maxInstances": 1,
                },
            },
            "stateDir": str(self.state),
            "systemctl": self.systemd.systemctl,
            "systemdRun": self.systemd.systemd_run,
            "path": PATH,
            "upTimeout": 10,
            "procRoot": str(self.proc.root),
        }
        self.answering: set[str] = {"probe-1", "probe-2", "other-1"}
        # The fake systemd stamps units with the real time.
        self.now = time.time()

    def process(self, pid: int, start: str = "4242") -> Holder:
        self.proc.process(pid, start)
        return Holder(pid=pid, start=start, label=f"label-{pid}")

    def end(self, pid: int) -> None:
        self.proc.end(pid)

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def probe(self, endpoint: Endpoint, timeout: float) -> None:
        if endpoint.instance not in self.answering:
            raise ConnectionRefusedError(f"{endpoint.instance} does not answer")

    def instances(self, kinds: list[str] | None = None) -> tuple[Instances, io.StringIO]:
        out = io.StringIO()
        return (
            Instances(
                self.config,
                kinds=kinds,
                run=self.systemd.run,
                probe=self.probe,
                clock=self.clock,
                sleep=self.sleep,
                out=out,
                uid=UID,
            ),
            out,
        )

    def calls(self) -> list[list[str]]:
        return self.systemd.calls()

    def changes(self) -> list[list[str]]:
        return self.systemd.changes()

    def units(self) -> dict[str, dict]:
        return self.systemd.units()

    def lease(self, name: str) -> dict | None:
        path = self.state / "leases" / f"{name}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def directory(self, name: str) -> Path:
        return self.instances_root / name
