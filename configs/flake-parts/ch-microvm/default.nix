# SPDX-License-Identifier: MIT
{ ... }:
{
  config,
  inputs,
  lib,
  ...
}:
{

  imports = [ inputs.flake-parts.flakeModules.partitions ];

  debug = false;
  caisson = {
    # The home-manager module of instances, which a consumer's registry
    # holds as `ch-microvm/microvm`.
    modules.homeManager.exported = modules: { inherit (modules) microvm; };

    # The command and its library are the `default` entry of this flake's
    # package overlay registry (pkg-overlays/, registered on mkLib), under
    # `pkgs.ch-microvm`. The package set applies it by default.
    # packages.* is not partitioned: consumers take these packages
    # against this flake's main lock. The flake-parts integration checks
    # the package `ch-microvm` as `ch-microvm-package`; building it runs
    # the unittest suites of the library and the command against a fake
    # `systemctl --user`, a fake `systemd-run --user`, a fake /proc and
    # fake VMMs.
    nixpkgs.packages.export.enabled = true;
  };

  # The checks of ch-microvm, with the pins of tests/dependencies that
  # only the checks need (configs/flake/checks). The caisson top gathers
  # the checks of every configuration declared inside it into its
  # `checks` output. This selector passes the configuration up to
  # nothing, so a flake that reads the exports of ch-microvm evaluates
  # neither the configuration nor its pins: the configuration is
  # evaluated when the `checks` output or the `nestedVmTests` output is
  # read.
  caisson.flake-parts.configurations.checks = lib.caisson.flake-parts.mkConfiguration {
    inherit (lib.caisson.pins.flake-compat ../../../tests/dependencies) dependencies;
  };
  caisson.flake-parts.exported = _: { };

  # The test that is not a check, which a computer with KVM builds by
  # name, from the outputs of the configuration that holds the checks.
  # It is read in the full evaluation of this caisson top, which holds
  # that configuration.
  flake = lib.mkIf (!lib.caisson-core.evalManifest.childless) {
    inherit (config.caisson.flake-parts.configurations.checks.outputs.flake) nestedVmTests;
  };

  partitionedAttrs.formatter = "formatter";

  partitions.formatter = {
    extraInputs = (lib.caisson.pins.flake-compat ../../../tests/dependencies).dependencies;
    module =
      { inputs, ... }:
      {
        imports = [ inputs.treefmt-nix.flakeModule ];
        perSystem.treefmt.programs.nixfmt.enable = true;
      };
  };

}
