Work on issue #{number} in {repo}.

<issue>
{title}

{body}
</issue>
{review}

Make the smallest change that satisfies every "Done when" item, with tests. Run `make check` until it passes.

When you're done, return:

- pr_title: one imperative line in sentence case, at most 72 characters, no period. It becomes the commit message too.
- pr_body: Markdown that starts with the line `Closes #{number}`, then these sections in order: `## Why`, `## What changed`, `## Evidence` (commands you ran and what they showed), `## Not done`, `## After merge`, `## Lessons`. At most 250 words, not counting code blocks. Don't write "closes", "fixes" or "resolves" before any other issue number. Under Lessons, write one line starting with `rule:` for each rule future agents should follow, or "None".

If the task can't be done within the rules, change nothing and explain why under "Not done".
