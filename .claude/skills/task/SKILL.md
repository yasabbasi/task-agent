---
name: task
description: File a task for an agent as a GitHub issue. Asks what should change plus type, priority and effort, drafts "Done when" and "Starting point" from the repo, runs the readiness check, then files the issue labeled status:triage. Use when the user types /task or asks to file a task for an agent.
---

# /task

File one GitHub issue in the format of the Task form (`.github/ISSUE_TEMPLATE/task.yml`), so
the readiness check reads both the same way.

1. **Repo.** The task goes to the repo of the current directory, or to the one the user
   names. Work in that repo's local clone. If you can't find the clone, ask where it is.
2. **What.** Text after `/task` is the "What". Without it, ask: "What should change, and why?"
3. **Type, priority, effort.** Ask in one AskUserQuestion call with three questions:
   - Type: feature, bug, docs, chore
   - Priority: p1 (do next), p2 (do soon), p3 (when convenient)
   - Effort: s (under an hour of agent work), m (a few hours), l (should be split)
4. **Draft.** Read just enough of the repo to write:
   - A title: imperative, sentence case, at most 72 characters, no period.
   - "Done when": 2 to 4 checklist items, each one something a test or a command can show.
   - "Starting point": 1 to 3 existing paths in backticks.
   - "Depends on": open issues whose work this task needs first, one `#N` or `owner/repo#N`
     per line (`gh issue list --state open`). Chain tasks that change the same files.
     Write `_No response_` when there are none.

   If the effort is `l`, suggest splitting the task first.
5. **Body.** Use exactly these headings, in this order:

   ```markdown
   ### What

   <what>

   ### Done when

   - [ ] <item>

   ### Starting point

   `<path>`

   ### Depends on

   #<N>

   ### Type

   <type>

   ### Priority

   <priority>

   ### Effort

   <effort>
   ```

6. **Check.** From the target clone, pipe the body into
   `uv run --project "${CLAUDE_SKILL_DIR}/../../.." task-agent ready` and fix every `fail` line.
7. **Confirm.** Show the title and body, and ask the user to confirm or edit.
8. **File.** Run `gh issue create --repo <owner/name> --title "<title>" --body-file -
   --label "status:triage,type:<type>,priority:<priority>,effort:<effort>"` with the body on
   stdin. Print the issue URL. An agent takes the task once the owner adds `status:ready`.
