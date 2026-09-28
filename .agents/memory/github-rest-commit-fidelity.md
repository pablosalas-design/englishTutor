---
name: GitHub REST commit fidelity
description: How to preserve exact local commit IDs when recreating commits through GitHub's Git Database API.
---

When recreating a local commit through the Git Database API, the tree, parent, author, committer, and message bytes must match. Git commit messages normally end with a newline; preserve it when creating the commit.

**Why:** Git hashes cover the raw commit object, so an otherwise identical message without its terminal newline produces a different SHA.

**How to apply:** Compare each API-created commit SHA with the local SHA before advancing the branch. Use a non-forced fast-forward ref update only after the entire chain matches.

In this workspace, the Git remote may reject direct HTTPS pushes even when the GitHub integration is connected; the authenticated GitHub REST connection can recreate the tree and advance `main`, but it creates a new commit unless all commit metadata is preserved.

**Why:** Replit's connected GitHub authorization is available to the integration proxy, not necessarily to the shell's Git credential helper.

**How to apply:** Treat a successful REST ref update as the GitHub publish result, then verify the remote branch SHA and the downstream deploy trigger separately.