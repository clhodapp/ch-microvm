# SPDX-License-Identifier: MIT
"""On-demand instances of microvm.nix guests, run by an ordinary user as units of that user's systemd manager.

`ch_microvm.instances` leases, starts and stops the instances.
`ch_microvm.transport` connects to the vsock port of a guest.
`ch_microvm.cli` is the command line `ch-microvm`.
`ch_microvm.testing` holds the fakes the unit tests use.
"""
