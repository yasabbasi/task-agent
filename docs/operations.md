# Operations

How the watcher runs, how to ask it for things, and where to look when something goes wrong.

## One-time setup

1. Create a GitHub App with read and write access to Contents, Issues and Pull requests,
   nothing else, and no webhook. Install it on every repo under `[tool.task-agent.repos]`.
2. Keep its private key in one of two places:
   - a file outside every checkout, where sessions can't read it, named by the
     `TASK_AGENT_KEY_FILE` environment variable
   - 1Password, as a document; then delete the file:
     `op document create key.pem --title "task-agent key" --vault <vault>`
3. Put the App's slug and client ID under `[tool.task-agent]` in `pyproject.toml`, with the
   1Password item's title and vault. When `TASK_AGENT_KEY_FILE` is set, 1Password is not used.
4. Run `make doctor` on the machine that runs the watcher. It names the fix for anything that
   is missing.

## The watcher

`make watch` reads the key once and keeps it in memory; 1Password may ask you to unlock it.
Then it repeats, every `EVERY` seconds (600 by default):

1. Fast-forward this checkout to `origin/main`. If that brought new code, the watcher
   restarts itself on it and hands the key to the new process through a pipe.
2. Run one pass in a child process, with the key on its standard input. The pass refuses to
   run unless the checkout is a clean, up-to-date `main`.
3. For each configured repo: answer a verify or metrics request if there is one, take every
   task the owner marked ready whose dependencies are closed as completed, and rework every
   pull request in review that the owner commented on or that conflicts with `main`.

Ctrl-C stops the sessions, releases the claims and puts each task back to `status:ready`, or
to `status:review` for a rework.

The machine must stay awake while the watcher runs. Platform note: on macOS,
`caffeinate -i make watch` keeps it from idle sleep; a closed lid still sleeps it.

Keep the checkout the watcher runs from on `main` and unchanged. Do other work in a separate
worktree or clone.

## Requests

The watcher holds the key, so anything that needs the key can be asked of it with a file in
`.worktrees/requests/`:

| File | Effect |
|---|---|
| `wake` | Start the next pass now instead of waiting. |
| `verify-<repo name>` | Run `make verify` for that repo. The file may hold a pull request number to try merging. |
| `metrics` | During the pass, open a pull request that refreshes the metrics table in `README.md`, as `make metrics PUBLISH=1` does. |

A new file there ends the wait early. `make verify REPO=owner/name REQUEST=1` writes the verify
request, and so does `uv run task-agent verify --repo owner/name --request`. Run either one
from the checkout the watcher runs in.

An empty file at `.worktrees/requests/metrics` is the metrics request. The watcher removes it
and logs one line. It files an issue labelled `metrics` and opens the pull request that closes
it. Nothing is opened while an earlier `metrics` issue is open, when a repo could not be read,
or when the table has not changed. `make metrics` alone prints the table.

## Setting up another repo

`make init REPO=owner/name` runs as whoever is signed in to `gh`, and is safe to run again.
First install the App on the repo, clone it, and list it under `[tool.task-agent.repos]`.
It takes two runs with a merge between them:

1. The first run creates the labels, allows only squash merges, deletes merged branches and
   keeps Actions from approving pull requests. Then it opens a pull request, "Set up
   task-agent", from the branch `chore/task-agent-init`. It holds the task form, the pull
   request template, `CODEOWNERS`, a `LICENSE` if the repo has none, and the `gate`,
   `readiness` and `close-out` workflows. The workflows pin the newest version tag
   (`vX.Y.Z`) on task-agent's `main` by its commit, or `main`'s commit when there is no tag.
2. Merge that pull request.
3. The second run finds the files in place and applies the ruleset. It prints
   `owner/name is up to date`.

A `gate_step` under `[tool.task-agent]` in the repo's own `pyproject.toml` becomes an extra
step at the end of its `gate`. Init warns when the repo is private and its owner is on the
free plan, because rulesets are not enforced there. Run it again after a new version tag to
bring the workflows up to date.

## When a task is blocked

