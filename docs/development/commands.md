# ch-microvm Development Commands

Run the commands from the root of the repository.

```bash
nix flake show
nix flake check
nix build '.#packages.x86_64-linux.ch-microvm'
nix build '.#checks.x86_64-linux.home-module'
nix build -L '.#nestedVmTests.x86_64-linux.microvm-user-vm'
nix fmt
```

The unit tests run without Nix as well:

```bash
cd pkg-overlays/default/packages/ch-microvm
python3 -m unittest discover -s tests -v
```
