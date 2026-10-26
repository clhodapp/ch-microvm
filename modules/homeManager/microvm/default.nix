# SPDX-License-Identifier: MIT
#
# The home-manager module `microvm` of ch-microvm. A consumer that lists
# ch-microvm in `projects` holds it as `ch-microvm/microvm`. The options
# it declares, under `ch-microvm`, are in ./ch-microvm.
{ mkModule, ... }:
{ ... }:
{
  imports = [
    (mkModule ./ch-microvm)
  ];
}
