# Instances of microvm.nix guests

The home-manager module `microvm` lets the user of a home run
microvm.nix guests on demand. A holder process leases an instance of a
guest, starts it, uses it, and stops it. The instance runs as units of
the user's systemd manager, in a directory the caller chooses or in a
default directory. The module knows the runner of a guest and nothing
of what runs in it.

## What the machine provides

- `/dev/kvm` is open to the user. cloud-hypervisor opens it as the user.
- Unprivileged user namespaces are enabled. virtiofsd runs as the user
  and puts itself in a user namespace of its own.
- The user's systemd manager runs while an instance runs. A lingering
  user has it from boot.

## Kinds and instances

A kind is a guest that the user runs as many times at once as the kind
allows. A running copy of the guest is an instance, named `<kind>-<n>`,
with `n` from 1 to `maxInstances`. Every instance of a kind runs the
same runner.

The option `ch-microvm.kinds.<kind>` declares a kind:

- `runner`: the runner package of the guest, which is
  `config.microvm.declaredRunner` of a NixOS configuration with the
  microvm.nix guest module and `microvm.hypervisor = "cloud-hypervisor"`.
- `shares`: the directories in the directory of an instance that the
  virtiofs shares of the runner name as their sources. The default is
  the empty list. A share of the guest declares a relative `source`,
  such as `source = "out"`, and `shares` lists `"out"`.
- `vsockPort`: the vsock port the guest serves on, or null. `up` waits
  until a started guest accepts a connection on it.
- `maxInstances`: the number of instances that run at once. The
  default is 1.
- `runtimeMaxSec`: the `RuntimeMaxSec` of the unit of the VMM, or null.
- `stateRoot`: the directory of the default directories of the
  instances. The default is `${config.xdg.stateHome}/ch-microvm`.

### What the runner of a kind needs

microvm.nix resolves every relative path of a cloud-hypervisor runner
in the working directory of the runner: the API socket, the sockets of
the virtiofs shares, the hybrid vsock socket `notify.vsock`, relative
volume images, and a relative `source` of a share. `up` starts the
runner with the directory of the instance as its working directory, so
each instance has its own sockets and its own shares.

The guest declares `microvm.vsock.cid` when it serves `vsockPort`.
cloud-hypervisor gives each VMM process a vsock of its own, reached
through `notify.vsock`, so every instance of a kind uses the same CID.
With a CID, the runner relays the readiness notification of the guest
to the unit. A guest that sets `microvm.socket`, such as
`socket = "control.socket"`, gets `bin/microvm-shutdown`, which the
unit of the VMM runs to stop the guest through its power button.

## virtiofsd without privileges

microvm.nix starts virtiofsd as root in its own NixOS service. Run by
the user, it meets two settings of microvm.nix that assume root:

- `virtiofsd-run` runs supervisord, whose configuration sets
  `user = root`. supervisord exits when it cannot become that user.
  Its option `--user`, given the user's own ID, wins over the
  configuration and asks for no change. `up` starts `virtiofsd-run`
  with `--user=<uid>`.
- virtiofsd gives the socket of a share the group of
  `microvm.virtiofsd.group`, the group `kvm` by default. A user outside
  that group may not, and virtiofsd then exits at once. The guest sets
  `microvm.virtiofsd.group = null`, and virtiofsd keeps the group of the
  user.

The script of each share leaves out `--rlimit-nofile` when it does not
run as root. The files the guest writes into a share belong to the user
on the host.

## Directories

The directory of an instance is one of two kinds of directory:

- The default directory `<stateRoot>/<kind>-<n>`. When a new holder
  claims the instance, `up` empties the whole directory, with the units
  of the instance stopped. The new holder then finds none of the files
  of an earlier holder: no share contents, no stale sockets, no volume
  images and no logs. `up` empties the whole directory rather than the
  share directories alone, since the runner keeps state of the guest
  beside the shares. A holder that calls `up` again keeps the files of
  its directory.
- A directory the caller passes, as an absolute path. `up` creates it
  when it is missing and never empties it. The next holder that claims
  the instance without a directory gets the default directory, and the
  directory passed before stays as it was. `up` refuses a directory
  inside a state root or the state directory, since `up` empties what
  it finds there. It refuses a directory that lies in the directory of
  an instance a live holder holds. A holder cannot move its instance to
  another directory: it stops the instance first.

`up` then creates each directory of `shares` in the directory of the
instance. It refuses a share that exists and is not a directory, since
virtiofsd would share the target of a symbolic link.

The guest can write symbolic links into a share, so emptying a default
directory follows no symbolic link. It removes each link and keeps its
target. Every component of the path of the directory is opened without
following a link, so a default directory that a link replaced is not
emptied. The path of `stateRoot` therefore holds no symbolic link. When
preparing the directory fails, `up` releases the lease, as it does when
the start fails, and the error names the instance and the path.

