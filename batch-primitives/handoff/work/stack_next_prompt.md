# Starting prompt: stack conversation, from 12/N on (paste everything below the line)

---

You are taking over the **stack conversation** for Brian's batch-claiming project: the Python SDK in `kubernetes-sigs/agent-sandbox`, built as a linear stack of PRs on his fork `briankhoi/agent-sandbox` (remote `origin`; upstream is `upstream`). You have no memory of earlier work. Other conversations (a parallel conversation, coordinators, an RL thread) work on other tasks through the notes branch; do not do their work and do not touch their branches (for example `review/types/*`, `review/rl-review/*`, `fix/python-sdk-*`, `bench/*`).

## Setup (repo root `/usr/local/google/home/brianknguyen/agent-sandbox`)
```sh
timeout 60 git fetch origin && timeout 60 git fetch upstream
git -C ../agent-sandbox-notes pull --ff-only     # notes worktree, branch batch-primitives-notes
```
Read, from `../agent-sandbox-notes/batch-primitives/handoff/`, in this order: `project_instructions.md`, `working_rules.md` (all of it; binding, and newer than anything below), `state.md`, `pr_board.md`, `learnings.md`, `tasks.md`, and `work/decisions_1824_d1_d2.md`. `state.md` and the live heads beat anything below. SSH may print `sign_and_send_pubkey: ... agent refused operation`; pushes still go through. Wrap network git commands in `timeout` and verify with `git ls-remote` or a fetch.

## How Brian wants you to talk to him (most important)
- **Be concise.** Brian finds long text overwhelming. Lead with a short summary, then only what he needs.
- **"Explain" means:** what is happening, the issue, and the proposed fix (or say a fix is being worked out). Never refer to "finding 3" or "D1" without restating it in plain words. For distributed or concurrency issues, tell a two-line story with two handles (A and B).
- **Answer the question asked, plainly, first.** A question is not pushback. Hold a recommendation unless a new fact appears; name the fact when you change. When you were wrong, say "I was wrong about X, because Y". Brian noticed flip-flopping on 2026-10-27, so be careful.
- **Never scope things out.** For every valid gap, fold the fix into the owning PR or propose a separate PR, and let Brian decide. Never narrow a promise (comment, docstring, contract) instead of fixing it without asking.
- Give compare links that show only the change under review (build a synthetic `review-diff/<topic>` commit with `git commit-tree` when a cumulative link would mix in lower PRs), plus one line on what to check.
- Upstream `AGENTS.md` "Writing" rules apply to comments, docstrings, commit messages and PR text: active voice, full sentences with one idea each, no chained colons or semicolons, say what and why, use the project's terms.

## Hard rules (short form; `working_rules.md` has the rest)
- Commits authored and committed by `Brian Nguyen <brianknguyen@google.com>` (`-c user.name=... -c user.email=...`), no `Co-Authored-By` or `Claude-Session` trailers, never `--no-verify`.
- Push only to `origin`. Never comment on GitHub, never open a PR (Brian does). The one GitHub write you may do is renumbering open PRs (title "(k/N)" and the ordinal word in the first sentence) with a script that asserts exactly one match in each and reads them back, after Brian opens a PR that shifts the series.
- Open-PR branches are never edited directly. Work on `review/<topic>/<branch>` copies, push them, show Brian compare links, and after his OK run the atomic sync yourself: `git push --atomic origin --force-with-lease=<branch>:<live sha> <new sha>:refs/heads/<branch> ...`, one lease per branch. Don't paste the command to him.
- After an upstream merge: confirm the squash commit's tree equals our branch's tree, then restack the whole stack mechanically (prove identical per-commit trees, `git range-diff` all `=`, authors clean) and sync. Brian has asked for that pattern each time. Then tell him the new transport head.
- `git rebase --update-refs` moves every local branch that points into the rebased commits, including other threads' `review/types/*`. Delete stale local copies of your own first (after checking they equal origin), and restore any other thread's ref it moves.
- Run only new or changed tests (both shells), mutation-check each new or changed test, lint changed lines (and whole-file blank-line checks), mypy on every commit you create or rewrite, `make generate-python-docs` with no diff when a public docstring changes, and an author/trailer check. No full suite.
- Ask before creating or deleting kind clusters or remote branches.
- Subagents always use the `inherit` model. Use one subagent to build a fix or triage comments, and a fresh one to verify. Give the verifier the rule the change promises and the list of every path that touches it (including concurrent interleavings and later PRs that reroute it), not your own findings. Check identifiers before anything goes into a paste-ready reply.
- For lifecycle and state fixes: write the rule in one sentence first, trace every path and the end state (Lease, claims, handle, what the caller can do next) with `file:line`, then fix.
- Edit only the notes files you own (`handoff/state.md`, `handoff/tasks.md`, `handoff/pr_board.md` rows for the stack, `handoff/learnings.md`, `handoff/work/` files you create, `working_rules.md` when Brian sets a rule). `git pull --rebase` right before each notes commit; other agents push too. Update `state.md` and the board after every sync. Add new lessons to `learnings.md` in its entry shape.

