# SPDX-License-Identifier: MIT
#
# A NixOS test of the home-manager module `microvm`: an ordinary user
# starts a cloud-hypervisor guest of microvm.nix with `ch-microvm up`,
# as units of that user's systemd manager. The test runs in a VM that
# runs the guest on nested KVM.
#
# `pkgs` is the package set of the checks. `microvm` is the microvm.nix
# flake. `mkCommand` takes the runner of the guest and returns the
# command `ch-microvm` of a home of `alice` with the kind `echo`, whose
# runner it is.
#
# The guest `echo` runs `socat VSOCK-LISTEN:5000,fork EXEC:cat`, so it
# answers on its vsock port 5000 with what it reads there. Its one
# virtiofs share has the relative source `out`, mounted at `/work/out`,
# and a service of the guest writes the file `hello` there at boot.
#
# The user `alice` has a lingering systemd manager, and `/dev/kvm` is
# open to every user. The test starts `echo-1` in its default directory
# for a holder process. It checks the properties of the two units, gets
# back a line through the hybrid vsock socket of the instance, and
# checks that the file the guest wrote belongs to `alice` on the host.
# It starts `echo-2` in a directory `alice` passes, beside `echo-1` and
# with the same CID, for a second holder, and talks to it the same way.
# A third holder finds no free instance. The test stops `echo-1` with
# `ch-microvm down`, ends the second holder, and lets `ch-microvm reap`
# stop `echo-2`. It checks that the units and processes of both are
# gone.
{
  pkgs,
  microvm,
  mkCommand,
}:
let
  lib = pkgs.lib;

  guest = import "${pkgs.path}/nixos/lib/eval-config.nix" {
    system = pkgs.stdenv.hostPlatform.system;
    modules = [
      microvm.nixosModules.microvm
      (
        { pkgs, ... }:
        {
          networking.hostName = "echo";
          system.stateVersion = "25.05";
          microvm = {
            hypervisor = "cloud-hypervisor";
            vcpu = 1;
            mem = 512;
            vsock.cid = 3;
            # The API socket, which `microvm-shutdown` uses.
            socket = "control.socket";
            # virtiofsd runs as the user, who may not give its sockets
            # the group `kvm` of the default.
            virtiofsd.group = null;
            shares = [
              {
                proto = "virtiofs";
                tag = "out";
                source = "out";
                mountPoint = "/work/out";
              }
            ];
          };
          systemd.services.echo = {
            wantedBy = [ "multi-user.target" ];
            serviceConfig.ExecStart = "${lib.getExe pkgs.socat} VSOCK-LISTEN:5000,fork EXEC:${pkgs.coreutils}/bin/cat";
          };
          systemd.services.write-out = {
            wantedBy = [ "multi-user.target" ];
            unitConfig.RequiresMountsFor = "/work/out";
            serviceConfig.Type = "oneshot";
            script = "echo from-guest > /work/out/hello";
          };
        }
      )
    ];
  };

  command = mkCommand guest.config.microvm.declaredRunner;

  # Connects to the hybrid vsock socket $1, asks for port 5000, and
  # prints the line the guest sends back for the line `hello`.
  ask = pkgs.writeShellScript "microvm-user-vm-ask" ''
    coproc guest { exec ${lib.getExe pkgs.socat} - UNIX-CONNECT:"$1"; }
    echo "CONNECT 5000" >&"''${guest[1]}"
    read -r -t 30 answer <&"''${guest[0]}" || exit 1
    case "$answer" in
      "OK "*) ;;
      *)
        echo "the socket answered: $answer" >&2
        exit 1
        ;;
    esac
    echo hello >&"''${guest[1]}"
    read -r -t 30 line <&"''${guest[0]}" || exit 1
    kill "$guest_PID"
    echo "$line"
  '';
