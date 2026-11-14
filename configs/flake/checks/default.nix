# SPDX-License-Identifier: MIT
#
# The checks of ch-microvm: a flake-parts configuration declared inside
# the flake-parts caisson top (configs/flake/ch-microvm), whose
# dependencies are those of the caisson top with the pins of
# tests/dependencies beside them. The caisson top gathers the checks
# declared here into its `checks` output, and publishes the
# `nestedVmTests` output of this configuration as its own. A flake that
# lists ch-microvm as an input reads nothing of this configuration, so
# it locks none of the pins of tests/dependencies.
#
# `self` is the flake of this configuration, and `self.outPath` is the
# source tree of ch-microvm. The home-manager module under test is read
# from the module registry of the composition.
{ ... }:
{
  inputs,
  lib,
  self,
  withSystem,
  ...
}:
{
  imports = [ inputs.treefmt-nix.flakeModule ];

  # An ordinary user starts a cloud-hypervisor guest of
  # microvm.nix with `ch-microvm up`, as units of that user's
  # systemd manager (tests/microvm-user-vm). The guest runs inside
  # the test VM, so it needs nested KVM. On a CI runner, which is
  # itself a VM, the guest is three levels deep, so the test is a
  # separate output and no check. A computer with KVM builds it by
  # name.
  flake.nestedVmTests.x86_64-linux.microvm-user-vm = withSystem "x86_64-linux" (
    { pkgs, ... }:
    import (self.outPath + "/tests/microvm-user-vm") {
      inherit pkgs;
      inherit (inputs) microvm;
      mkCommand =
        runner:
        (inputs.home-manager.lib.homeManagerConfiguration {
          inherit pkgs;
          modules = [
            lib.caisson.modules.homeManager.microvm
            {
              home.username = "alice";
              home.homeDirectory = "/home/alice";
              home.stateVersion = "25.05";
              ch-microvm = {
                upTimeout = 600;
                kinds.echo = {
                  inherit runner;
                  shares = [ "out" ];
                  vsockPort = 5000;
                  maxInstances = 2;
                  runtimeMaxSec = 3600;
                };
              };
            }
          ];
        }).config.ch-microvm.package;
    }
  );

  perSystem =
    { pkgs, ... }:
    let
      # The `config` of a home-manager configuration of `tester`
      # that selects the module with the given kinds.
      testerConfig =
        kinds:
        (inputs.home-manager.lib.homeManagerConfiguration {
          inherit pkgs;
          modules = [
            lib.caisson.modules.homeManager.microvm
            {
              home.username = "tester";
              home.homeDirectory = "/home/tester";
              home.stateVersion = "25.05";
              ch-microvm.kinds = kinds;
            }
          ];
        }).config;

      # A package with the files of a microvm.nix runner that the
      # command looks for. No check runs it.
      runner = pkgs.runCommand "microvm-fake-runner" { } ''
        mkdir -p $out/bin $out/share/microvm
        for name in microvm-run virtiofsd-run microvm-shutdown; do
          printf '#!/bin/sh\nexit 1\n' > $out/bin/$name
          chmod +x $out/bin/$name
        done
        echo 3 > $out/share/microvm/vsock-cid
      '';

      # Two instances of `probe`, which has shares and a vsock
      # port, and one of `quiet`, which has neither.
      kinds = {
        probe = {
          inherit runner;
          shares = [
            "in"
            "out"
          ];
          vsockPort = 5000;
          maxInstances = 2;
          runtimeMaxSec = 3600;
        };
        quiet.runner = runner;
      };
      fullConfig = testerConfig kinds;
      bareConfig = testerConfig { };
      # Whether a home-manager configuration fails an assertion of
      # the module. home-manager refuses to evaluate a
      # configuration whose assertions fail.
      refused = extra: !(builtins.tryEval (testerConfig (kinds // extra)).home.packages).success;

      state = "/home/tester/.local/state/ch-microvm";
    in
    {
      # Duplicated in the formatter partition of configs/flake/ch-microvm:
      # the partition and this configuration are separate evaluations.
      treefmt.programs.nixfmt.enable = true;

      caisson.checks = {
        treefmt = {
          category = "format";
          description = "the Nix files are formatted with nixfmt";
        };

        # The home-manager configuration with kinds gets the command
        # with the kinds baked in and the reap timer. The baked
        # configuration carries the runner, the shares, the limits and
        # the state root of each kind, and the systemctl, systemd-run
        # and PATH of the units. The record `instances` names the
        # instances of each kind with their directories and share
        # directories. The home-manager configuration without kinds gets
        # none of it. A kind name outside the pattern fails an
        # assertion.
        home-manager-module = {
          category = "build";
          description = "the home-manager module turns its kinds into the command, its configuration file, the instance record and the reap timer, and refuses a kind name outside the pattern";
          derivation =
            assert
              fullConfig.ch-microvm.instances == {
                probe = [
                  {
                    name = "probe-1";
                    directory = "${state}/probe-1";
                    shareDirectories = [
                      "${state}/probe-1/in"
                      "${state}/probe-1/out"
                    ];
                  }
                  {
                    name = "probe-2";
                    directory = "${state}/probe-2";
                    shareDirectories = [
                      "${state}/probe-2/in"
                      "${state}/probe-2/out"
                    ];
                  }
                ];
                quiet = [
                  {
                    name = "quiet-1";
                    directory = "${state}/quiet-1";
                    shareDirectories = [ ];
                  }
                ];
              };
            assert bareConfig.ch-microvm.instances == { };
            assert !(bareConfig.systemd.user.timers ? ch-microvm-reap);
            assert !(builtins.elem bareConfig.ch-microvm.package bareConfig.home.packages);
            # The control: a kind of another name evaluates.
            assert !refused { extra.runner = runner; };
            assert refused { "bad/name".runner = runner; };
            pkgs.runCommand "home-manager-module"
              {
                nativeBuildInputs = [ pkgs.jq ];
                files = fullConfig.home-files;
                command = lib.getExe fullConfig.ch-microvm.package;
                config = fullConfig.ch-microvm.configFile;
                installed = builtins.elem fullConfig.ch-microvm.package fullConfig.home.packages;
                inherit runner state;
              }
              ''
                test "$installed" = 1
                units=$files/.config/systemd/user

                # The reap timer and its service, with the access the
                # module gives it.
                grep -qx 'OnCalendar=minutely' "$units/ch-microvm-reap.timer"
                service=$units/ch-microvm-reap.service
                grep -qx "ExecStart=$command reap" "$service"
                for line in PrivateUsers=true ProtectSystem=strict ProtectHome=read-only \
                  "ReadWritePaths=$state" "RestrictAddressFamilies=AF_UNIX" NoNewPrivileges=true; do
                  grep -qx "$line" "$service"
                done

                # The command reads the configuration file of the
                # option, which carries each kind as its options give
                # it, and the commands and the PATH of the units.
                grep -aqF "$config" "$command"
                jq -e --arg r "$runner" --arg s "$state" '.kinds.probe == {
                  runner: $r, shares: ["in", "out"], vsockPort: 5000, maxInstances: 2,
                  runtimeMaxSec: 3600, stateRoot: $s
                }' "$config"
                jq -e --arg r "$runner" --arg s "$state" '.kinds.quiet == {
                  runner: $r, shares: [], vsockPort: null, maxInstances: 1,
                  runtimeMaxSec: null, stateRoot: $s
                }' "$config"
                jq -e --arg s "$state" '.stateDir == $s and .upTimeout == 120' "$config"
                jq -e '.systemctl | endswith("/bin/systemctl")' "$config"
                jq -e '.systemdRun | endswith("/bin/systemd-run")' "$config"
                jq -e '.path | split(":") | length == 2' "$config"

                # The command has every kind baked in. No systemd runs
                # here, so the unit state is unknown.
                "$command" status > status.txt
                grep -qx "probe-1  kind probe  directory $state/probe-1  free  unknown" status.txt
                grep -qx "probe-2  kind probe  directory $state/probe-2  free  unknown" status.txt
                grep -qx "quiet-1  kind quiet  directory $state/quiet-1  free  unknown" status.txt
                grep -qx '  probe: 2 of 2 instances free' status.txt
                "$command" status --json > status.json
                jq -e '.instances | map(.instance) == ["probe-1", "probe-2", "quiet-1"]' status.json
                jq -e '.kinds.probe == {instances: 2, free: 2}' status.json
                rc=0
                "$command" up nope 2> err.txt || rc=$?
                test "$rc" = 2
                grep -q "^ch-microvm: unknown kind 'nope'" err.txt
                touch $out
              '';
        };
      };
    };
}
