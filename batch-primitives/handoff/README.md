# Handoff pack for the local agent

Paste the block below into a fresh Claude Code chat started in your local `agent-sandbox` clone. Everything it needs is in this folder; you do not need the cloud project's memory, threads or files.

```
You are taking over the SDK stack work for Brian's batch claiming project (Python SDK in kubernetes-sigs/agent-sandbox, stack of PRs on his fork briankhoi/agent-sandbox). You have no memory of earlier work, so everything you need is in a handoff pack on the notes branch.

Setup, in the repo root:
  git fetch origin
  git remote get-url upstream || git remote add upstream https://github.com/kubernetes-sigs/agent-sandbox.git
  git fetch upstream
  git worktree add ../agent-sandbox-notes origin/batch-primitives-notes   # skip if it already exists; then `git -C ../agent-sandbox-notes pull`
Read these in order, from ../agent-sandbox-notes/batch-primitives/handoff/:
  1. project_instructions.md   (Brian's rules; they override your defaults)
  2. working_rules.md          (how he wants work done and reported)
  3. state.md                  (stack, branches, merged work, approved decisions)
  4. tasks.md                  (ordered tasks; each says which other files to read)
Then do Task 1 in tasks.md. Do not start a later task until Brian says to, except to read ahead.

Hard rules (the files repeat them): commits authored by Brian Nguyen <brianknguyen@google.com> with no Co-Authored-By or Claude-Session trailers, never --no-verify, push only to origin, never comment on GitHub or open a PR (Brian does), review copies before syncing open-PR branches, ask before creating or deleting kind clusters, base every claim on code you have read, and report short: summary, ranked findings, decisions last.
```

Files here: `project_instructions.md`, `working_rules.md`, `state.md`, `tasks.md`, and `refs/` (cloud-only documents that tasks point to).
