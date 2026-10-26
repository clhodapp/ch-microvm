# SPDX-License-Identifier: MIT
#
# The `pkgs.ch-microvm.*` scope: the command `ch-microvm` with its
# Python library.
{ callPackage, ... }:
{
  ch-microvm = callPackage ./ch-microvm { };
}
