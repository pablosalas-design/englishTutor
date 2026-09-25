---
name: GitHub REST commit fidelity
description: How to preserve exact local commit IDs when recreating commits through GitHub's Git Database API.
---

When recreating a local commit through the Git Database API, the tree, parent, author, committer, and message bytes must match. Git commit messages normally end with a newline; preserve it when creating the commit.

**Why:** Git hashes cover the raw commit object, so an otherwise identical message without its terminal newline produces a different SHA.

**How to apply:** Compare each API-created commit SHA with the local SHA before advancing the branch. Use a non-forced fast-forward ref update only after the entire chain matches.