A task or a rework that fails gets `status:blocked` and a comment that starts with
**Blocked:** and gives the reason. The owner fixes the cause, then adds `status:ready` again.
If the task already has an open pull request from the App, the retry reworks that pull request
instead of starting over. Common reasons:

| Reason | What the owner does |
|---|---|
| The session timed out, or ran out of turns or budget | Split the task, or raise the limit under `[tool.task-agent]`. |
| The agent made no changes | Read its explanation in the comment and make the issue clearer. |
| Nothing was pushed: a protected path changed | Make that change by hand, or take it out of the task. |
| The work is on the branch, but its description fails | Retry, or open the pull request by hand from the branch. |
| Nothing was pushed: a file is still unmerged or has a conflict marker | Resolve the conflict on the branch by hand, or retry. |
| Refused a rework: the branch changes `pyproject.toml`, `uv.lock` or `.python-version` | Take that change off the branch by hand. |

Next to the limits, `model` pins the session's model (passed as `--model`); when it is empty,
the installed Claude Code's default is used.

A task that is not taken, with no comment, shows in the log instead:

- `not taken`: the latest `status:ready` label isn't the owner's, or the issue was edited
  after it. Add the label again.
- `waiting on #N (open)`: a dependency is not closed as completed. Close it, or edit
  "Depends on" and add the label again.
- `the rework changed nothing`: the pull request gets a comment saying so and stays in review.

## Lessons

After a merge, the `close-out` workflow reads the pull request's description. Each line under
`## Lessons` that starts with `rule:` becomes an issue labelled `status:triage` and `lesson`.
Its title is the rule's first 72 characters, and its body is the rule and a link to the pull
request. At most 5 are filed per pull request, and a rule that an open lesson already holds is
not filed again. Text in HTML comments and code blocks is not read.

The owner closes a lesson, or moves it into `CLAUDE.md` or `prompts/` by hand, since agents
may not change those. Close-out also clears the status labels of the issues the pull request
closed, and labels a pull request the App didn't write `human-authored`.

## Where to look

| What | Where |
|---|---|
| Every line the watcher printed | `.worktrees/logs/watch.log` |
| A session's raw output | `.worktrees/logs/<repo>-issue-<n>-<time>-<id>.json` |
| Why a task was blocked | the comment on its issue |
| A task's worktree while it runs | `<clone>/.worktrees/issue-<n>` |

## Verify

`make verify` checks, for one repo:

- the ruleset requires the owner's approval after the last push and the `gate` check from
  GitHub Actions
- the App cannot get permissions it was not given, push to `main`, or push a workflow change
- inside a real session: no network, no reads of the home folder, `gh`'s token or git's
  stored credentials, no writes outside the worktree or to its `.git` file
- the Read and Write tools stay inside the checkout, and Edit is refused on `CLAUDE.md`
- a planted `.mcp.json` and a user-level skill are not loaded
- a $0.01 budget stops a session

Each line reads `pass`, `fail` or `skip`. Platform note: the planted keychain item that a session
must fail to read is a macOS check, and is skipped on other systems.

## Running elsewhere

Nothing in a pass depends on the machine that runs the watcher. A pass is one stateless
command, `make dispatch-all`, that any scheduler can run, because state lives in GitHub:
labels, claim comments and pull requests. The watcher only repeats it and holds the key.

In containers, the split is the one the loop already has:

- The session runs in a container with the checkout, no network and no credentials.
- A separate trusted step, which holds the App's key, checks the session's output and
  publishes it: the commit, the push and the pull request.

When the watcher's environment sets `TASK_AGENT_RUNNER`, its words, split as a shell would,
go in front of the session and the dependency install: `$TASK_AGENT_RUNNER session claude -p
...` and `$TASK_AGENT_RUNNER prepare uv sync --locked --quiet`, both run in the worktree. The
session's settings then turn the OS sandbox off, so the runner must be the box: no network,
no credentials, nothing but the worktree. The variable is never read from a repo's files and
is not passed to the session. Unset or empty, both commands run as before.

Not built yet:

- cloning on demand: every repo needs a clone on the machine already
- a container mode: sessions rely on the OS sandbox, which is proven only on macOS so far
- a Linux run of `make verify`
