# SPDX-License-Identifier: MIT
#
# The default package overlay: the packages of ch-microvm under
# `pkgs.ch-microvm` (packages/default.nix lists them). A consumer that
# lists this flake in `projects` holds it as `ch-microvm/default`, which
# its package sets apply by default. The scope name is bound here, so the
# packages land under `pkgs.ch-microvm` in any consumer.
{ closure-lib, ... }:
{
  overlay = closure-lib.caisson.nixpkgs.mkPackagesOverlay ./packages "ch-microvm";
}
