# Product Definition

## Vision

`ch-microvm` runs microvm.nix guests on demand for an ordinary user.
A home declares kinds of guests. An instance of a kind is leased to a
holder process, runs as units of the user's systemd manager in a
directory of its own, and stops when its holder process exits.

## Goals

- Publish the home-manager module `microvm` with the options
  `ch-microvm.kinds.<kind>`, the command `ch-microvm` with the kinds
  baked in, and the reap timer.
- Publish the package `ch-microvm` under `pkgs.ch-microvm`, with the
  Python library `ch_microvm` that other programs build on.
- Give other modules the configuration file and the instances of each
  kind with their share directories, as read-only options.
- Run the guests unprivileged: cloud-hypervisor and virtiofsd run as the
  user.
- Keep one directory per instance, with the rules in
  [instances.md](instances.md) for emptying and keeping it.

## Scope

- The module knows the runner of a guest and nothing of what runs in it.
  A tree that runs a service in a guest declares the guest
  configuration itself.
- The guests run under cloud-hypervisor. The library reaches a guest
  through the hybrid vsock socket of its VMM.
- The leases scope instances to holder processes of one user. They keep
  holders from stopping or sharing the guests of each other by
  accident. They are no boundary between the processes of one user.
