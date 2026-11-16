"""Command line entry point: `task-agent <command>`."""

import argparse
import sys
from importlib.metadata import version


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit code."""
    parser = argparse.ArgumentParser(prog="task-agent")
    parser.add_argument("--version", action="version", version=version("task-agent"))
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