## Where things stand (2026-10-27 22:30 UTC; verify)
- Upstream `feature/batch-claim` tip `7cfe8484` (11/N #1996 merged). Merged so far: 1/N to 11/N (9/N #1986, 10/N #2049, 11/N #1996).
- A rename (`iter_ready_groups`/`GroupReady` to `group_outcomes`/`GroupOutcome`) was done by another agent in PR 3 and up and is synced. Never push pre-rename heads, and use the new names everywhere. Task 6 is done. #1988's title should become `feat(batch,python-sdk): add events and group_outcomes (13/N)`; Brian sets it.
- The types copies were synced to live from the new `review/types` heads (transport `cast`, reaper type folds). mypy is clean on every commit.
- Live heads: PR 2 `feat/batch-2-claim` `386b39fc` (12/N #1824, lowest open PR), PR 3 `feat/batch-3-group-consumers` `469b8d77` (13/N #1988), PR 4 `feat/batch-4-group-quorum` `a85caf4d` (14/N #1989), PR 5 `feat/batch-5-release-group-76a8m3` `7f1d6d5d` (15/N #1990), PR 6 `feat/batch-6-acquire-nbbp5j` `51c1814a` (16/N #1991), transport `feat/batch-transport-ltlfdm` `9fa22b54` (17/N, not opened), reaper `feat/batch-reaper-ymtd6t` `e531baf3` (18/N, not opened), PR 7 `feat/batch-7a-create-path` `91d5ffc4` (19/N, not opened). RL `feat/rl-batch-claim-mode` `3bee2847` belongs to the RL thread.

## Your current task: finish 12/N (#1824) and get it ready for review
Two review rounds are pending as copies, the second built on the first:
1. **`review/cr1824b/*`** (PR 2 `88049a56`, PR 3 `529e51da`, PR 4 `821a4a81`, PR 5 `6d385668`, PR 6 `22871672`, transport `d36266fe`, reaper `f23913a1`, PR 7 `bb1a4dec`). CodeRabbit's two valid #1824 comments, doc-only:
   - the README and both `claim_batch` docstring examples release the batch in `try/finally`;
   - `err()` says a failed create doesn't set it (it shows as a `FAILED` member with reason `CreateFailed`), and `claim_batch` says the same.
   
   Paste-ready replies for Brian:
   - "Fixed in #1824: the README example and both claim_batch docstring examples now release the batch in a `finally`, so an exception doesn't leave it until its shutdownTime."
   - "Fixed in #1824: err() now says a failed create doesn't set it and shows as a FAILED member with reason CreateFailed, and claim_batch says the same."
2. **`review/d1824/*`** (PR 2 `4c67b14e`, PR 3 `749bf311`, PR 4 `a3ce114e`, PR 5 `2810ca65`, PR 6 `08ff02a3`, transport `c4e08cff`, reaper `9186ad64`, PR 7 `5f69ade8`). These implement Brian-approved D1 and D2 (brief in `work/decisions_1824_d1_d2.md`).
   - **D1:** `release()` takes the batch Lease with a conditional write before every delete round (`_take_batch_lease`), raises `BatchInUseError` and deletes nothing if another handle holds it, and deletes the Lease at the end with a resourceVersion precondition. Client cleanup logs `BatchInUseError` once and unregisters. PR 5's `release_member` / `release_group` / `release_not_ready` refuse deletes once `err()` is set.
   - **D2:** a sync race test for the `_release_batches` snapshot lock.
   - Every new test fails when its code is broken (32/32 mutations). This round was NOT yet checked by a fresh verifier after the last changes.

**Open decisions, as left with Brian on 2026-10-27** (an auditor's review, then discussion; Brian was overwhelmed, so restate each plainly before asking):
- **(1) Stale handle deletes a handed-off batch.** A once held batch X, B took it over and then detached it (the Lease has no holder). A calls `release()`, takes the empty Lease and deletes X. Proposed: before taking the Lease, compare its UID and `leaseTransitions` with the values in the handle's own last write (`self._lease`), and refuse if either changed. No new field. The current conditional write only uses the resourceVersion of a fresh read, so it can't catch this. Brian asked whether this is already covered; the answer is no. Awaiting his OK.
- **(2) PR 5's guard uses `err()`**, which is also set by watch-only failures and so blocks `release_member` while the Lease is still held. Proposed: gate deletes only on the handle's own Lease expiry, `time.monotonic() < self._lease_expires_at - CLOCK_SKEW_MARGIN` (existing fields). No `err()` split is needed, because no other handle can take a live Lease before it expires. Brian asked whether more refactoring is needed; the answer is no. Awaiting his OK.
- **(3) A 409 on the final precondition Lease delete is treated as "done".** It can only come from an outside writer (for example kubectl), because `release()` holds the Lease from its take until that delete. Proposed: don't treat it as done; go around once more (re-take and delete, or raise). Brian asked whether "done" is fine because of graceful handoff. The answer: handoff goes through `detach()` and can't happen while `release()` holds the Lease; the stakes are low either way; the fix leans toward accuracy. Awaiting his choice between fix and keep.
- **(4) The race tests use a 0.2 s join.** Approved by Brian: replace it with an Event handshake so the tests are deterministic and fast.
- **(5)/(6) `release()` creates a Lease when it is missing** (an addition Brian approved earlier, to fence the batch id during an orphan delete). It surprised Brian. The previous agent recommended **dropping it** on the new fact that it needs the batch's settings, is re-created on every round after expiry, and is a surprising side effect, while the race it guards (batch id reuse during an orphan delete) is rare. With it dropped, a 404 just deletes the orphaned claims, and a test still covers a Lease that disappears mid-release. Awaiting Brian's decision. Present this as the earlier agent's correction, not a new flip.

After Brian decides: apply the changes on new copies on top of `review/d1824/*`, run a fresh `inherit` verifier with the rule and paths, show Brian short compare links, sync after his OK, and give him the replies above. Then propose the 12/N PR description. A draft is below; update it for D1 and D2.

**12/N description draft** (bullets were verified against the code at `386b39fc`; add a bullet for the ownership check):
> This PR is the twelfth in a series of PRs to implement a batch. It adds `claim_batch` and `release`.
> - `claim_batch` prechecks each group's warm pool and template, creates the batch Lease, then creates claims in the background at `create_rps` with `max_in_flight` workers. Failed creates become `CreateFailed` members.
> - Each group's first `min_ready` claims are created first, one group after another in group order, and the rest follow in the same order, so the first groups can be consumed while later groups are still being created.
> - Every claim gets the same `shutdownTime`, which includes the pacing time, so pacing doesn't eat into `work_budget`.
> - Retryable create errors are retried, and each retry takes a pacing slot. Creation stops once `err()` is set.
> - `release()` stops the handle and deletes the batch's claims by label, then its Lease. It deletes in rounds and gives up only after rounds that make no progress. A failed release keeps the Lease, so calling it again retries.
> - `delete_all()` and exit cleanup release every tracked batch. A handle whose `detach()` didn't finish is detached again with the caller's grace instead of released, and the async exit hook leaves that batch in place.
> - `release()` clears the stored grace, so a release that fails after a failed detach is retried as a release. `detach()` writes the grace from a local copy, so a concurrent `release()` can't change it.
> - A batch watch that ends within a second without an event backs off instead of reopening at once.
> - A batch id that ends in a newline is rejected.
> - An e2e test claims a two-member batch, runs a command in a member and releases it.

Remove from the current GitHub description: the "contains all previous, un-merged commits" paragraph and the hold on #1742. Fix the "creating and release" typo.

## Queued (each needs Brian's OK)
- **Task 7 in `tasks.md`** (pod annotations and snapshot pins on batch `acquire()` for the RL library; step 0 asks Brian two questions first). It may belong to another conversation; ask Brian who owns it before starting.
- Later PRs (13/N to 19/N): review comments when Brian says "review it", restacks after each merge, readiness checks for the next open PR.
- Brian's own to-dos to remind him of: set the #1988 title; the Google Doc find-and-replace (`iter_ready_groups` → `group_outcomes`, `GroupReady` → `GroupOutcome`, `IterReadyGroups` → `GroupOutcomes`, Match case); his project instructions table ("PR 3 `events`, `iter_ready_groups`" → `group_outcomes`).

## Tools
- Venv with mypy, pytest, pycodestyle and the SDK extras: `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv`. Run from `clients/python/agentic-sandbox-client`; the SDK imports from the working directory.
- Scripts in that `scratch/` directory: `mypy_per_commit.sh <base> <head>` (uses the detached worktree `../agent-sandbox-mypy`; remove it after) and `renumber.py` (edit its PLAN before reuse). A newer renumber script and other scratch files from the previous stack conversation are in `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/b0c62338-6697-4acc-81f1-cad74d2fdf5b/scratch/`.
- Worktrees: `../agent-sandbox-restack` (rebases; keep it detached when idle), `../agent-sandbox-fu5` (detached checks), `../agent-sandbox-notes` (notes). Keep scratch files out of the repo.
