# SPDX-License-Identifier: MIT
#
# On-demand instances of microvm.nix guests that the user of this home
# runs. The option `kinds` declares them, each with the runner of a
# microvm.nix guest under cloud-hypervisor. An instance of a kind runs as
# two transient units of the user's systemd manager, in a directory of
# the instance. The module knows the runner of a guest and nothing of
# what runs in it.
#
# With kinds, the module installs the command `ch-microvm` with the
# kinds baked in, and a user timer that stops the instances whose holder
# process is gone. The read-only options `configFile` and `instances`
# give other modules the configuration the command reads, and the names,
# directories and share directories of the instances of each kind.
#
# docs/development/instances.md describes the whole mechanism.
{ ... }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ch-microvm;

  namePattern = "[A-Za-z0-9][A-Za-z0-9_.-]*";

  stateDir = "${config.xdg.stateHome}/ch-microvm";

  kindType = lib.types.submodule {
    options = {
      runner = lib.mkOption {
        type = lib.types.package;
        example = lib.literalExpression "nixosConfigurations.echo-guest.config.microvm.declaredRunner";
        description = ''
          The runner of the guest: `config.microvm.declaredRunner` of a
          NixOS configuration that imports the microvm.nix guest module
          and sets `microvm.hypervisor = "cloud-hypervisor"`. Every
          virtiofs share of the guest has a relative `source`, which
          names a directory in `shares`. A relative `socket` of a share,
          the default of microvm.nix, puts the socket in the directory
          of the instance. The guest sets
          `microvm.virtiofsd.group = null`, since virtiofsd runs as the
          user and exits when it cannot give its sockets the group of
          that option. The guest serves `vsockPort` on its vsock when
          `vsockPort` is set, and the guest then sets
          `microvm.vsock.cid`. Instances of a kind may share one CID,
          since each instance has a vsock of its own.
        '';
      };
      shares = lib.mkOption {
        type = lib.types.listOf (lib.types.strMatching namePattern);
        default = [ ];
        example = [
          "in"
          "out"
        ];
        description = ''
          The directories in the directory of an instance that the
          virtiofs shares of the runner name as their sources. `up`
          creates each of them before it starts the instance.
        '';
      };
      vsockPort = lib.mkOption {
        type = lib.types.nullOr lib.types.port;
        default = null;
        description = ''
          The vsock port the guest serves on. `up` waits until a started
          guest accepts a connection on it, through the hybrid vsock
          socket `notify.vsock` in the directory of the instance. With
          null, `up` returns once the units have started.
        '';
      };
      maxInstances = lib.mkOption {
        type = lib.types.ints.positive;
        default = 1;
        description = ''
          The number of instances of the kind that run at once. The
          instances are `<kind>-1` to `<kind>-<maxInstances>`.
        '';
      };
      runtimeMaxSec = lib.mkOption {
        type = lib.types.nullOr lib.types.ints.positive;
        default = null;
        description = ''
          The `RuntimeMaxSec` of the unit of the VMM of an instance, or
          null. systemd stops an instance that has run this many
          seconds.
        '';
      };
      stateRoot = lib.mkOption {
        type = lib.types.strMatching "/.+";
        default = stateDir;
        defaultText = lib.literalExpression ''"''${config.xdg.stateHome}/ch-microvm"'';
        description = ''
          The directory that holds the default directories of the
          instances: `<stateRoot>/<kind>-<n>`. `up` empties the default
          directory of an instance when a new holder claims it. Every
          component of the path is opened without following a symbolic
          link, so the path holds no symbolic link.
        '';
      };
    };
  };

  kinds = cfg.kinds;

  badKindNames = builtins.filter (name: builtins.match namePattern name == null) (
    builtins.attrNames kinds
  );

  # The instances of each kind, in the order of their numbers, each with
  # its default directory and the share directories in it.
  instances = builtins.mapAttrs (
    name: kind:
    map (
      n:
      let
        instance = "${name}-${toString n}";
        directory = "${kind.stateRoot}/${instance}";
      in
      {
        name = instance;
        inherit directory;
        shareDirectories = map (share: "${directory}/${share}") kind.shares;
      }
    ) (lib.range 1 kind.maxInstances)
  ) kinds;

  # What the command reads: the kinds, where the leases live, the
  # systemctl and systemd-run it runs, and the PATH of the units. The
  # scripts of a microvm.nix runner call `id`, `rm` and
  # `systemd-notify` through PATH.
  configFile = pkgs.writeText "ch-microvm-kinds.json" (
    builtins.toJSON {
      kinds = builtins.mapAttrs (_: kind: {
        runner = "${kind.runner}";
        inherit (kind)
          shares
          vsockPort
          maxInstances
          runtimeMaxSec
          stateRoot
          ;
      }) kinds;
      inherit stateDir;
      systemctl = lib.getExe' pkgs.systemd "systemctl";
      systemdRun = lib.getExe' pkgs.systemd "systemd-run";
      path = lib.makeBinPath [
        pkgs.systemd
        pkgs.coreutils
      ];
      inherit (cfg) upTimeout;
    }
  );

  # The command with the configuration baked in.
  command =
    pkgs.runCommand "ch-microvm"
      {
        nativeBuildInputs = [ pkgs.makeBinaryWrapper ];
        meta.mainProgram = "ch-microvm";
      }
      ''
        makeWrapper ${lib.getExe cfg.programPackage} "$out/bin/ch-microvm" \
          --set CH_MICROVM_CONFIG ${configFile}
      '';

  # The access of the reap service. It talks to the user's systemd
  # manager over its Unix socket, reads /proc/<pid>/stat of the holders
  # and writes its state directory.
  hardening = {
    NoNewPrivileges = true;
    PrivateUsers = true;
    ProtectSystem = "strict";
    ProtectHome = "read-only";
    ReadWritePaths = stateDir;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectKernelLogs = true;
    ProtectControlGroups = true;
    ProtectClock = true;
    ProtectHostname = true;
    RestrictNamespaces = true;
    RestrictRealtime = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    SystemCallArchitectures = "native";
    RestrictAddressFamilies = "AF_UNIX";
    UMask = "0077";
  };

  instanceType = lib.types.submodule {
    options = {
      name = lib.mkOption {
        type = lib.types.str;
        description = "The name of the instance, `<kind>-<n>`.";
      };
      directory = lib.mkOption {
        type = lib.types.str;
        description = "The default directory of the instance, `<stateRoot>/<kind>-<n>`.";
      };
      shareDirectories = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        description = ''
          The share directories in the default directory of the
          instance, in the order of `shares`.
        '';
      };
    };
  };
