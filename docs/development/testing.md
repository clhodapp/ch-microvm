# ch-microvm Testing

`nix flake check` builds these checks:

- `checks.<system>.ch-microvm` is the package. Its build runs the
  unittest suites, against the fakes of `ch_microvm.testing`: a fake
  `systemctl --user`, a fake `systemd-run --user`, a fake `/proc` and
  fake VMMs on hybrid vsock sockets.
  - The instance suite leases instances up to the limit of a kind and
    refuses one more, uses a held instance again, and checks the units
    and their properties. It empties the default directory on a new
    claim and keeps it on reuse, keeps a directory the caller passes,
    refuses directories that `up` empties, and refuses a share that is
    a symbolic link. It stops the units of a failed start, waits for a
    guest through a fake VMM, and ends the wait at a permission error.
    It refuses to stop an instance another live holder holds, stops an
    instance whatever holds it, reaps the instances of holders that are
    gone, limits the instances to the served kinds, and checks the
    status as text and as data.
  - The command line suite runs `up`, `status`, `status --json` and
    `down`, `up` with `--directory` and `--holder`, the parent process
    as the holder without `--holder`, and the refusal of every argument
    outside the verbs.
  - The transport suite runs the hybrid vsock exchange against a fake
    VMM: an accepted connection, a refused `CONNECT`, an answer that is
    not `OK `, a missing socket, a socket nothing listens on, a socket
    the user may not open, a socket path too long for `connect(2)`, and
    a VMM that never answers.
- `checks.<system>.home-module` evaluates homes with the module and a
  fake runner. The home with kinds gets the command, the reap timer and
  its hardened service. The configuration file carries each kind as its
  options give it, and the command reads that file. The record
  `ch-microvm.instances` names each instance with its default directory
  and share directories. `ch-microvm status` and `status --json` list
  every instance. A home without kinds gets none of it, and a kind name
  outside the pattern fails an assertion.
- `checks.<system>.treefmt` checks the formatting.

## The nested VM test

`nestedVmTests.x86_64-linux.microvm-user-vm` is a NixOS test
(`tests/microvm-user-vm`). An ordinary user with a lingering systemd
manager starts a cloud-hypervisor guest of microvm.nix with
`ch-microvm up`. The guest runs `socat VSOCK-LISTEN:5000,fork EXEC:cat`
and writes a file into its `out` share at boot. The test checks the
properties of both units, that virtiofsd and the VMM run as the user, a
line through `notify.vsock`, and that the file the guest wrote belongs
to the user on the host. It starts a second instance in a directory the
user passes, with the same CID, and refuses a third. It stops the first
with `ch-microvm down` and lets `ch-microvm reap` stop the second after
its holder exits.

The guest runs inside the test VM, so it needs nested KVM. On a CI
runner, which is itself a VM, the guest is three levels deep, so the
test is no check and no workflow builds it. Build it by name on a
machine with KVM before merging a change to the library or the module:

```bash
nix build -L '.#nestedVmTests.x86_64-linux.microvm-user-vm'
```

It takes microvm.nix from `tests/dependencies`.
