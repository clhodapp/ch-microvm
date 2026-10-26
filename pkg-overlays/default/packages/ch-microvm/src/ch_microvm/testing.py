# SPDX-License-Identifier: MIT
"""The fakes the unit tests of ch-microvm use, published so that a
program built on the library tests against the same fakes.

`FakeSystemd` installs a `systemctl --user` and a `systemd-run --user`
that keep the units of the user's manager in a file. `FakeProc` is a
/proc of stat files. `FakeVmm` is the hybrid vsock socket of a VMM.
`fake_runner` writes the files of a microvm.nix runner that the library
looks for. None of them runs a guest.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

# One program for both commands. It logs its name and arguments, and
# keeps the loaded units with the properties and command each was
# started with. A transient unit is unloaded when it stops, as with
# `systemd-run --collect`.
FAKE_SYSTEMD = """\
import json, os, sys, time
state_path, log_path = {state!r}, {log!r}
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(log_path, "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
try:
    state = json.load(open(state_path))
except FileNotFoundError:
    state = {{}}
units = state.setdefault("units", {{}})
fail = state.get("fail", {{}})
def save():
    json.dump(state, open(state_path, "w"))
assert args[:1] == ["--user"], args
args = args[1:]
if name == "systemd-run":
    split = args.index("--")
    options, argv = args[:split], args[split + 1:]
    unit = [o.split("=", 1)[1] for o in options if o.startswith("--unit=")][0]
    if unit in units:
        print(f"Failed to start transient service unit: Unit {{unit}} was already loaded", file=sys.stderr)
        sys.exit(1)
    if "start" in fail.get(unit, []):
        print(f"Job for {{unit}} failed", file=sys.stderr)
        sys.exit(1)
    units[unit] = {{
        "since": int(time.time()) - 5,
        "options": options,
        "argv": argv,
    }}
    save()
    sys.exit(0)
if args[:1] == ["--no-block"]:
    args = args[1:]
verb = args[0]
if verb == "stop":
    code = 0
    for unit in args[1:]:
        if "stop" in fail.get(unit, []):
            print("Access denied", file=sys.stderr)
            sys.exit(4)
        if unit not in units:
            print(f"Failed to stop {{unit}}: Unit {{unit}} not loaded.", file=sys.stderr)
            code = 5
            continue
        del units[unit]
    save()
    sys.exit(code)
if verb == "is-active":
    on = args[1] in units
    print("active" if on else "inactive")
    sys.exit(0 if on else 3)
if verb == "show":
    unit = args[-1]
    if "--property=LoadState" in args:
        print("loaded" if unit in units else "not-found")
    else:
        since = units.get(unit, {{}}).get("since")
        print("@%d" % since if since is not None else "")
    sys.exit(0)
sys.exit(1)
"""


class FakeSystemd:
    """A `systemctl` and a `systemd-run` in `root/tools`, which keep the
    units in `root/systemd.json` and log every call to
    `root/systemd.log`. `run` runs a command as `subprocess.run` does,
    and after a started VMM unit calls each hook of `on_start` with the
    instance and its working directory."""

    def __init__(self, root: Path) -> None:
        self.state = root / "systemd.json"
        self.log = root / "systemd.log"
        tools = root / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        for name in ("systemctl", "systemd-run"):
            fake = tools / name
            fake.write_text(f"#!{sys.executable}\n" + FAKE_SYSTEMD.format(state=str(self.state), log=str(self.log)))
            fake.chmod(0o755)
        self.systemctl = str(tools / "systemctl")
        self.systemd_run = str(tools / "systemd-run")
        self.on_start: list[Callable[[str, Path], None]] = []

    def run(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        done = subprocess.run(argv, **kwargs)
        if done.returncode == 0 and Path(argv[0]).name == "systemd-run":
            unit = [a.split("=", 1)[1] for a in argv if a.startswith("--unit=")][0]
            if not unit.endswith("-virtiofsd.service"):
                instance = unit.removeprefix("microvm-").removesuffix(".service")
                prefix = "--property=WorkingDirectory="
                directory = [a.removeprefix(prefix) for a in argv if a.startswith(prefix)][0]
                for hook in self.on_start:
                    hook(instance, Path(directory))
        return done

    def set_state(self, state: dict) -> None:
        """Replace the units and the failures the fakes keep."""
        self.state.write_text(json.dumps(state))

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def changes(self) -> list[list[str]]:
        """The calls that start or stop units."""
        return [call for call in self.calls() if call[0] == "systemd-run" or "stop" in call]

    def units(self) -> dict[str, dict]:
        if not self.state.exists():
            return {}
        return json.loads(self.state.read_text()).get("units", {})


def stat_line(pid: int, start: str, comm: str = "holder") -> str:
    return f"{pid} ({comm}) S " + " ".join(["0"] * 18) + f" {start} 0 0\n"


class FakeProc:
    """A /proc at `root` with a stat file for each process it runs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def process(self, pid: int, start: str = "4242", comm: str = "holder") -> str:
        """Run process `pid` with the start time `start`, and return it."""
        (self.root / str(pid)).mkdir(exist_ok=True)
        (self.root / str(pid) / "stat").write_text(stat_line(pid, start, comm))
        return start

    def end(self, pid: int) -> None:
        (self.root / str(pid) / "stat").unlink()
        (self.root / str(pid)).rmdir()


class FakeVmm:
    """The hybrid vsock socket of a cloud-hypervisor VMM, listening at
    `path`. For each connection it reads one line, records it in
    `requests`, and sends what `answer` returns for the requested port,
    or closes the connection when `answer` returns None. After an
    answer that starts with `OK `, it hands the connection and the port
    to `accepted`, or closes the connection when `accepted` is None."""

    def __init__(
        self,
        path: Path,
        answer: Callable[[int], bytes | None] = lambda port: b"OK 1073741824\n",
        accepted: Callable[[socket.socket, int], None] | None = None,
    ) -> None:
        self.path = path
        self.answer = answer
        self.accepted = accepted
        self.requests: list[bytes] = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(8)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn: socket.socket) -> None:
        line = bytearray()
        while not line.endswith(b"\n"):
            byte = conn.recv(1)
            if not byte:
                conn.close()
                return
            line += byte
        self.requests.append(bytes(line))
        verb, _, port = line.decode().strip().partition(" ")
        reply = self.answer(int(port)) if verb == "CONNECT" and port.isdigit() else None
        if reply is None:
            conn.close()
            return
        if reply:
            conn.sendall(reply)
        if not reply.startswith(b"OK "):
            conn.close()
        elif self.accepted is not None:
            self.accepted(conn, int(port))
        else:
            conn.close()

    def close(self) -> None:
        try:
            self.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.listener.close()


def fake_runner(path: Path, *, virtiofsd: bool, notify: bool) -> Path:
    """A directory with the files of a microvm.nix runner that the
    library looks for. None of them runs."""
    (path / "bin").mkdir(parents=True)
    names = ["microvm-run"] + (["virtiofsd-run", "microvm-shutdown"] if virtiofsd else [])
    for name in names:
        (path / "bin" / name).write_text("#!/bin/false\n")
    if notify:
        (path / "share" / "microvm").mkdir(parents=True)
        (path / "share" / "microvm" / "vsock-cid").write_text("3\n")
    return path