The read-only option `ch-microvm.instances` lists the instances of each
kind with their default directories and the share directories in them.
A module that grants another program access to the share directories,
such as a sandbox of a tool that copies files into a share, reads the
paths there.

## Units

`up` starts an instance with `systemd-run --user --collect`, as two
transient units. Each runs with the directory of the instance as its
`WorkingDirectory`, and with a `PATH` of systemd and coreutils, since
the scripts of the runner call `id`, `rm` and `systemd-notify` through
`PATH`.

- `microvm-<kind>-<n>-virtiofsd.service` runs
  `<runner>/bin/virtiofsd-run --user=<uid>`, with `Type=notify` and
  `NotifyAccess=all`. `up` starts it when the runner has
  `bin/virtiofsd-run`, which microvm.nix builds for a guest with
  virtiofs shares. supervisord runs one virtiofsd per share and
  notifies the unit once all of them run.
- `microvm-<kind>-<n>.service` runs `<runner>/bin/microvm-run`. It has
  `BindsTo=` and `After=` on the virtiofsd unit, and an `ExecStopPost`
  that stops the virtiofsd unit when the VMM stops for any reason. With
  a vsock CID in the runner, it is of `Type=notify` with
  `NotifyAccess=all`: the runner relays the readiness notification of
  the guest from a helper process to `NOTIFY_SOCKET`. Without a CID it
  is of `Type=simple`. It has the `RuntimeMaxSec` of the kind when the
  kind sets one, and `ExecStop=<runner>/bin/microvm-shutdown` when the
  runner has that script. A guest that stops stays stopped until the
  next `up`.

Both units have the `TimeoutStartSec` of `upTimeout`. `systemd-run`
waits until a unit has started, so `up` returns from the start of the
VMM unit once the guest has booted. `--collect` unloads a unit once it
stops, also after a failure, so the next start of the instance can
create it again. A stop names only the units that are loaded, since
systemctl fails to stop a unit that is not loaded.

## Reaching a guest

`ch_microvm.transport.connect` reaches the vsock port of a guest. It
takes the instance, the port of its kind, and the socket
`<directory>/notify.vsock`. The wait of `up` goes through it, and so
does a program that talks to the service of a guest.

The VMM of the guest listens on that Unix socket on the host. `connect`
connects to it and sends `CONNECT <port>` and a newline. The VMM
answers `OK <host-port>` and a newline when the guest accepts the
connection. Any other answer, or the end of the stream, means the guest
refused it. After the answer, the stream is the connection to the port
of the guest. `connect` reads the answer one byte at a time, so nothing
the guest sends after it is lost. A socket path longer than
`connect(2)` takes is reached through `/proc/self/fd` and a descriptor
of its directory.

Connecting and the exchange each wait at most one second in the wait of
`up`. A failure names the instance and the cause:

- the socket does not exist, or nothing listens on it, when the VMM of
  the instance does not run;
- permission is denied on the socket;
- the VMM refuses `CONNECT`, with the answer it gave.

The wait of `up` tries again every half second until `upTimeout`, and
the message at the timeout carries the last failure. A permission error
ends the wait at once, since waiting does not change it. `status` shows
a permission error after `not answering`, and every other failure as
`not answering` alone.

## The command

`ch-microvm` is a Python program with no dependencies outside the
standard library. The package `pkgs.ch-microvm.ch-microvm` is the
program alone. The module writes a configuration file with the kinds,
the state directory, the systemctl and systemd-run of `pkgs.systemd`,
the `PATH` of the units and `upTimeout`, and wraps the program with it
as the command of the home. `CH_MICROVM_CONFIG` names the file.

The verbs:

- `ch-microvm up <kind> [--directory DIR] [--holder PID]` leases an
  instance of the kind to the holder process, starts its units, waits
  until its vsock port accepts a connection, and prints the instance,
  its directory, its vsock socket and its share directories. The holder
  is the process `--holder` names, and the parent process of the
  command otherwise, which is the shell a user types the command in.
  The instance is the lowest `<kind>-<n>` that no live holder holds. An
  instance the holder already holds is used again. Its units start
  again when its guest stopped. A newly claimed instance has its units
  stopped first, so a guest left running by a holder that is gone
  carries none of its state into the new lease. A relative directory is
  taken relative to the working directory of the command.
- `ch-microvm down <instance>` stops the instance and drops its lease,
  whatever process holds it. The user who runs the command controls
  every instance.
- `ch-microvm status [--json]` shows every instance with its directory,
  its holder, the state of its VMM unit, whether it answers, and its
  uptime from the `ActiveEnterTimestamp` of the unit, and how many
  instances of each kind are free. With `--json` it prints the same as
  data.
- `ch-microvm reap` stops every leased instance whose holder process is
  gone, and drops its lease. The reap timer runs it.

The command accepts only these verbs, the kind and instance names of the
configuration, and the options of `up` and `status`. It refuses every
other argument before it reads any state.

## The library

