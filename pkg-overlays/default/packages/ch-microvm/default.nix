# SPDX-License-Identifier: MIT
#
# The command `ch-microvm` and its Python library `ch_microvm`. The
# command leases on-demand instances of microvm.nix guests to holder
# processes, and starts and stops them as units of the user's systemd
# manager. It reads the kinds of guests from the file
# `CH_MICROVM_CONFIG` names. The home-manager module `microvm` writes
# that file from its options and wraps this package with it as the
# command of the home.
#
# The package is a Python package as well as a command. A Python
# program built on the library lists it in its `dependencies`, and the
# package set gives both the same Python.
{
  python3Packages,
  lib,
}:
python3Packages.buildPythonPackage {
  pname = "ch-microvm";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [ python3Packages.setuptools ];

  pythonImportsCheck = [ "ch_microvm" ];

  checkPhase = ''
    runHook preCheck
    python -m unittest discover -s tests -v
    runHook postCheck
  '';

  meta = {
    description = "Lease, start and stop on-demand instances of microvm.nix guests as units of the user's systemd manager";
    license = lib.licenses.mit;
    mainProgram = "ch-microvm";
  };
}