in
pkgs.testers.runNixOSTest {
  name = "microvm-user-vm";

  # The test host runs on KVM, so its guest runs on nested KVM.
  qemu.forceAccel = true;

  nodes.host = {
    virtualisation.memorySize = 3072;
    virtualisation.cores = 2;
    # The virtualization extensions of the CPU of the machine that runs
    # the test, which cloud-hypervisor uses through `/dev/kvm`.
    virtualisation.qemu.options = [
      "-cpu"
      "host"
    ];
    boot.kernelModules = [
      "kvm-amd"
      "kvm-intel"
    ];
    services.udev.extraRules = ''
      KERNEL=="kvm", MODE="0666"
    '';

    users.users.alice = {
      isNormalUser = true;
      uid = 1000;
      linger = true;
    };

    environment.systemPackages = [
      command
      pkgs.socat
    ];
  };

  testScript = ''
    state = "/home/alice/.local/state/ch-microvm"
    first = f"{state}/echo-1"
    second = "/home/alice/work"

    def alice(command):
        return f"runuser -u alice -- env XDG_RUNTIME_DIR=/run/user/1000 {command}"

    def holder():
        return host.succeed(
            alice("sh -c 'setsid sleep 3600 > /dev/null 2>&1 < /dev/null & echo $!'")
        ).strip()

    def show(unit, *properties):
        args = " ".join(f"--property={p}" for p in properties)
        return host.succeed(alice(f"systemctl --user show {args} {unit}"))

    def logs():
        print(host.execute("journalctl --no-pager -n 200 _UID=1000")[1])

    host.wait_for_unit("multi-user.target")
    host.wait_for_unit("user@1000.service")
    kvm = host.succeed("stat -c %a /dev/kvm").strip()
    assert kvm == "666", f"/dev/kvm has the mode {kvm}"

    one = holder()
    try:
        print(host.succeed(alice(f"ch-microvm up echo --holder {one}"), timeout=900))
    except Exception:
        logs()
        raise

    vm = show(
        "microvm-echo-1.service",
        "Type", "NotifyAccess", "WorkingDirectory", "BindsTo", "After",
        "RuntimeMaxUSec", "ActiveState",
    )
    print(vm)
    for line in (
        "Type=notify", "NotifyAccess=all", f"WorkingDirectory={first}",
        "RuntimeMaxUSec=1h", "ActiveState=active",
    ):
        assert line in vm.splitlines(), f"{line} is not in {vm}"
    assert "microvm-echo-1-virtiofsd.service" in show("microvm-echo-1.service", "BindsTo")
    assert "microvm-shutdown" in show("microvm-echo-1.service", "ExecStop")
    fs = show("microvm-echo-1-virtiofsd.service", "Type", "NotifyAccess", "WorkingDirectory", "ActiveState")
    print(fs)
    for line in ("Type=notify", "NotifyAccess=all", f"WorkingDirectory={first}", "ActiveState=active"):
        assert line in fs.splitlines(), f"{line} is not in {fs}"

    # virtiofsd and the VMM run as alice.
    users = set(host.succeed("ps -o user= -C virtiofsd,cloud-hyperviso").split())
    assert users == {"alice"}, f"the processes run as {users}"

    host.succeed(alice(f"${ask} {first}/notify.vsock") + " | grep -qx hello")

    host.wait_until_succeeds(f"test -f {first}/out/hello", timeout=120)
    owner = host.succeed(f"stat -c '%U %G' {first}/out/hello").strip()
    print(f"the file the guest wrote belongs to {owner}")
    assert owner == "alice users", f"the file the guest wrote belongs to {owner}"
    assert host.succeed(f"cat {first}/out/hello").strip() == "from-guest"

    status = host.succeed(alice("ch-microvm status"))
    print(status)
    assert f"echo-1  kind echo  directory {first}  held by process {one}  active  answering" in status
    records = host.succeed(alice("ch-microvm status --json"))
    print(records)
    assert '"answering": true' in records

    # A second instance, in a directory alice passes, with the same CID.
    two = holder()
    host.succeed(alice(f"mkdir -p {second}"))
    host.succeed(alice(f"touch {second}/kept"))
    print(host.succeed(alice(f"ch-microvm up echo --directory {second} --holder {two}"), timeout=900))
    host.succeed(alice(f"${ask} {second}/notify.vsock") + " | grep -qx hello")
    host.succeed(f"test -f {second}/kept")
    host.wait_until_succeeds(f"test -f {second}/out/hello", timeout=120)
    # A third holder finds no free instance.
    host.fail(alice(f"ch-microvm up echo --holder {holder()}"))

    host.succeed(alice("ch-microvm down echo-1"), timeout=300)
    # The second holder ends, and reap stops its instance.
    host.succeed(f"kill {two}")
    host.wait_until_succeeds(f"test ! -e /proc/{two}", timeout=60)
    host.succeed(alice("ch-microvm reap"), timeout=300)
    host.succeed(alice("ch-microvm status") + " | grep -q 'echo: 2 of 2 instances free'")
    for unit in ("microvm-echo-1.service", "microvm-echo-1-virtiofsd.service", "microvm-echo-2.service"):
        host.fail(alice(f"systemctl --user is-active {unit}"))
    host.fail("pgrep -x virtiofsd")
    host.fail("pgrep cloud-hyperviso")
  '';
}
