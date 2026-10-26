# SPDX-License-Identifier: MIT
"""The command line, end to end over the fakes of the fixture, for a
kind that declares no vsock port, so no probe runs: up, status as text
and as JSON, and down; a directory and a holder for up; the holder of
up without --holder; down of an instance another process holds; and
the refusal of every argument that is not part of a verb."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest

from fixture import Fixture
from ch_microvm import cli


class CommandLineCase(Fixture):
    def main(self, *argv: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config))
        base = {cli.CONFIG_VARIABLE: str(config_path)}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), base | (env or {}))
        return code, out.getvalue(), err.getvalue()

    def test_up_status_down(self) -> None:
        self.process(1000)
        code, out, err = self.main("up", "quiet", "--holder", "1000")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("instance quiet-1 (kind quiet)", out)
        self.assertEqual(self.lease("quiet-1")["pid"], 1000)
        code, out, _ = self.main("status")
        self.assertEqual(code, 0)
        self.assertIn(f"quiet-1  kind quiet  directory {self.directory('quiet-1')}  held by process 1000  active", out)
        self.assertIn("  quiet: 0 of 1 instances free", out)
        code, out, _ = self.main("status", "--json")
        self.assertEqual(code, 0)
        records = json.loads(out)
        quiet = [record for record in records["instances"] if record["instance"] == "quiet-1"][0]
        self.assertEqual((quiet["holder"]["pid"], quiet["unit"]), (1000, "active"))
        self.assertEqual(records["kinds"]["quiet"], {"instances": 1, "free": 0})
        code, out, _ = self.main("down", "quiet-1")
        self.assertEqual(code, 0)
        self.assertIn("stopped quiet-1", out)
        self.assertIsNone(self.lease("quiet-1"))

    def test_up_in_a_directory(self) -> None:
        self.process(1000)
        work = self.root / "work"
        code, out, err = self.main("up", "quiet", "--directory", str(work), "--holder", "1000")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"directory {work} (passed by the caller, kept as it was)", out)

    def test_the_holder_without_an_option_is_the_parent(self) -> None:
        # The fake /proc gives the parent of the test the start time 77.
        self.proc.process(os.getppid(), "77")
        code, _, err = self.main("up", "quiet")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((self.lease("quiet-1")["pid"], self.lease("quiet-1")["start"]), (os.getppid(), "77"))

    def test_down_stops_an_instance_another_process_holds(self) -> None:
        self.process(1000)
        self.main("up", "quiet", "--holder", "1000")
        code, out, _ = self.main("down", "quiet-1")
        self.assertEqual(code, 0)
        self.assertIn("stopped quiet-1", out)
        self.assertEqual(self.units(), {})

    def test_a_relative_directory_is_taken_from_the_working_directory(self) -> None:
        self.assertEqual(cli.parse(["up", "quiet", "--directory", "work"], self.config, "/home/u").directory, "/home/u/work")
        self.assertEqual(cli.parse(["up", "quiet", "--directory", "/w"], self.config, "/home/u").directory, "/w")

    def test_arguments_outside_the_verbs_and_names_are_refused(self) -> None:
        for argv in (
            [],
            ["start", "probe"],
            ["up"],
            ["up", "probe", "/a"],
            ["up", "probe", "--directory"],
            ["up", "probe", "--directory", ""],
            ["up", "probe", "--directory", "--holder"],
            ["up", "probe", "--directory", "/a", "--directory", "/b"],
            ["up", "probe", "--holder", "x1"],
            ["up", "probe", "--holder", "0"],
            ["up", "probe", "--all"],
            ["up", "../etc"],
            ["up", "probe-1"],
            ["up", "--holder", "1"],
            ["status", "--all"],
            ["status", "probe-1"],
            ["down"],
            ["down", "probe"],
            ["down", "probe-3"],
            ["down", "probe-1", "probe-2"],
            ["reap", "probe-1"],
            ["mcp"],
        ):
            with self.subTest(argv=argv):
                code, out, err = self.main(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("usage:", err)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.state.exists())

    def test_a_holder_that_does_not_run_is_refused(self) -> None:
        code, _, err = self.main("up", "quiet", "--holder", "12345")
        self.assertEqual(code, 1)
        self.assertIn("the holding process 12345 does not run", err)
        self.assertEqual(self.calls(), [])

    def test_without_a_configuration_nothing_runs(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["status"], {})
        self.assertEqual(code, 1)
        self.assertIn("CH_MICROVM_CONFIG is not set", err.getvalue())


if __name__ == "__main__":
    unittest.main()
