"""Command line entry point: `task-agent <command>`."""

import argparse
import os
import sys
from importlib.metadata import version
from pathlib import Path

from task_agent import readiness


def cmd_ready(args: argparse.Namespace) -> int:
    """Readiness check: a GitHub event in Actions, or an issue body on stdin."""
    root = Path.cwd()
    if args.event:
        readiness.run_event(args.event, os.environ["GITHUB_REPOSITORY"], root)
        return 0
    fields = readiness.parse_body(sys.stdin.read())
    results = readiness.check(fields, readiness.path_checker(root))
    print(readiness.comment(results))
    return 0 if all(ok for ok, _ in results) else 1


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit code."""
    parser = argparse.ArgumentParser(prog="task-agent")
    parser.add_argument("--version", action="version", version=version("task-agent"))
    sub = parser.add_subparsers(dest="command", required=True)

    ready = sub.add_parser("ready", help="check a task issue; the body comes on stdin")
    ready.add_argument("--event", help="path of a GitHub issues event, inside Actions")
    ready.set_defaults(func=cmd_ready)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
