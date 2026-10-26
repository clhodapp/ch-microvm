# SPDX-License-Identifier: MIT
"""The command line `ch-microvm`.

The command accepts only its verbs, the kind and instance names of the
configuration, and the options of `up` and `status`. It refuses every
other argument before it reads any state. `CH_MICROVM_CONFIG` names the
configuration file. The wrapper the home-manager module builds sets it.

The holder of the instance that `up` leases is the process `--holder`
names. Without `--holder`, it is the parent process of the command,
which is the shell a user types the command in. The user who runs the
command controls every instance, so `down` stops an instance whatever
process holds it.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

from .instances import Instances, Refused, load

CONFIG_VARIABLE = "CH_MICROVM_CONFIG"

VERBS = ("up", "down", "status", "reap")

USAGE = """usage:
  ch-microvm up <kind> [--directory DIR] [--holder PID]
      lease an instance of <kind> to the process PID, or to the parent process, start it in DIR or in its
      default directory, and wait until it answers
  ch-microvm down <instance>
      stop the instance and drop its lease
  ch-microvm status [--json]
      every instance with its directory, its holder and the state of its units, and the free instances of each kind
  ch-microvm reap
      stop every instance whose holder process is gone"""


class UsageError(Exception):
    pass


@dataclass(frozen=True)
class Command:
    verb: str
    name: str | None = None
    directory: str | None = None
    holder: int | None = None
    json: bool = False


def instance_names(kinds: dict[str, dict]) -> list[str]:
    return [f"{kind}-{n}" for kind, info in sorted(kinds.items()) for n in range(1, int(info["maxInstances"]) + 1)]


def parse(argv: list[str], config: dict, cwd: str) -> Command:
    """The command in `argv`. Every argument that is not part of a verb
    is refused. A relative directory is taken relative to `cwd`."""
    kinds: dict[str, dict] = config["kinds"]
    if not argv:
        raise UsageError("no verb given")
    verb, rest = argv[0], argv[1:]
    if verb not in VERBS:
        raise UsageError(f"unknown verb {verb!r}; the verbs are {', '.join(VERBS)}")
    if verb == "up":
        if not rest:
            raise UsageError("up takes a kind")
        kind, options = rest[0], rest[1:]
        if kind not in kinds:
            raise UsageError(f"unknown kind {kind!r}; the kinds are {', '.join(sorted(kinds)) or 'none'}")
        directory: str | None = None
        holder: int | None = None
        seen: set[str] = set()
        while options:
            option = options[0]
            if option not in ("--directory", "--holder"):
                raise UsageError(f"up takes --directory and --holder, not {option!r}")
            if option in seen:
                raise UsageError(f"up takes {option} once")
            if len(options) < 2:
                raise UsageError(f"{option} takes a value")
            seen.add(option)
            value, options = options[1], options[2:]
            if option == "--directory":
                if not value or value.startswith("-"):
                    raise UsageError(f"{value!r} is not a directory")
                directory = os.path.join(cwd, value)
            else:
                if not value.isdigit() or int(value) <= 0:
                    raise UsageError(f"{value!r} is not a process ID")
                holder = int(value)
        return Command(verb, name=kind, directory=directory, holder=holder)
    if verb == "down":
        names = instance_names(kinds)
        if len(rest) != 1:
            raise UsageError("down takes one instance")
        if rest[0] not in names:
            raise UsageError(f"unknown instance {rest[0]!r}; the instances are {', '.join(names) or 'none'}")
        return Command(verb, name=rest[0])
    if verb == "status":
        if rest not in ([], ["--json"]):
            raise UsageError("status takes only --json")
        return Command(verb, json=rest == ["--json"])
    if rest:
        raise UsageError(f"{verb} takes no arguments")
    return Command(verb)


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = dict(os.environ) if env is None else env
    try:
        config_path = env.get(CONFIG_VARIABLE)
        if not config_path:
            raise Refused(f"{CONFIG_VARIABLE} is not set; run the ch-microvm command the home-manager module installs")
        config = load(config_path)
        try:
            command = parse(argv, config, os.getcwd())
        except UsageError as error:
            print(f"ch-microvm: {error}\n{USAGE}", file=sys.stderr)
            return 2

        instances = Instances(config, out=sys.stdout)
        if command.verb == "reap":
            instances.reap()
        elif command.verb == "up":
            holder = instances.holder_for(command.holder if command.holder is not None else os.getppid())
            instances.up(command.name, holder, command.directory)
        elif command.verb == "down":
            instances.stop(command.name)
        elif command.json:
            print(json.dumps(instances.records(), indent=2, sort_keys=True))
        else:
            instances.status(None, everything=True)
        return 0
    except Refused as error:
        print(f"ch-microvm: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"ch-microvm: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
