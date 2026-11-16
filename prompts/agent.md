Work on issue #{number} in {repo}.

<issue>
{title}

{body}
</issue>
{review}

Make the smallest change that satisfies every "Done when" item, with tests. Write each new test first and run it against the unchanged code, to see it fail for the reason you expect. Run `make check` until it passes.

When you're done, return:

- pr_title: one imperative line in sentence case, at most 72 characters, no period. It becomes the commit message too.
- pr_body: Markdown that starts with the line `Closes #{number}`, then exactly these sections in order: `## Why`, `## What changed`, `## Evidence` (commands you ran and what they showed), `## Not done`, `## After merge`, `## Lessons`. Every section needs text: write "Nothing." where there is nothing to say. At most 250 words, not counting code blocks, so aim for 200. Put code in plain ``` fences that start a line. Don't write "closes", "fixes" or "resolves" before any other issue number. Under Lessons, write one line starting with `rule:` for each rule future agents should follow, or "None". No emoji, and nothing after the Lessons section: no "Generated with" line.

If the task can't be done within the rules, change nothing and explain why under "Not done".