A program that leases instances itself builds on the library
`ch_microvm`, which the package `ch-microvm` carries. It lists the
package in its `dependencies`. It reads the configuration file of the
home, `ch-microvm.configFile`, with `ch_microvm.instances.load`, so its
leases and units are the ones `ch-microvm status` shows and
`ch-microvm reap` stops.

- `Instances(config, kinds=...)` holds the instances of the configured
  kinds. With `kinds`, it serves the instances of those kinds and
  refuses the others.
- `Instances.holder_for(pid, label)` reads the start time of a running
  process and returns its `Holder`. The label is what `status` shows
  for the holder.
- `up(kind, holder, directory)`, `down(holder, instance)`,
  `stop(instance)`, `status(holder, everything)`, `records()` and
  `reap()` are the verbs. `down` stops only what the holder holds.
  `stop` stops an instance whatever holds it.
- `capture()` collects what the verbs print in the calling thread, for
  a program that returns the text instead of printing it.
- `endpoint(instance, directory)` and `transport.connect` reach the
  vsock port of a guest.
- `ch_microvm.testing` holds the fakes of the unit tests: a
  `systemctl` and a `systemd-run` that keep units in a file, a `/proc`
  of stat files, a VMM on a hybrid vsock socket, and the files of a
  runner. A program built on the library tests against them.

## Leases

A lease is the file `$XDG_STATE_HOME/ch-microvm/leases/<instance>.json`:

```json
{
  "instance": "echo-1",
  "kind": "echo",
  "directory": "/home/alice/.local/state/ch-microvm/echo-1",
  "default": true,
  "label": "<a label of the holder, or null>",
  "pid": 12345,
  "start": "<field 22 of /proc/12345/stat>",
  "claimed": 1793000000
}
```

The library writes a lease to a temporary file and renames it into
place. It changes leases only while it holds an exclusive lock on
`$XDG_STATE_HOME/ch-microvm/lock`, so two concurrent `up` calls never
lease one instance twice.

The holder of a lease is a process, recorded with its start time, since
a process ID alone can be reused. An instance is free when it has no
lease or when its holder process is gone. A caller holds a lease when
it names the holder process with its start time. The label decides
nothing.

## Stopping

Three mechanisms stop a guest, each covering what the one before
misses:

1. The holder stops its instances with `down`, or the user stops one
   with `ch-microvm down`.
2. The user timer `ch-microvm-reap.timer` runs `ch-microvm reap` every
   minute, which stops the instances whose holder process is gone.
3. The unit of the VMM has the `RuntimeMaxSec` of its kind, when the
   kind sets `runtimeMaxSec`.

## The reap service

The reap service runs with this `serviceConfig`:

- `PrivateUsers`, `ProtectSystem = "strict"` and
  `ProtectHome = "read-only"`, with `ReadWritePaths` set to the state
  directory. The command writes leases there and nowhere else. A
  systemd user tmpfiles rule creates the directory, since a missing
  `ReadWritePaths` entry keeps the service from starting.
- `RestrictAddressFamilies = "AF_UNIX"`. systemctl reaches the user's
  systemd manager over a Unix socket.
- `PrivateTmp`, `PrivateDevices`, the `ProtectKernel*`,
  `ProtectControlGroups`, `ProtectClock` and `ProtectHostname`
  settings, `RestrictNamespaces`, `RestrictRealtime`,
  `RestrictSUIDSGID`, `LockPersonality`,
  `SystemCallArchitectures = "native"` and `UMask = "0077"`.
- `ProtectProc` keeps its default, since the command reads
  `/proc/<pid>/stat` of the holders.

## Options

- `ch-microvm.kinds`: the kinds, described above.
- `ch-microvm.upTimeout`: the number of seconds `up` waits for a guest.
  It is the `TimeoutStartSec` of the units, and the time `up` then
  waits for the vsock port. The default is 120.
- `ch-microvm.programPackage`: the command before the module wraps it.
  The default is `pkgs.ch-microvm.ch-microvm`.
- `ch-microvm.package` (read-only): the command of the home with the
  kinds baked in. The home installs it when it has kinds.
- `ch-microvm.configFile` (read-only): the configuration file the
  command reads.
- `ch-microvm.instances` (read-only): the instances of each kind, each
  with its name, its default directory and its share directories.

## What a tree sets

A machine whose user runs guests:

- opens `/dev/kvm` to the user and enables unprivileged user
  namespaces;
- declares a NixOS configuration for each guest with the microvm.nix
  guest module, `microvm.hypervisor = "cloud-hypervisor"`,
  `microvm.virtiofsd.group = null`, virtiofs shares with relative
  sources, `microvm.vsock.cid` when the guest serves a vsock port, and
  `microvm.socket` for a clean shutdown;
- declares a home for the user that selects `ch-microvm/microvm` and
  sets `ch-microvm.kinds.<kind>` with the
  `config.microvm.declaredRunner` of the guest, its shares and its
  port.
