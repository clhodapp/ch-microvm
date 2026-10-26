# SPDX-License-Identifier: MIT
"""The instances against a fake `systemctl --user`, a fake
`systemd-run --user`, a fake /proc and a fake vsock probe: leasing the
lowest free instance up to the limit of the kind and refusing past it,
using a held instance again, the units and their properties, emptying
the default directory of a newly claimed instance and keeping it on
reuse, keeping a directory the caller passes, refusing a share that is
a symbolic link, waiting for a guest through its hybrid vsock socket,
the end of the wait at a permission error, refusing to stop an instance
another live holder holds, stopping an instance whatever holds it,
reaping the instances of holders that are gone, the served kinds, and
the status as text and as data."""

from __future__ import annotations

import re
import shutil
import unittest
from pathlib import Path

from fixture import PATH, UID, Fixture
from ch_microvm import transport
from ch_microvm.instances import Holder, Refused, start_time
from ch_microvm.testing import FakeVmm, stat_line
from ch_microvm.transport import Endpoint


class InstancesCase(Fixture):
    def test_start_time_reads_field_22_past_a_command_with_parentheses(self) -> None:
        (self.proc.root / "7").mkdir()
        (self.proc.root / "7" / "stat").write_text(stat_line(7, "999", comm="a) (b c"))
        self.assertEqual(start_time(self.proc.root, 7), "999")
        self.assertIsNone(start_time(self.proc.root, 8))

    def test_the_holder_of_a_running_process(self) -> None:
        instances, _ = self.instances()
        self.proc.process(1000, "77")
        self.assertEqual(instances.holder_for(1000, "a label"), Holder(pid=1000, start="77", label="a label"))
        with self.assertRaisesRegex(Refused, "the holding process 2000 does not run"):
            instances.holder_for(2000)
        with self.assertRaisesRegex(Refused, "0 is not a process ID"):
            instances.holder_for(0)

    def test_instances_come_from_the_kinds(self) -> None:
        instances, _ = self.instances()
        self.assertEqual(instances.kinds["probe"], ["probe-1", "probe-2"])
        self.assertEqual(instances.kinds["quiet"], ["quiet-1"])
        self.assertEqual(instances.default_directory("probe-2"), f"{self.instances_root}/probe-2")

    def test_served_kinds_limit_the_instances(self) -> None:
        served, _ = self.instances(kinds=["probe", "quiet"])
        self.assertEqual(sorted(served.kinds), ["probe", "quiet"])
        self.assertNotIn("other-1", served.instances)
        with self.assertRaisesRegex(Refused, "unknown kind 'other'"):
            served.up("other", self.process(1000))
        with self.assertRaisesRegex(Refused, "unknown instance 'other-1'"):
            served.down(self.process(1000), "other-1")
        # The state roots of every kind stay refused as directories.
        with self.assertRaisesRegex(Refused, "lies in"):
            served.up("quiet", self.process(1000), str(self.directory("other-1")))
        with self.assertRaisesRegex(Refused, "declares no kind nope"):
            self.instances(kinds=["probe", "nope"])

    def test_up_claims_the_lowest_free_instance_and_records_the_holder(self) -> None:
        holder = self.process(1000)
        instances, out = self.instances()
        self.assertEqual(instances.up("probe", holder), "probe-1")
        lease = self.lease("probe-1")
        directory = str(self.directory("probe-1"))
        self.assertEqual(
            {k: lease[k] for k in ("instance", "kind", "directory", "default", "label", "pid", "start")},
            {
                "instance": "probe-1",
                "kind": "probe",
                "directory": directory,
                "default": True,
                "label": "label-1000",
                "pid": 1000,
                "start": "4242",
            },
        )
        text = out.getvalue()
        self.assertIn("instance probe-1 (kind probe)", text)
        self.assertIn(f"directory {directory} (emptied for the new lease)", text)
        self.assertIn(f"vsock port 5000 through {directory}/notify.vsock", text)
        self.assertIn(f"share in {directory}/in", text)
        self.assertIn(f"share out {directory}/out", text)
        for share in ("in", "out"):
            self.assertTrue((self.directory("probe-1") / share).is_dir())
        self.assertEqual(sorted(p.name for p in (self.state / "leases").iterdir()), ["probe-1.json"])

    def test_the_units_and_their_properties(self) -> None:
        instances, _ = self.instances()
        instances.up("probe", self.process(1000))
        directory = str(self.directory("probe-1"))
        virtiofsd = "microvm-probe-1-virtiofsd.service"
        vm = "microvm-probe-1.service"
        common = ["--user", "--collect", "--quiet"]
        env = [f"--setenv=PATH={PATH}", "--"]
        self.assertEqual(
            [call for call in self.calls() if call[0] == "systemd-run"],
            [
                [
                    "systemd-run",
                    *common,
                    f"--unit={virtiofsd}",
                    "--property=Type=notify",
                    "--property=NotifyAccess=all",
                    f"--property=WorkingDirectory={directory}",
                    "--property=TimeoutStartSec=10",
                    *env,
                    f"{self.runner}/bin/virtiofsd-run",
                    f"--user={UID}",
                ],
                [
                    "systemd-run",
                    *common,
                    f"--unit={vm}",
                    f"--property=WorkingDirectory={directory}",
                    "--property=TimeoutStartSec=10",
                    f"--property=BindsTo={virtiofsd}",
                    f"--property=After={virtiofsd}",
                    f"--property=ExecStopPost={self.config['systemctl']} --user --no-block stop {virtiofsd}",
                    "--property=Type=notify",
                    "--property=NotifyAccess=all",
                    "--property=RuntimeMaxSec=3600",
                    f"--property=ExecStop={self.runner}/bin/microvm-shutdown",
                    *env,
                    f"{self.runner}/bin/microvm-run",
                ],
            ],
        )
        # Nothing was loaded before, so nothing was stopped.
        self.assertEqual([call for call in self.calls() if "stop" in call], [])
        self.assertEqual(sorted(self.units()), [virtiofsd, vm])

    def test_a_runner_without_shares_or_a_vsock_cid_gets_one_simple_unit(self) -> None:
        instances, _ = self.instances()
        instances.up("quiet", self.process(1000))
        runs = [call for call in self.calls() if call[0] == "systemd-run"]
        self.assertEqual(
            runs,
            [
                [
                    "systemd-run",
                    "--user",
                    "--collect",
                    "--quiet",
                    "--unit=microvm-quiet-1.service",
                    f"--property=WorkingDirectory={self.directory('quiet-1')}",
                    "--property=TimeoutStartSec=10",
                    "--property=Type=simple",
                    f"--setenv=PATH={PATH}",
                    "--",
                    f"{self.quiet_runner}/bin/microvm-run",
                ]
            ],
        )

    def test_up_uses_the_instance_the_holder_holds_again(self) -> None:
        holder = self.process(1000)
        instances, _ = self.instances()
        instances.up("probe", holder)
        before = self.changes()
        self.assertEqual(instances.up("probe", holder), "probe-1")
        self.assertEqual(self.changes(), before)

    def test_holders_get_their_own_instances_until_none_is_free(self) -> None:
        instances, _ = self.instances()
        self.assertEqual(instances.up("probe", self.process(1000)), "probe-1")
        self.assertEqual(instances.up("probe", self.process(2000)), "probe-2")
        with self.assertRaisesRegex(Refused, r"every instance of the kind probe is held .*\(2 at most\)"):
            instances.up("probe", self.process(3000))
        self.assertIsNone(self.lease("quiet-1"))
        self.assertEqual(sorted(p.name for p in self.instances_root.iterdir()), ["probe-1", "probe-2"])

    def test_the_lowest_instance_of_a_holder_that_is_gone_is_claimed_and_stopped_first(self) -> None:
        instances, _ = self.instances()
        instances.up("probe", self.process(1000))
        instances.up("probe", self.process(2000))
        self.end(1000)
        before = len(self.calls())
        self.assertEqual(instances.up("probe", self.process(3000)), "probe-1")
        self.assertEqual(self.lease("probe-1")["label"], "label-3000")
        changes = [call for call in self.calls()[before:] if call[0] == "systemd-run" or "stop" in call]
        self.assertEqual(
            changes[0], ["systemctl", "--user", "stop", "microvm-probe-1.service", "microvm-probe-1-virtiofsd.service"]
        )
        self.assertEqual(
            [call[4] for call in changes[1:]],
            ["--unit=microvm-probe-1-virtiofsd.service", "--unit=microvm-probe-1.service"],
        )

    def test_a_holder_whose_guest_stopped_gets_it_started_again_with_its_files(self) -> None:
        holder = self.process(1000)
        instances, _ = self.instances()
        instances.up("probe", holder)
        (self.directory("probe-1") / "out" / "result").write_text("kept")
        # The guest stops by itself, and its virtiofsd unit with it.
        self.systemd.set_state({"units": {}})
        self.assertEqual(instances.up("probe", holder), "probe-1")
        self.assertEqual((self.directory("probe-1") / "out" / "result").read_text(), "kept")
        self.assertIn("microvm-probe-1.service", self.units())

    def test_the_label_alone_holds_nothing(self) -> None:
        instances, _ = self.instances()
        instances.up("probe", self.process(1000))
        same_label = Holder(pid=2000, start="4242", label="label-1000")
        self.process(2000)
        with self.assertRaisesRegex(Refused, "probe-1 is held by another live holder"):
            instances.down(same_label, "probe-1")

    def test_down_refuses_an_instance_another_live_holder_holds(self) -> None:
        first, second = self.process(1000), self.process(2000)
        instances, out = self.instances()
        instances.up("probe", first)
        with self.assertRaisesRegex(Refused, "probe-1 is held by another live holder"):
            instances.down(second, "probe-1")
        self.assertIsNotNone(self.lease("probe-1"))
        instances.down(first, None)
        self.assertIsNone(self.lease("probe-1"))
        self.assertEqual(
            self.calls()[-1], ["systemctl", "--user", "stop", "microvm-probe-1.service", "microvm-probe-1-virtiofsd.service"]
        )
        self.assertEqual(self.units(), {})
        self.assertIn("stopped probe-1", out.getvalue())

    def test_down_without_blocking_queues_the_stop(self) -> None:
        holder = self.process(1000)
        instances, _ = self.instances()
        instances.up("probe", holder)
        instances.down(holder, None, no_block=True)
        self.assertEqual(
            self.calls()[-1],
            ["systemctl", "--user", "--no-block", "stop", "microvm-probe-1.service", "microvm-probe-1-virtiofsd.service"],
        )
        self.assertIsNone(self.lease("probe-1"))

    def test_down_of_an_instance_whose_units_are_gone(self) -> None:
        holder = self.process(1000)
        instances, out = self.instances()
        instances.up("quiet", holder)
        self.systemd.set_state({"units": {}})
        instances.down(holder, "quiet-1")
        self.assertIn("stopped quiet-1", out.getvalue())
        self.assertIsNone(self.lease("quiet-1"))

    def test_stop_stops_an_instance_whatever_holds_it(self) -> None:
        instances, out = self.instances()
        instances.up("probe", self.process(1000))
        instances.stop("probe-1")
        self.assertIsNone(self.lease("probe-1"))
        self.assertEqual(self.units(), {})
        self.assertIn("stopped probe-1", out.getvalue())
        # An instance without a lease or units stops as well.
        instances.stop("quiet-1")
        with self.assertRaisesRegex(Refused, "unknown instance 'probe-3'"):
            instances.stop("probe-3")

    def test_a_reused_process_id_is_not_the_holder(self) -> None:
        instances, _ = self.instances()
        instances.up("probe", self.process(1000, start="1"))
        # The process ends and another takes its ID.
        self.process(1000, start="2")
        self.assertFalse(instances.alive(self.lease("probe-1")))
        stranger = Holder(pid=1000, start="2")
        instances.down(stranger, "probe-1")
        self.assertIsNone(self.lease("probe-1"))

    def test_reap_stops_the_instances_of_holders_that_are_gone(self) -> None:
        instances, out = self.instances()
        instances.up("probe", self.process(1000))
        instances.up("probe", self.process(2000))
        self.end(1000)
        before = len(self.calls())
        instances.reap()
        self.assertIsNone(self.lease("probe-1"))
        self.assertIsNotNone(self.lease("probe-2"))
        stops = [call for call in self.calls()[before:] if "stop" in call]
        self.assertEqual(
            stops, [["systemctl", "--user", "stop", "microvm-probe-1.service", "microvm-probe-1-virtiofsd.service"]]
        )
        self.assertEqual(sorted(self.units()), ["microvm-probe-2-virtiofsd.service", "microvm-probe-2.service"])
        self.assertIn("reaped probe-1", out.getvalue())

    def test_a_failed_start_releases_the_claim_and_stops_the_units(self) -> None:
        self.systemd.set_state({"fail": {"microvm-probe-1.service": ["start"]}})
        instances, _ = self.instances()
        with self.assertRaisesRegex(Refused, "systemd-run of microvm-probe-1.service failed .*Job for"):
            instances.up("probe", self.process(1000))
        self.assertIsNone(self.lease("probe-1"))
        self.assertEqual(self.units(), {})

    # Directories.

    def fill(self, directory: Path) -> Path:
        """Fill `directory` and both of its shares with a file, a nested
        directory and a symbolic link to a directory outside, and return
        that outside directory."""
        outside = self.root / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "keep").write_text("kept")
        for sub in (directory, directory / "in", directory / "out"):
            (sub / "nested" / "deeper").mkdir(parents=True)
            (sub / "file").write_text("old")
            (sub / "nested" / "deeper" / "file").write_text("old")
            (sub / "link").symlink_to(outside, target_is_directory=True)
        return outside

    def test_rmtree_resists_symbolic_link_attacks(self) -> None:
        self.assertTrue(shutil.rmtree.avoids_symlink_attacks)

    def test_a_new_claim_empties_the_default_directory(self) -> None:
        outside = self.fill(self.directory("probe-1"))
        instances, _ = self.instances()
        self.assertEqual(instances.up("probe", self.process(1000)), "probe-1")
        self.assertEqual(sorted(p.name for p in self.directory("probe-1").iterdir()), ["in", "out"])
        for share in ("in", "out"):
            self.assertEqual(list((self.directory("probe-1") / share).iterdir()), [])
        self.assertEqual((outside / "keep").read_text(), "kept")

    def test_a_claim_of_an_instance_whose_holder_is_gone_empties_its_directory(self) -> None:
        instances, _ = self.instances()
        instances.up("probe", self.process(1000))
        outside = self.fill(self.directory("probe-1"))
        self.end(1000)
        self.assertEqual(instances.up("probe", self.process(2000)), "probe-1")
        self.assertEqual(list((self.directory("probe-1") / "out").iterdir()), [])
        self.assertFalse((self.directory("probe-1") / "file").exists())
        self.assertEqual((outside / "keep").read_text(), "kept")

    def test_the_holder_keeps_its_files(self) -> None:
        holder = self.process(1000)
        instances, _ = self.instances()
        instances.up("probe", holder)
        self.fill(self.directory("probe-1"))
        self.assertEqual(instances.up("probe", holder), "probe-1")
        self.assertEqual((self.directory("probe-1") / "out" / "file").read_text(), "old")
        self.assertEqual((self.directory("probe-1") / "nested" / "deeper" / "file").read_text(), "old")

    def test_a_directory_the_caller_passes_is_kept_as_it_was(self) -> None:
        work = self.root / "work"
        outside = self.fill(work)
        instances, out = self.instances()
        self.assertEqual(instances.up("probe", self.process(1000), str(work)), "probe-1")
        lease = self.lease("probe-1")
        self.assertEqual((lease["directory"], lease["default"]), (str(work), False))
        self.assertEqual((work / "file").read_text(), "old")
        self.assertEqual((work / "out" / "nested" / "deeper" / "file").read_text(), "old")
        self.assertEqual((outside / "keep").read_text(), "kept")
        self.assertFalse(self.directory("probe-1").exists())
        self.assertIn(f"--property=WorkingDirectory={work}", self.calls()[-1])
        self.assertIn(f"directory {work} (passed by the caller, kept as it was)", out.getvalue())
        # The next holder gets the default directory of the instance,
        # and the directory passed before stays as it was.
        self.end(1000)
        instances.up("probe", self.process(2000))
        self.assertEqual(self.lease("probe-1")["directory"], str(self.directory("probe-1")))
        self.assertEqual((work / "file").read_text(), "old")

    def test_a_missing_directory_is_created_with_its_shares(self) -> None:
        work = self.root / "new" / "work"
        instances, _ = self.instances()
        instances.up("probe", self.process(1000), str(work))
        self.assertEqual(sorted(p.name for p in work.iterdir()), ["in", "out"])

    def test_directories_that_are_refused(self) -> None:
        instances, _ = self.instances()
        holder = self.process(1000)
        for directory, message in (
            ("relative/dir", "is not an absolute path"),
            (str(self.instances_root), f"lies in {re.escape(str(self.instances_root))}"),
            (str(self.directory("probe-2")), "lies in"),
            (str(self.state / "x"), f"lies in {re.escape(str(self.state))}"),
            ("/tmp/a\nb", "control character"),
        ):
            with self.subTest(directory=directory):
                with self.assertRaisesRegex(Refused, message):
                    instances.up("probe", holder, directory)
        self.assertEqual(self.changes(), [])
        self.assertIsNone(self.lease("probe-1"))

    def test_a_directory_another_live_holder_uses_is_refused(self) -> None:
        work = self.root / "work"
        instances, _ = self.instances()
        instances.up("probe", self.process(1000), str(work))
        with self.assertRaisesRegex(Refused, "belongs to probe-1, which a live holder holds"):
            instances.up("probe", self.process(2000), str(work))
        with self.assertRaisesRegex(Refused, "belongs to probe-1"):
            instances.up("other", self.process(2000), str(work / "in"))

    def test_the_holder_may_not_move_its_instance(self) -> None:
        holder = self.process(1000)
        instances, _ = self.instances()
        instances.up("probe", holder)
        with self.assertRaisesRegex(Refused, "the holder holds probe-1 with the directory .*; stop it with down probe-1"):
            instances.up("probe", holder, str(self.root / "work"))
        self.assertEqual(instances.up("probe", holder), "probe-1")

    def test_a_share_that_is_a_symbolic_link_is_refused(self) -> None:
        work = self.root / "work"
        work.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("kept")
        (work / "out").symlink_to(outside, target_is_directory=True)
        instances, _ = self.instances()
        with self.assertRaisesRegex(Refused, "^the share out of probe-1 at .*/work/out is not a directory"):
            instances.up("probe", self.process(1000), str(work))
        self.assertIsNone(self.lease("probe-1"))
        self.assertEqual((outside / "keep").read_text(), "kept")
        self.assertEqual([call for call in self.calls() if call[0] == "systemd-run"], [])

    def test_a_default_directory_replaced_by_a_link_is_not_followed(self) -> None:
        # A link at the default directory would point the instance at a
        # directory elsewhere, so emptying it fails and the files there
        # stay.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep").write_text("kept")
        self.instances_root.mkdir()
        self.directory("probe-1").symlink_to(elsewhere, target_is_directory=True)
        instances, _ = self.instances()
        with self.assertRaisesRegex(
            Refused, f"^preparing the directory {re.escape(str(self.directory('probe-1')))} of probe-1 failed: "
        ):
            instances.up("probe", self.process(1000))
        self.assertIsNone(self.lease("probe-1"))
        self.assertEqual((elsewhere / "keep").read_text(), "kept")

    # Waiting.

    def test_a_guest_that_never_answers_stays_leased(self) -> None:
        self.answering = set()
        instances, _ = self.instances()
        with self.assertRaisesRegex(Refused, "did not answer within 10 seconds"):
            instances.up("probe", self.process(1000))
        self.assertIsNotNone(self.lease("probe-1"))

    def test_the_endpoint_is_the_socket_in_the_directory(self) -> None:
        instances, _ = self.instances()
        self.assertEqual(
            instances.endpoint("probe-1", "/w"), Endpoint(instance="probe-1", port=5000, vsock_socket="/w/notify.vsock")
        )
        self.assertIsNone(instances.endpoint("quiet-1", "/w"))

    def test_up_waits_for_the_guest_through_its_vsock_socket(self) -> None:
        # The VMM refuses CONNECT twice, as it does while the guest boots.
        answers = [None, b"FAILED\n", b"OK 1073741824\n"]
        vmms: list[FakeVmm] = []

        def started(instance: str, directory: Path) -> None:
            vmm = FakeVmm(directory / "notify.vsock", answer=lambda port: answers.pop(0))
            self.addCleanup(vmm.close)
            vmms.append(vmm)

        self.systemd.on_start.append(started)
        instances, _ = self.instances()
        instances.probe = transport.probe
        self.assertEqual(instances.up("probe", self.process(1000)), "probe-1")
        self.assertEqual(vmms[0].requests, [b"CONNECT 5000\n"] * 3)

    def test_a_permission_error_ends_the_wait_at_once(self) -> None:
        def denied(endpoint: Endpoint, timeout: float) -> None:
            raise PermissionError(f"permission denied on the vsock socket /s of {endpoint.instance}")

        instances, out = self.instances()
        instances.probe = denied
        started = self.now
        with self.assertRaisesRegex(
            Refused, "^probe-1 was started but permission denied on the vsock socket /s of probe-1; it stays leased"
        ):
            instances.up("probe", self.process(1000))
        self.assertEqual(self.now, started)
        self.assertIsNotNone(self.lease("probe-1"))
        instances.status(self.process(1000), everything=False)
        self.assertIn("active  not answering: permission denied", out.getvalue())

    # Status.

    def test_status_shows_the_instances_and_their_holders(self) -> None:
        instances, out = self.instances()
        holder = self.process(1000)
        instances.status(holder, everything=False)
        self.assertIn("the caller holds no instances", out.getvalue())
        instances.up("probe", holder)
        work = self.root / "work"
        instances.up("probe", self.process(2000), str(work))
        instances.up("quiet", Holder(pid=3000, start=self.proc.process(3000)))
        self.end(3000)
        out.truncate(0)
        out.seek(0)
        instances.status(holder, everything=False)
        lines = out.getvalue().splitlines()
        self.assertRegex(
            lines[0],
            rf"^probe-1  kind probe  directory {re.escape(str(self.directory('probe-1')))}  active  answering  up \d+s$",
        )
        self.assertIn("  probe: 0 of 2 instances free", lines)
        self.assertIn("  quiet: 1 of 1 instances free", lines)
        out.truncate(0)
        out.seek(0)
        instances.status(holder, everything=True)
        text = out.getvalue()
        self.assertIn("probe-1  kind probe  directory ", text)
        self.assertIn("  held by the caller  active", text)
        self.assertIn(f"probe-2  kind probe  directory {work}  held by label-2000 (process 2000)  active", text)
        self.assertIn(f"quiet-1  kind quiet  directory {self.directory('quiet-1')}  free (holder process 3000 is gone)", text)
        self.assertIn(f"other-1  kind other  directory {self.directory('other-1')}  free  inactive", text)

    def test_the_records_of_the_instances(self) -> None:
        instances, _ = self.instances()
        work = self.root / "work"
        instances.up("probe", self.process(2000), str(work))
        records = instances.records()
        self.assertEqual(records["kinds"], {"other": {"instances": 1, "free": 1}, "probe": {"instances": 2, "free": 1}, "quiet": {"instances": 1, "free": 1}})
        by_name = {record["instance"]: record for record in records["instances"]}
        self.assertEqual(list(by_name), ["other-1", "probe-1", "probe-2", "quiet-1"])
        probe = by_name["probe-1"]
        self.assertEqual(
            {k: probe[k] for k in ("kind", "directory", "defaultDirectory", "unit", "answering")},
            {"kind": "probe", "directory": str(work), "defaultDirectory": False, "unit": "active", "answering": True},
        )
        self.assertEqual(
            {k: probe["holder"][k] for k in ("pid", "start", "label", "alive")},
            {"pid": 2000, "start": "4242", "label": "label-2000", "alive": True},
        )
        self.assertIsInstance(probe["uptime"], int)
        self.assertEqual(
            by_name["quiet-1"],
            {
                "instance": "quiet-1",
                "kind": "quiet",
                "directory": str(self.directory("quiet-1")),
                "defaultDirectory": True,
                "holder": None,
                "unit": "inactive",
                "answering": None,
                "uptime": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
