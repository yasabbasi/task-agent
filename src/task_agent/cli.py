"""Command line entry point: `task-agent <command>`."""

import argparse
import os
import signal
import sys
from importlib.metadata import version
from pathlib import Path

from task_agent import close_out, config, dispatch, init, pr_format, readiness, verify, watch
from task_agent.app_token import App, read_key

ROOT = config.ROOT


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


def cmd_pr_check(args: argparse.Namespace) -> int:
    """The checks on an App pull request, read from PR_* variables; gate runs this."""
    env, bot = os.environ, args.bot or config.load().bot
    if env.get("PR_AUTHOR") != bot and not pr_format.BRANCH.fullmatch(env["PR_BRANCH"]):
        print("pr-check: not an App pull request, so nothing to check")
        return 0
    files = sorted(set(Path(args.files).read_text().splitlines()))
    count = int(env.get("PR_CHANGED_FILES") or 0)
    problems = pr_format.check(env["PR_TITLE"], env.get("PR_BODY", ""), env["PR_BRANCH"], files)
    if count >= pr_format.MAX_FILES or len(files) < count:
        problems.append(f"could not list all {count} changed files")
    for problem in problems:
        print(f"::error::{problem}")
    print(f"pr-check: {len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_close_out(args: argparse.Namespace) -> int:
    close_out.run_event(args.event, args.bot or config.load().bot)
    return 0


def loaded(args: argparse.Namespace) -> tuple[App, config.Config, config.Config]:
    """The App, main's settings, and those settings aimed at --repo and --clone."""
    base = config.load(dispatch.show_main(ROOT, "pyproject.toml"))
    stdin = getattr(args, "key_stdin", False)
    key = sys.stdin.read() if stdin else read_key(base.key_title, base.key_vault)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    target = base.target(getattr(args, "repo", None), getattr(args, "clone", None))
    return App(base.client_id, key), base, target


def cmd_dispatch(args: argparse.Namespace) -> int:
    app, base, target = loaded(args)
    dispatch.run_pass(app, base, [target], dry_run=args.dry_run)
    return 0


def cmd_dispatch_all(args: argparse.Namespace) -> int:
    app, base, _ = loaded(args)
    targets = [base.target(repo) for repo in base.repos]
    dispatch.run_pass(app, base, targets, each=lambda cfg, out: verify.requested(app, cfg, out))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        watch.watch(args.every, args.key_fd)
    except KeyboardInterrupt:
        watch.say("stopped")
    return 130


def cmd_verify(args: argparse.Namespace) -> int:
    if args.request:
        target = config.load().target(args.repo)
        repo, request = target.repo, ROOT / ".worktrees" / "requests" / f"verify-{target.name}"
        request.write_text(str(args.pr or ""))
        print(f"asked the watcher to verify {repo}; results go to .worktrees/logs/watch.log")
        return 0
    app, _, target = loaded(args)
    results = verify.run(app, target, args.pr, sessions=not args.skip_session)
    for ok, text in results:
        print(f"{'pass' if ok else 'fail'}: {text}")
    return 0 if all(ok for ok, _ in results) else 1


def cmd_init(args: argparse.Namespace) -> int:
    base = config.load(dispatch.show_main(ROOT, "pyproject.toml"))
    init.run(base, base.target(args.repo, args.clone))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit code."""
    parser = argparse.ArgumentParser(prog="task-agent")
    parser.add_argument("--version", action="version", version=version("task-agent"))
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, func: object, text: str) -> argparse.ArgumentParser:
        added = sub.add_parser(name, help=text)
        added.set_defaults(func=func)
        return added

    ready = command("ready", cmd_ready, "check a task issue; the body comes on stdin")
    ready.add_argument("--event", help="path of a GitHub issues event, inside Actions")
    check = command("pr-check", cmd_pr_check, "check an App pull request, described in PR_*")
    check.add_argument("--files", required=True, help="file listing the changed paths")
    closed = command("close-out", cmd_close_out, "tidy up after a merged pull request")
    closed.add_argument("--event", required=True, help="path of a pull_request closed event")
    for parsed in (check, closed):
        parsed.add_argument("--bot", help="the App's bot login; defaults to this repo's config")
    one = command("dispatch", cmd_dispatch, "run one pass over one repo")
    one.add_argument("--dry-run", action="store_true", help="list what would be taken")
    every = command("dispatch-all", cmd_dispatch_all, "run one pass over every configured repo")
    every.add_argument("--key-stdin", action="store_true", help="read the App key from stdin")
    loop = command("watch", cmd_watch, "keep running passes; asks for Touch ID once")
    loop.add_argument("--every", type=int, default=600, help="seconds between passes")
    loop.add_argument("--key-fd", type=int, help=argparse.SUPPRESS)
    guard = command("verify", cmd_verify, "check that the guardrails hold for one repo")
    guard.add_argument("--pr", type=int, help="an open, unapproved App pull request to try")
    guard.add_argument("--skip-session", action="store_true", help="skip the session checks")
    guard.add_argument("--request", action="store_true", help="ask the running watcher to do it")
    setup = command("init", cmd_init, "set up a repo for the loop, or bring it up to date")
    for parsed in (one, guard, setup):
        parsed.add_argument("--repo", help="owner/name; defaults to this repo")
        parsed.add_argument("--clone", help="its local clone; defaults to the configured one")

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
