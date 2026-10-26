# ch-microvm

On-demand instances of [microvm.nix](https://github.com/microvm-nix/microvm.nix)
guests, run by an ordinary user as units of that user's systemd
manager. A home declares kinds of guests, each with the runner of a
microvm.nix guest under cloud-hypervisor. An instance of a kind is
leased to a holder process, runs in a directory of its own, and stops
when its holder process exits.

The flake publishes three things:

- **The package `ch-microvm`**, under `pkgs.ch-microvm` in a package set
  that applies the package overlay `ch-microvm/default`. It is the
  command `ch-microvm` and its Python library `ch_microvm`. A Python
  program built on the library lists the package in its
  `dependencies`.
- **The home-manager module `microvm`**, which a consumer's registry
  holds as `ch-microvm/microvm`. It declares
  `ch-microvm.kinds.<kind>`, installs the command with the kinds baked
  in, and runs a user timer that stops the instances whose holder
  process is gone. Its read-only options `ch-microvm.configFile` and
  `ch-microvm.instances` give other modules the configuration the
  command reads and the instances of each kind with their share
  directories.
- **The NixOS test `nestedVmTests.x86_64-linux.microvm-user-vm`**, in
  which an ordinary user starts a real guest.

## Use it

```nix
{
  inputs.ch-microvm.url = "github:clhodapp/ch-microvm";

  # A caisson flake lists it in `projects`; its package sets then apply
  # `ch-microvm/default`, and its registry holds `ch-microvm/microvm`.
  # In a home that selects that module:
  #   ch-microvm.kinds.echo = {
  #     runner = nixosConfigurations.echo-guest.config.microvm.declaredRunner;
  #     shares = [ "out" ];
  #     vsockPort = 5000;
  #     maxInstances = 2;
  #   };
}
```

The user then runs:

```
ch-microvm up echo                      # lease echo-1 to this shell, start it, wait until it answers
ch-microvm up echo --directory ~/work   # run the instance in ~/work instead of its default directory
ch-microvm status                       # every instance, its holder and its state
ch-microvm status --json                # the same as data
ch-microvm down echo-1                  # stop the instance and drop its lease
```

The machine opens `/dev/kvm` to the user and enables unprivileged user
namespaces. The design, the directory rules, the units and the
findings about unprivileged virtiofsd are in
[`docs/development/instances.md`](docs/development/instances.md).

## Development

`nix flake check` builds the package, which runs its unittest suites,
evaluates homes with the module, and checks the formatting. `nix fmt`
formats. The nested VM test needs KVM and runs by name:
`nix build -L '.#nestedVmTests.x86_64-linux.microvm-user-vm'`.
[`docs/development/testing.md`](docs/development/testing.md) lists what
each check covers.

In CI the same `nix flake check` runs with the Nix store cached between
runs. A pull request that leaves `.github/` alone is checked by `main`'s
copy of the workflow, in `main`'s context once its own check completes,
and adds its build to the shared cache. A pull request that changes the
pipeline is checked by its own copy, under a cache only it can see. The
comments at the top of the two workflow files say why that split is
what makes the cache safe to write from a pull request.

## Binary cache

The pipeline pulls from the `clhodapp` cachix cache and its upstreams:

| Substituter | Public key |
|---|---|
| `https://clhodapp.cachix.org` | `clhodapp.cachix.org-1:EW/0conxH0OQyo0o4ub/grdkFspholmQMSnQyj0vrZI=` |
| `https://nix-community.cachix.org` | `nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs=` |
| `https://numtide.cachix.org` | `numtide.cachix.org-1:2ps1kLBUWjxIneOy1Ik6cQjb41X0iXVXeHigGmycPPE=` |

The flake's `nixConfig` declares all three, so a direct `nix build` or
`nix flake check` here uses them once accepted: answer Nix's prompt, or
pass `--accept-flake-config`. A flake that consumes this one as an input
adds them to its own `extra-substituters` and
`extra-trusted-public-keys`, since Nix does not carry the settings of
an input into the consumer.

## License

MIT, see [`LICENSE`](LICENSE).