in
{
  options.ch-microvm = {
    kinds = lib.mkOption {
      type = lib.types.attrsOf kindType;
      default = { };
      example = lib.literalExpression ''
        {
          echo = {
            runner = nixosConfigurations.echo-guest.config.microvm.declaredRunner;
            shares = [ "in" "out" ];
            vsockPort = 5000;
            maxInstances = 2;
            runtimeMaxSec = 14400;
          };
        }
      '';
      description = ''
        The guest kinds this home runs, by name. The user of this home
        starts an instance of a kind with `ch-microvm up`, and a program
        built on the library `ch_microvm` starts one through that
        library. An instance runs as the transient units
        `microvm-<kind>-<n>-virtiofsd.service` and
        `microvm-<kind>-<n>.service` of the user's systemd manager. The
        machine needs `/dev/kvm` open to the user, and unprivileged user
        namespaces for virtiofsd.
      '';
    };

    programPackage = lib.mkOption {
      type = lib.types.package;
      default =
        pkgs.ch-microvm.ch-microvm or (throw ''
          ch-microvm.programPackage defaults to pkgs.ch-microvm.ch-microvm:
          apply the package overlay of ch-microvm to the package set this
          home evaluates with. A caisson consumer lists the flake in
          `projects`, and its package sets apply `ch-microvm/default`.
        '');
      defaultText = lib.literalExpression "pkgs.ch-microvm.ch-microvm";
      description = ''
        The command `ch-microvm` before the module bakes the kinds of
        this home into it.
      '';
    };

    upTimeout = lib.mkOption {
      type = lib.types.ints.positive;
      default = 120;
      description = ''
        The number of seconds `up` waits for a started guest. It is the
        `TimeoutStartSec` of the units of an instance, and the time `up`
        then waits for the guest to accept a connection on the vsock
        port of its kind.
      '';
    };

    package = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      default = command;
      defaultText = lib.literalMD "the command `ch-microvm` with the kinds of this home baked in";
      description = ''
        The command `ch-microvm` of this home, with the kinds of this
        home baked in. The home installs it when it has kinds.
      '';
    };

    configFile = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      default = configFile;
      defaultText = lib.literalMD "the configuration file the command of this home reads";
      description = ''
        The configuration file the command of this home reads: the
        kinds, the state directory, the systemctl and systemd-run the
        units start with, and the `PATH` of the units. A program built
        on the library `ch_microvm` reads it with
        `ch_microvm.instances.load`, so its leases and units are the
        ones `ch-microvm` shows and stops.
      '';
    };

    instances = lib.mkOption {
      type = lib.types.attrsOf (lib.types.listOf instanceType);
      readOnly = true;
      default = instances;
      defaultText = lib.literalMD "the instances of each kind of `kinds`";
      description = ''
        The instances of each kind, by kind, in the order of their
        numbers. Each has its name, its default directory and the share
        directories in that directory. A module that grants access to
        the share directories of a kind reads them here.
      '';
    };
  };

  config = lib.mkIf (kinds != { }) {
    assertions = [
      {
        assertion = badKindNames == [ ];
        message = "ch-microvm.kinds: the kind names ${lib.concatStringsSep ", " badKindNames} are not of letters, digits, `.`, `_` and `-`.";
      }
    ];

    home.packages = [ command ];

    # The command writes leases here, and the reap service sees the rest
    # of home read-only, so the directory exists before it runs.
    systemd.user.tmpfiles.rules = [ "d ${stateDir}/leases 0700 - - -" ];

    systemd.user.timers.ch-microvm-reap = {
      Unit.Description = "Stop the microvm instances whose holder process is gone";
      Timer = {
        OnCalendar = "minutely";
        AccuracySec = "10s";
      };
      Install.WantedBy = [ "timers.target" ];
    };

    systemd.user.services.ch-microvm-reap = {
      Unit.Description = "Stop the microvm instances whose holder process is gone";
      Service = hardening // {
        Type = "oneshot";
        ExecStart = "${lib.getExe command} reap";
      };
    };
  };
}
