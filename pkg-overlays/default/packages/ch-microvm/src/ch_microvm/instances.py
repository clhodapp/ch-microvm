# SPDX-License-Identifier: MIT
"""The instances of the configured kinds, their leases, and their units.

A kind is a guest that the user runs as many times at once as the kind
allows. Every kind has a runner: the runner package of a microvm.nix
guest under cloud-hypervisor. A running copy of the guest is an
instance, named `<kind>-<n>` with `n` from 1 to `maxInstances`. Each
instance runs in a directory of its own. Cloud-hypervisor creates its
sockets in that directory, and the virtiofs shares of the runner name
directories in it. The home-manager module writes the kinds into the
configuration file that `load` reads.

An instance is leased to a holder. The holder is a process, recorded as
its process ID and its start time (field 22 of /proc/<pid>/stat), since
a process ID alone can be reused. A lease also records the directory of
the instance, and a label of the holder for display. An instance is
free when it has no lease, or when the process of its lease is gone.

The leases live under `<stateDir>/leases/<instance>.json`. Every change
to them happens under an exclusive lock on `<stateDir>/lock`, so two
processes that claim at once never claim one instance twice.

An instance runs as two transient units of the user's systemd manager,
which `systemd-run --user` starts: `microvm-<instance>-virtiofsd`
runs the virtiofsd daemons of its shares, and `microvm-<instance>`
runs the VMM.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TextIO

from . import transport
from .transport import Endpoint

# The hybrid vsock socket a cloud-hypervisor runner of microvm.nix
# creates in its working directory.
VSOCK_SOCKET = "notify.vsock"


class Refused(Exception):
    """A request that is refused, with the message for the caller."""


@dataclass(frozen=True)
class Holder:
    """The process that holds a lease: its process ID, its start time,
    and a label that `status` shows. The label decides nothing."""

    pid: int
    start: str
    label: str | None = None


def load(path: str | os.PathLike) -> dict:
    """The configuration in the file at `path`, which the home-manager
    module writes."""
    return json.loads(Path(path).read_text())


def start_time(proc_root: Path, pid: int) -> str | None:
    """The start time of process `pid` (field 22 of its stat file), or
    None when no such process runs."""
    try:
        text = (proc_root / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The second field is the command name in parentheses, which may
    # itself hold spaces and parentheses, so the fields are counted from
    # the last closing parenthesis. The field after it is field 3.
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 1 :].split()
    if len(fields) < 20:
        return None
    return fields[19]


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def open_directory_nofollow(path: str) -> int:
    """Open the directory at the absolute `path` one component at a time
    from `/`, each without following a symbolic link, and return its
    descriptor. A link at any component raises an error, so a process
    that can write a directory above an instance directory cannot point
    it at a directory elsewhere."""
    if not os.path.isabs(path):
        raise ValueError(f"{path} is not an absolute path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in [p for p in path.split("/") if p]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def empty_directory(path: str) -> None:
    """Remove everything inside the directory `path` and keep `path`
    itself. A missing `path` stays missing. The guest can write symbolic
    links into a share, so no symbolic link is followed: a link is
    removed and its target is kept. Every component of `path` is opened
    without following a link, so a directory that was replaced by a link
    raises an error. The walk is relative to open directories, with the
    `shutil.rmtree` that resists symbolic link attacks."""
    try:
        fd = open_directory_nofollow(path)
    except FileNotFoundError:
        return
    try:
        with os.scandir(fd) as entries:
            names = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
        for name, is_dir in names:
            if is_dir:
                shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


def within(path: str, root: str) -> bool:
    """Whether `path` is `root` or lies under it, with symbolic links of
    both resolved."""
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


class Instances:
    """The instances of the configured kinds, with the leases and units
    that go with them. With `kinds`, only the instances of those kinds,
    so a program that serves some kinds to others refuses the rest."""

    def __init__(
        self,
        config: dict,
        *,
        kinds: Iterable[str] | None = None,
        proc_root: Path | None = None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        probe: Callable[[Endpoint, float], None] = transport.probe,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        out: TextIO | None = None,
        uid: int | None = None,
    ) -> None:
        every_kind: dict[str, dict] = config["kinds"]
        served = every_kind
        if kinds is not None:
            wanted = set(kinds)
            unknown = sorted(wanted - set(every_kind))
            if unknown:
                raise Refused(f"the configuration declares no kind {', '.join(unknown)}")
            served = {kind: info for kind, info in every_kind.items() if kind in wanted}
        self.kind_info = served
        self.state_dir = Path(config["stateDir"])
        # The directories whose contents `up` empties: the state roots
        # of every kind, and the state directory.
        self.owned_roots = sorted({info["stateRoot"] for info in every_kind.values()} | {str(self.state_dir)})
        self.systemctl = config["systemctl"]
        self.systemd_run = config["systemdRun"]
        self.unit_path = config.get("path")
        self.up_timeout = float(config.get("upTimeout", 120))
        self.proc_root = proc_root or Path(config.get("procRoot", "/proc"))
        self.uid = os.getuid() if uid is None else uid
        self.run = run
        self.probe = probe
        self.clock = clock
        self.sleep = sleep
        self.out = out
        self._captured = threading.local()
        self.instances: dict[str, str] = {}
        self.kinds: dict[str, list[str]] = {}
        for kind in sorted(served):
            names = [f"{kind}-{n}" for n in range(1, int(served[kind]["maxInstances"]) + 1)]
            self.kinds[kind] = names
            for name in names:
                self.instances[name] = kind

    def say(self, line: str = "") -> None:
        out = getattr(self._captured, "out", None) or self.out or sys.stdout
        print(line, file=out, flush=True)

    @contextlib.contextmanager
    def capture(self) -> Iterator[io.StringIO]:
        """Collect what this thread says while the context is open."""
        out = io.StringIO()
        self._captured.out = out
        try:
            yield out
        finally:
            self._captured.out = None

    def info(self, name: str) -> dict:
        return self.kind_info[self.instances[name]]

    def default_directory(self, name: str) -> str:
        return f"{self.info(name)['stateRoot']}/{name}"

    def holder_for(self, pid: int, label: str | None = None) -> Holder:
        """The holder for the running process `pid`, with its start time
        read now. A process that does not run is refused."""
        if pid <= 0:
            raise Refused(f"{pid} is not a process ID")
        start = start_time(self.proc_root, pid)
        if start is None:
            raise Refused(f"the holding process {pid} does not run")
        return Holder(pid=pid, start=start, label=label)

    # Leases.

    @property
    def lease_dir(self) -> Path:
        return self.state_dir / "leases"

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.lease_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_dir / "lock", "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def leases(self) -> dict[str, dict]:
        """The leases of the served instances, by instance. A file that
        is not a lease of a served instance is ignored."""
        found: dict[str, dict] = {}
        for name in self.instances:
            path = self.lease_dir / f"{name}.json"
            try:
                lease = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(lease, dict) and lease.get("instance") == name and isinstance(lease.get("directory"), str):
                found[name] = lease
        return found

    def write_lease(self, lease: dict) -> None:
        self.lease_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.lease_dir, prefix=".lease-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(lease, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.lease_dir / f"{lease['instance']}.json")
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def remove_lease(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.lease_dir / f"{name}.json").unlink()

    def alive(self, lease: dict) -> bool:
        """Whether the process that holds a lease still runs."""
        pid = lease.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        return start_time(self.proc_root, pid) == lease.get("start")

    @staticmethod
    def holds(holder: Holder | None, lease: dict) -> bool:
        """Whether `holder` holds a lease: it is the process of the lease,
        with the same start time. The label of a lease decides nothing."""
        return holder is not None and lease.get("pid") == holder.pid and lease.get("start") == holder.start

    def lease_for(self, name: str, holder: Holder, directory: str | None) -> dict:
        return {
            "instance": name,
            "kind": self.instances[name],
            "directory": directory if directory is not None else self.default_directory(name),
            "default": directory is None,
            "label": holder.label,
            "pid": holder.pid,
            "start": holder.start,
            "claimed": int(self.clock()),
        }

    # Units.

    @staticmethod
    def vm_unit(name: str) -> str:
        return f"microvm-{name}.service"

    @staticmethod
    def virtiofsd_unit(name: str) -> str:
        return f"microvm-{name}-virtiofsd.service"

    def systemctl_run(self, *args: str) -> subprocess.CompletedProcess:
        return self.run([self.systemctl, "--user", *args], capture_output=True, text=True, check=False)

    def loaded(self, unit: str) -> bool:
        """Whether the user's manager has `unit` loaded. A transient unit
        is unloaded once it stops, and systemctl fails to stop a unit
        that is not loaded."""
        done = self.systemctl_run("show", "--property=LoadState", "--value", unit)
        return (done.stdout or "").strip() == "loaded"

    def stop_units(self, name: str, *, no_block: bool = False) -> None:
        """Stop the units of `name` that are loaded."""
        units = [unit for unit in (self.vm_unit(name), self.virtiofsd_unit(name)) if self.loaded(unit)]
        if not units:
            return
        done = self.systemctl_run(*(["--no-block"] if no_block else []), "stop", *units)
        # A unit that stopped by itself after the check is unloaded, and
        # stopping it fails. The stop has then happened.
        if done.returncode != 0 and any(self.loaded(unit) for unit in units):
            detail = (done.stderr or done.stdout or "").strip()
            raise Refused(
                f"systemctl --user stop {' '.join(units)} failed (exit {done.returncode})"
                + (f": {detail}" if detail else "")
            )

    def start_unit(self, unit: str, properties: list[str], argv: list[str]) -> None:
        command = [
            self.systemd_run,
            "--user",
            "--collect",
            "--quiet",
            f"--unit={unit}",
            *[f"--property={prop}" for prop in properties],
            *([f"--setenv=PATH={self.unit_path}"] if self.unit_path else []),
            "--",
            *argv,
        ]
        done = self.run(command, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip()
            raise Refused(f"systemd-run of {unit} failed (exit {done.returncode})" + (f": {detail}" if detail else ""))

    def start_units(self, name: str, directory: str) -> None:
        """Start the units of `name` in `directory`: the virtiofsd unit
        when the runner has `bin/virtiofsd-run`, then the VMM unit."""
        info = self.info(name)
        runner = Path(info["runner"])
        timeout = f"TimeoutStartSec={int(self.up_timeout)}"
        vm_properties = [f"WorkingDirectory={directory}", timeout]
        virtiofsd = runner / "bin" / "virtiofsd-run"
        if virtiofsd.exists():
            unit = self.virtiofsd_unit(name)
            # supervisord, which virtiofsd-run runs, sets `user = root`
            # in its configuration and exits when it cannot become that
            # user. Its option --user, given the user's own ID, wins
            # over the configuration and asks for no change.
            self.start_unit(
                unit,
                ["Type=notify", "NotifyAccess=all", f"WorkingDirectory={directory}", timeout],
                [str(virtiofsd), f"--user={self.uid}"],
            )
            vm_properties += [
                f"BindsTo={unit}",
                f"After={unit}",
                f"ExecStopPost={self.systemctl} --user --no-block stop {unit}",
            ]
        # A runner whose guest has a vsock CID relays the readiness
        # notification of the guest to NOTIFY_SOCKET from a helper
        # process, so the unit is of Type=notify with NotifyAccess=all.
        if (runner / "share" / "microvm" / "vsock-cid").exists():
            vm_properties += ["Type=notify", "NotifyAccess=all"]
        else:
            vm_properties += ["Type=simple"]
        if info.get("runtimeMaxSec") is not None:
            vm_properties.append(f"RuntimeMaxSec={int(info['runtimeMaxSec'])}")
        shutdown = runner / "bin" / "microvm-shutdown"
        if shutdown.exists():
            vm_properties.append(f"ExecStop={shutdown}")
        self.start_unit(self.vm_unit(name), vm_properties, [str(runner / "bin" / "microvm-run")])

    def active(self, name: str) -> str:
        done = self.systemctl_run("is-active", self.vm_unit(name))
        return (done.stdout or "").strip() or "unknown"

    def uptime(self, name: str) -> float | None:
        done = self.systemctl_run(
            "show", "--timestamp=unix", "--property=ActiveEnterTimestamp", "--value", self.vm_unit(name)
        )
        value = (done.stdout or "").strip()
        if done.returncode != 0 or not value.startswith("@"):
            return None
        try:
            return self.clock() - float(value[1:])
        except ValueError:
            return None

    def endpoint(self, name: str, directory: str) -> Endpoint | None:
        """The vsock port of the guest of `name` running in `directory`,
        or None when its kind serves on no port."""
        port = self.info(name).get("vsockPort")
        if port is None:
            return None
        return Endpoint(instance=name, port=port, vsock_socket=f"{directory}/{VSOCK_SOCKET}")

    def answering(self, name: str, directory: str, timeout: float = 1.0) -> tuple[bool, str | None] | None:
        """Whether the guest of `name` answers on its vsock port, with
        the error of a refused permission, or None when its kind serves
        on no port. The user can act on a permission error, so it is
        kept."""
        endpoint = self.endpoint(name, directory)
        if endpoint is None:
            return None
        try:
            self.probe(endpoint, timeout)
        except PermissionError as error:
            return False, str(error)
        except OSError:
            return False, None
        return True, None

    # Directories.

    def check_directory(self, directory: str) -> str:
        """The directory a caller passed, normalized. It must be absolute
        and must lie outside the directories whose contents `up`
        empties."""
        if not os.path.isabs(directory):
            raise Refused(f"the directory {directory!r} is not an absolute path")
        if any(ord(c) < 32 for c in directory):
            raise Refused("the directory holds a control character")
        directory = os.path.normpath(directory)
        for root in self.owned_roots:
            if within(directory, root):
                raise Refused(
                    f"the directory {directory} lies in {root}, where ch-microvm keeps the directories "
                    "it empties; pass a directory outside it, or no directory"
                )
        return directory

    def prepare(self, name: str, lease: dict, *, claimed: bool) -> None:
        """Create the directory of `name` and its share directories. The
        default directory of an instance is emptied first when a new
        holder claims it. A share that is not a directory is refused,
        since virtiofsd would share the target of a symbolic link."""
        directory = lease["directory"]
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            if claimed and lease.get("default"):
                empty_directory(directory)
        except OSError as error:
            raise Refused(f"preparing the directory {directory} of {name} failed: {error}") from None
        for share in self.info(name).get("shares", []):
            path = os.path.join(directory, share)
            try:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(path, 0o700)
                mode = os.lstat(path).st_mode
            except OSError as error:
                raise Refused(f"creating the share {share} of {name} at {path} failed: {error}") from None
            if not stat.S_ISDIR(mode):
                raise Refused(
                    f"the share {share} of {name} at {path} is not a directory; "
                    "virtiofsd would share the target of a symbolic link"
                )

    # Verbs.

    def up(self, kind: str, holder: Holder, directory: str | None = None) -> str:
        """Lease an instance of `kind` to `holder`, start it, and wait
        until it answers. An instance the holder already holds is used
        again and keeps its files. A newly claimed instance in its
        default directory starts from an empty directory. A directory the
        caller passes is used as it is."""
        if kind not in self.kinds:
            raise Refused(f"unknown kind {kind!r}; the kinds are {', '.join(sorted(self.kinds)) or 'none'}")
        if directory is not None:
            directory = self.check_directory(directory)
        with self.locked():
            leases = self.leases()
            held = [name for name in self.kinds[kind] if name in leases and self.holds(holder, leases[name])]
            if held:
                name = held[0]
                lease = leases[name]
                claimed = False
                if directory is not None and os.path.realpath(directory) != os.path.realpath(lease["directory"]):
                    raise Refused(
                        f"the holder holds {name} with the directory {lease['directory']}; "
                        f"stop it with down {name} to use another directory"
                    )
                if holder.label is not None and lease.get("label") != holder.label:
                    lease = dict(lease, label=holder.label)
                    self.write_lease(lease)
            else:
                if directory is not None:
                    for other, other_lease in sorted(leases.items()):
                        if self.alive(other_lease) and within(directory, other_lease["directory"]):
                            raise Refused(f"the directory {directory} belongs to {other}, which a live holder holds")
                free = [name for name in self.kinds[kind] if name not in leases or not self.alive(leases[name])]
                if not free:
                    raise Refused(
                        f"every instance of the kind {kind} is held by another live holder "
                        f"({len(self.kinds[kind])} at most)"
                    )
                name = free[0]
                claimed = True
                lease = self.lease_for(name, holder, directory)
                self.write_lease(lease)

        try:
            # A newly claimed instance may still run for a holder that
            # is gone, so its units are stopped first. Its default
            # directory is emptied while they are stopped, so the new
            # holder gets none of the files of the holder before it.
            if claimed or self.active(name) != "active":
                self.stop_units(name)
                self.prepare(name, lease, claimed=claimed)
                self.start_units(name, lease["directory"])
        except Refused:
            if claimed:
                with contextlib.suppress(Refused):
                    self.stop_units(name)
                with self.locked():
                    current = self.leases().get(name)
                    if current is not None and self.holds(holder, current):
                        self.remove_lease(name)
            raise

        self.wait_answering(name, lease["directory"])
        self.describe(name, lease, emptied=claimed and bool(lease.get("default")))
        return name

    def wait_answering(self, name: str, directory: str) -> None:
        """Wait until the guest of `name` answers on its vsock port. A
        permission error ends the wait at once, since waiting does not
        change it. Every other error is tried again until `upTimeout`."""
        endpoint = self.endpoint(name, directory)
        if endpoint is None:
            return
        stays = f"it stays leased to the holder, so stop it with down {name}"
        deadline = self.clock() + self.up_timeout
        while True:
            try:
                self.probe(endpoint, 1.0)
                return
            except PermissionError as error:
                raise Refused(f"{name} was started but {error}; {stays}") from None
            except OSError as error:
                last = str(error)
            if self.clock() >= deadline:
                raise Refused(
                    f"{name} was started but its vsock port {endpoint.port} did not answer within "
                    f"{int(self.up_timeout)} seconds ({last}); {stays}"
                )
            self.sleep(0.5)

    def describe(self, name: str, lease: dict, *, emptied: bool) -> None:
        info = self.info(name)
        directory = lease["directory"]
        self.say(f"instance {name} (kind {self.instances[name]})")
        if emptied:
            self.say(f"directory {directory} (emptied for the new lease)")
        elif lease.get("default"):
            self.say(f"directory {directory}")
        else:
            self.say(f"directory {directory} (passed by the caller, kept as it was)")
        if info.get("vsockPort") is not None:
            self.say(f"vsock port {info['vsockPort']} through {directory}/{VSOCK_SOCKET}")
        for share in info.get("shares", []):
            self.say(f"share {share} {directory}/{share}")

    def every_instance(self) -> list[str]:
        return [name for kind in sorted(self.kinds) for name in self.kinds[kind]]

    def free(self, kind: str, leases: dict[str, dict]) -> list[str]:
        return [name for name in self.kinds[kind] if name not in leases or not self.alive(leases[name])]

    def status(self, holder: Holder | None, everything: bool) -> None:
        """Say the instances `holder` holds, or with `everything` every
        instance and its holder, and how many instances of each kind are
        free."""
        leases = self.leases()
        every = self.every_instance()
        if everything:
            shown = every
        else:
            shown = [name for name in every if name in leases and self.holds(holder, leases[name])]
            if not shown:
                self.say("the caller holds no instances")
        for name in shown:
            lease = leases.get(name)
            directory = lease["directory"] if lease is not None else self.default_directory(name)
            parts = [name, f"kind {self.instances[name]}", f"directory {directory}"]
            if everything:
                parts.append(self.holder_text(holder, lease))
            state = self.active(name)
            parts.append(state)
            if state == "active":
                answering = self.answering(name, directory)
                if answering is not None:
                    ok, detail = answering
                    parts.append("answering" if ok else "not answering" + (f": {detail}" if detail else ""))
                up_for = self.uptime(name)
                if up_for is not None:
                    parts.append(f"up {format_duration(up_for)}")
            self.say("  ".join(parts))
        self.say("kinds:")
        for kind in sorted(self.kinds):
            self.say(f"  {kind}: {len(self.free(kind, leases))} of {len(self.kinds[kind])} instances free")

    def holder_text(self, holder: Holder | None, lease: dict | None) -> str:
        if lease is None:
            return "free"
        if self.holds(holder, lease):
            return "held by the caller"
        pid = lease.get("pid")
        who = f"{lease['label']} (process {pid})" if lease.get("label") else f"process {pid}"
        if self.alive(lease):
            return f"held by {who}"
        return f"free (holder {who} is gone)"

    def records(self) -> dict[str, Any]:
        """Every instance with its lease and the state of its units, and
        the free instances of each kind, as data."""
        leases = self.leases()
        instances = []
        for name in self.every_instance():
            lease = leases.get(name)
            directory = lease["directory"] if lease is not None else self.default_directory(name)
            state = self.active(name)
            record: dict[str, Any] = {
                "instance": name,
                "kind": self.instances[name],
                "directory": directory,
                "defaultDirectory": lease["default"] if lease is not None else True,
                "holder": None,
                "unit": state,
                "answering": None,
                "uptime": None,
            }
            if lease is not None:
                record["holder"] = {
                    "pid": lease.get("pid"),
                    "start": lease.get("start"),
                    "label": lease.get("label"),
                    "alive": self.alive(lease),
                    "claimed": lease.get("claimed"),
                }
            if state == "active":
                answering = self.answering(name, directory)
                if answering is not None:
                    record["answering"] = answering[0]
                up_for = self.uptime(name)
                if up_for is not None:
                    record["uptime"] = int(up_for)
            instances.append(record)
        kinds = {
            kind: {"instances": len(self.kinds[kind]), "free": len(self.free(kind, leases))} for kind in sorted(self.kinds)
        }
        return {"instances": instances, "kinds": kinds}

    def down(self, holder: Holder, name: str | None = None, *, no_block: bool = False) -> None:
        """Stop one instance `holder` holds, or every instance it holds.
        An instance another live holder holds is refused. With
        `no_block`, systemctl queues each stop and returns at once."""
        if name is not None and name not in self.instances:
            raise Refused(f"unknown instance {name!r}; the instances are {', '.join(self.instances) or 'none'}")
        # Without leases there is nothing to stop and nothing to lock,
        # so the state directory is left as it is.
        if name is None and not self.leases():
            self.say("the caller holds no instances")
            return
        with self.locked():
            leases = self.leases()
            if name is not None:
                lease = leases.get(name)
                if lease is not None and not self.holds(holder, lease) and self.alive(lease):
                    raise Refused(f"{name} is held by another live holder; the caller may not stop it")
                targets = [name]
            else:
                targets = [target for target, lease in sorted(leases.items()) if self.holds(holder, lease)]
        if not targets:
            self.say("the caller holds no instances")
            return
        self.stop_all(targets, keep=lambda current: not self.holds(holder, current) and self.alive(current), no_block=no_block)

    def stop(self, name: str, *, no_block: bool = False) -> None:
        """Stop the instance `name` whatever holds it, and drop its
        lease."""
        if name not in self.instances:
            raise Refused(f"unknown instance {name!r}; the instances are {', '.join(self.instances) or 'none'}")
        self.stop_all([name], keep=lambda current: False, no_block=no_block)

    def stop_all(self, targets: list[str], *, keep: Callable[[dict], bool], no_block: bool) -> None:
        """Stop the units of each target and drop its lease, unless
        `keep` says that the lease found after the stop stays."""
        failures = []
        for target in targets:
            try:
                self.stop_units(target, no_block=no_block)
            except Refused as error:
                failures.append(str(error))
                continue
            with self.locked():
                current = self.leases().get(target)
                if current is not None and not keep(current):
                    self.remove_lease(target)
            self.say(f"stopped {target}")
        if failures:
            raise Refused("; ".join(failures))

    def reap(self) -> None:
        """Stop every leased instance whose holder is gone, and drop its
        lease."""
        if not self.leases():
            return
        with self.locked():
            dead = [name for name, lease in sorted(self.leases().items()) if not self.alive(lease)]
        failures = []
        for name in dead:
            try:
                self.stop_units(name)
            except Refused as error:
                failures.append(str(error))
                continue
            with self.locked():
                current = self.leases().get(name)
                # Another holder may have claimed the instance meanwhile.
                if current is not None and not self.alive(current):
                    self.remove_lease(name)
            self.say(f"reaped {name}")
        if failures:
            raise Refused("; ".join(failures))
