# Operations

How the watcher runs, how to ask it for things, and where to look when something goes wrong.

## One-time setup

1. Create a GitHub App with read and write access to Contents, Issues and Pull requests,
   nothing else, and no webhook. Install it on every repo under `[tool.task-agent.repos]`.
2. Store its private key in 1Password as a document, then delete the file:
   `op document create key.pem --title "task-agent key" --vault <vault>`.
3. Put the App's slug and client ID, and the item's title and vault, under `[tool.task-agent]`
   in `pyproject.toml`.
4. Run `make doctor`. It names the fix for anything that is missing.

## The watcher

`make watch` reads the key once, which asks for Touch ID, and keeps it in memory. Then it
repeats, every `EVERY` seconds (600 by default):

1. Fast-forward this checkout to `origin/main`. If that brought new code, the watcher
   restarts itself on it and hands the key to the new process through a pipe.
2. Run one pass in a child process, with the key on its standard input. The pass refuses to
   run unless the checkout is a clean, up-to-date `main`.
3. For each configured repo: answer a verify request if there is one, then take every task
   the owner marked ready.

Ctrl-C stops the sessions, releases the claims and puts each task back to `status:ready`.

The Mac must stay awake. `caffeinate -i make watch` keeps it from idle sleep while the watcher
runs; a closed lid still sleeps it.

Keep the checkout the watcher runs from on `main` and unchanged. Do other work in a separate
worktree or clone.

## Requests

The watcher holds the key, so anything that needs the key can be asked of it with a file in
`.worktrees/requests/`:

| File | Effect |
|---|---|
| `wake` | Start the next pass now instead of waiting. |
| `verify-<repo name>` | Run `make verify` for that repo. The file may hold a pull request number to try merging. |
| `metrics` | After the pass, open a pull request that refreshes the metrics table in `README.md`, as `make metrics PUBLISH=1` does. |

A new file there ends the wait early. `make verify REPO=owner/name REQUEST=1` writes the verify
request, and so does `uv run task-agent verify --repo owner/name --request`. Run either one
from the checkout the watcher runs in.

## Where to look

| What | Where |
|---|---|
| Every line the watcher printed | `.worktrees/logs/watch.log` |
| A session's raw output | `.worktrees/logs/<repo>-issue-<n>-<time>.json` |
| Why a task was blocked | the comment on its issue |
| A task's worktree while it runs | `<clone>/.worktrees/issue-<n>` |

## Verify

`make verify` checks, for one repo:

- the ruleset requires the owner's approval after the last push and the `gate` check from
  GitHub Actions
- the App cannot get permissions it was not given, push to `main`, or push a workflow change
- inside a real session: no network, no reads of the home folder, a planted keychain item,
  `gh`'s token or git's stored credentials, no writes outside the worktree or to its `.git` file
- the Read and Write tools stay inside the checkout, and Edit is refused on `CLAUDE.md`
- a planted `.mcp.json` and a user-level skill are not loaded
- a $0.01 budget stops a session

Each line reads `pass` or `fail`.
