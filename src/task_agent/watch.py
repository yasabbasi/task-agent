"""Hold the App key and keep running passes over every configured repo on main's latest code."""

import contextlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from task_agent.app_token import read_key
from task_agent.config import ROOT, load

STATE = ROOT / ".worktrees"
CLI = [sys.executable, "-m", "task_agent.cli"]


def say(line: str) -> None:
    """Print a line and keep it in the log, so it can be read without the terminal."""
    stamped = f"{datetime.now():%m-%d %H:%M:%S} {line}"
    with (STATE / "logs" / "watch.log").open("a") as log:
        log.write(stamped + "\n")
    with contextlib.suppress(OSError):
        print(stamped, flush=True)


def run(*cmd: str) -> subprocess.CompletedProcess[str]:
    """Run git, uv or this CLI in the checkout, without the owner's global git config."""
    quiet_git = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    env = os.environ | quiet_git
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)


def update() -> bool:
    """Fast-forward this checkout to origin/main; True when that brought new code."""
    before = run("git", "rev-parse", "HEAD").stdout
    pull = run("git", "pull", "--ff-only", "--quiet", "origin", "main")
    if pull.returncode:
        say(f"git pull failed: {pull.stderr.strip()[-300:]}")
    return run("git", "rev-parse", "HEAD").stdout != before


def restart(key: str, every: int) -> None:
    """Replace this process with one on the new code, handing it the key through a pipe."""
    run("uv", "sync", "--locked", "--quiet")
    if run(*CLI, "--version").returncode:
        return say("the new code does not start; staying on the old watcher")
    read, write = os.pipe()
    os.write(write, key.encode())
    os.close(write)
    os.set_inheritable(read, True)
    os.execv(sys.executable, [*CLI, "watch", "--every", str(every), "--key-fd", str(read)])


def one_pass(key: str) -> None:
    """Run one pass in a child on the current code; Ctrl-C here cancels it cleanly."""
    child = subprocess.Popen(
        [*CLI, "dispatch-all", "--key-stdin"], cwd=ROOT, text=True, start_new_session=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    assert child.stdin and child.stdout
    try:
        child.stdin.write(key)
        child.stdin.close()
        for line in child.stdout:
            say(line.rstrip())
    except KeyboardInterrupt:
        child.send_signal(signal.SIGINT)
        for line in child.stdout:
            say(line.rstrip())
        raise
    finally:
        child.wait()


def watch(every: int, key_fd: int | None) -> None:
    """Read the key once, then pull, pass and sleep until stopped."""
    for folder in ("logs", "requests"):
        (STATE / folder).mkdir(parents=True, exist_ok=True)
    for name in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(name, signal.default_int_handler)
    cfg = load()
    if key_fd is None:
        key = read_key(cfg.key_title, cfg.key_vault)
    else:
        with os.fdopen(key_fd) as pipe:
            key = pipe.read()
    say(f"watching {', '.join(cfg.repos)} every {every} seconds; Ctrl-C stops it")
    while True:
        if update():
            say("main changed: restarting on the new code")
            restart(key, every)
        (STATE / "requests" / "wake").unlink(missing_ok=True)
        unserved = set(os.listdir(STATE / "requests"))  # a request new since here ends the sleep
        one_pass(key)
        for _ in range(every // 5):
            if set(os.listdir(STATE / "requests")) - unserved:
                break
            time.sleep(5)
