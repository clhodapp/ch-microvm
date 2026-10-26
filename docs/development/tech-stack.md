# Technology Stack

- **Nix** for the module, the package and the checks.
- **caisson** ([github:nix-caisson/caisson](https://github.com/nix-caisson/caisson))
  for `mkLib`, `caisson.flake-parts.mkTopConfiguration`, the module and
  package overlay registries, and the package sets. The package overlay
  `default` puts the packages under `pkgs.ch-microvm`.
- **flake-parts** for the flake outputs, with the `checks` and
  `formatter` partitions that take the inputs of `tests/dependencies`.
- **home-manager** for the module. The checks evaluate homes with it.
- **Python 3**, standard library only, for the command and the library.
  The package is built with `buildPythonPackage`, so it is both a
  command and a dependency of another Python package.
- **systemd** user units, started with `systemd-run --user` as
  transient units.
- **microvm.nix** runners under **cloud-hypervisor**, with **virtiofsd**
  for the shares. microvm.nix is an input of the VM test only.
