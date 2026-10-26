# SPDX-License-Identifier: MIT
{ ... }:
{
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
    # against this flake's main lock.
    nixpkgs.packages.export.enabled = true;
  };

  partitionedAttrs.checks = "checks";
  partitionedAttrs.formatter = "formatter";
  partitionedAttrs.nestedVmTests = "checks";

  partitions.formatter = {
    extraInputs = (lib.caisson.pins.flake-compat ../../../tests/dependencies).sources;
    module =
      { inputs, ... }:
      {
        imports = [ inputs.treefmt-nix.flakeModule ];
        perSystem.treefmt.programs.nixfmt.enable = true;
      };
  };

  partitions.checks = {
    extraInputs = (lib.caisson.pins.flake-compat ../../../tests/dependencies).sources;
    module =
      {
        inputs,
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
        # separate output and no check. A machine with KVM builds it by
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
                  self.modules.homeManager.microvm
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
          { pkgs, system, ... }:
          let
            # Homes of `tester` that select the module with the given
            # kinds.
            home =
              kinds:
              (inputs.home-manager.lib.homeManagerConfiguration {
                inherit pkgs;
                modules = [
                  self.modules.homeManager.microvm
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
            fullHome = home kinds;
            bareHome = home { };
            # Whether a home fails an assertion of the module.
            # home-manager refuses to evaluate a home whose assertions
            # fail.
            refused = extra: !(builtins.tryEval (home (kinds // extra)).home.packages).success;

            state = "/home/tester/.local/state/ch-microvm";
          in
          {
            checks = {
              # Building runs the unittest suites of the library and the
              # command against a fake `systemctl --user`, a fake
              # `systemd-run --user`, a fake /proc and fake VMMs.
              ch-microvm = self.packages.${system}.ch-microvm;

              # The module with kinds from its options. The home with
              # kinds gets the command with the kinds baked in and the
              # reap timer. The baked configuration carries the runner,
              # the shares, the limits and the state root of each kind,
              # and the systemctl, systemd-run and PATH of the units. The
              # record `instances` names the instances of each kind with
              # their directories and share directories. The home without
              # kinds gets none of it. A kind name outside the pattern
              # fails an assertion.
              home-module =
                assert
                  fullHome.ch-microvm.instances == {
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
                assert bareHome.ch-microvm.instances == { };
                assert !(bareHome.systemd.user.timers ? ch-microvm-reap);
                assert !(builtins.elem bareHome.ch-microvm.package bareHome.home.packages);
                # The control: a kind of another name evaluates.
                assert !refused { extra.runner = runner; };
                assert refused { "bad/name".runner = runner; };
                pkgs.runCommand "home-module"
                  {
                    nativeBuildInputs = [ pkgs.jq ];
                    files = fullHome.home-files;
                    command = lib.getExe fullHome.ch-microvm.package;
                    config = fullHome.ch-microvm.configFile;
                    installed = builtins.elem fullHome.ch-microvm.package fullHome.home.packages;
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
            treefmt.programs.nixfmt.enable = true;
          };
      };
  };

}
