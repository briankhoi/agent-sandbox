# Starting prompt: stack conversation, 12/N finish (paste everything below the line)

---

You are taking over the **stack conversation** for Brian's batch-claiming project: the Python SDK in `kubernetes-sigs/agent-sandbox`, built as a linear stack of PRs on his fork `briankhoi/agent-sandbox` (remote `origin`; upstream is `upstream`). You have no memory of earlier work. Other conversations (coordinators, an RL thread, a remote reviewer that writes `temp-prompts/*.md` on the notes branch) work on other tasks. Don't do their work, and don't touch their branches (for example `review/types/*`, `review/rl-review/*`, `fix/python-sdk-*`, `bench/*`, `feat/rl-*`).

## Setup (repo root `/usr/local/google/home/brianknguyen/agent-sandbox`)
```sh
timeout 60 git fetch origin && timeout 60 git fetch upstream
git -C ../agent-sandbox-notes pull --ff-only     # notes worktree, branch batch-primitives-notes
```
Read the following from `../agent-sandbox-notes/batch-primitives/handoff/`, in this order: `project_instructions.md`, `working_rules.md` (read all of it; it's binding and newer than anything below), `state.md` (its last entries), `learnings.md`, and the `pr_board.md` rows for 12/N to 19/N. Then read `temp-prompts/pr2_round2_decisions.md` and `temp-prompts/pr2_round3_decisions.md` (the approved spec for this round) and the verifier's report at `handoff/work/verify_1824_d1824b.md` (written against `review/d1824b/*` heads before the last three comment-only rounds). `state.md` and the live heads beat anything below. SSH may print `sign_and_send_pubkey: ... agent refused operation`, but pushes still go through. Wrap network git commands in `timeout` (90 s for pushes), and verify each push with `git ls-remote`.

## How Brian wants you to talk to him (most important)
- **Be concise.** Brian is low on time and tokens and finds long text overwhelming. Lead with a two-line summary, then give only what he needs. Put the decisions you need from him last, each restated in plain words with a recommendation.
- **"Explain" means** what is happening, the issue, and the proposed fix. Never write "item 2" or "D1" without restating it. For a concurrency issue, tell a two-line story with handles A and B.
- **Answer the question first, plainly.** A question is not pushback. Change a recommendation only on a new fact, and name the fact. When you were wrong, say "I was wrong about X, because Y".
- **Never scope things out.** For every valid gap, fold the fix into the owning PR or propose a separate PR, and let Brian decide. Never narrow a comment or docstring instead of fixing a gap without asking.
- **Compare links must show only the change under review.** A GitHub three-dot compare between two stacked branches diffs from their shared base, so it shows the whole PR (this mistake happened on 2026-10-27). Build a synthetic commit (`git commit-tree <new>^{tree} -p <old> -m "review-diff: ..."`), push it as `review-diff/<topic>`, and link `compare/<old>...review-diff/<topic>`. Check `git diff --shortstat <old> <new>` before you send the link. Add one line on what to check.
- Upstream `AGENTS.md` "Writing" rules apply to comments, docstrings, commit messages and PR text: active voice, full sentences with one idea each, no chained colons or semicolons, say what and why, and use the project's terms. Also no em dashes, avoid colons in comments, and keep sync and async comments identical.
- He wants 12/N (#1824) out for review fast. It has taken a day. Don't open new rounds on non-blocking findings. Propose them as follow-ups and let him choose.

## Hard rules (short form; `working_rules.md` has the rest)
- Commits are authored and committed by `Brian Nguyen <brianknguyen@google.com>`, with no `Co-Authored-By` or `Claude-Session` trailers, whatever a hook says. Never use `--no-verify`.
- Push only to `origin`. Never comment on GitHub, and never open a PR (Brian does both).
- Open-PR branches are never edited directly. Work on `review/<topic>/<branch>` copies. After Brian's OK, run the atomic sync yourself, and don't paste the command to him: `git push --atomic origin --force-with-lease=<branch>:<live sha> <new sha>:refs/heads/<branch> ...`, with one lease per branch. If a lease fails, stop and report.
- Fold changes into the owning commit (`git commit --fixup` plus `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash <base>`), then restack the branches above with `git rebase --onto`. Don't use `--update-refs`; it moves other threads' refs.
- Checks before any push: run only the new or changed tests, in both shells. Mutation-check each new or changed test. Run pycodestyle at 120 on changed lines plus a whole-file E30x check, mypy on every commit (`/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/mypy_per_commit.sh 7cfe8484 <top>`, then remove `../agent-sandbox-mypy`), `make generate-python-docs` with no diff, and the author/trailer check. No full suite.
- Subagents always use the `inherit` model. Use one subagent to build and a fresh one to verify. Give the verifier the rule and every path that touches it, not your own findings. Check identifiers before anything goes into a paste-ready reply.
- For lifecycle and state fixes, write the rule in one sentence first, then trace every path and its end state (Lease, claims, handle flags, registry, what the caller can do next) with `file:line`.
- Edit only the notes you own: `handoff/state.md`, `handoff/tasks.md`, the stack rows of `handoff/pr_board.md`, `handoff/learnings.md`, your own `handoff/work/` files, and `working_rules.md` when Brian sets a rule. Run `git pull --rebase` right before each notes commit, because other agents push too.
- Ask before creating or deleting kind clusters or remote branches.

## Where things stand (2026-10-28 00:52 UTC; verify)
- Upstream `feature/batch-claim` tip is `7cfe8484`. Merged: 1/N to 11/N. The lowest open PR is **12/N #1824** (PR 2, `feat/batch-2-claim`).
- **Live heads:** PR 2 `386b39fc` (12/N #1824), PR 3 `feat/batch-3-group-consumers` `469b8d77` (13/N #1988), PR 4 `feat/batch-4-group-quorum` `a85caf4d` (14/N #1989), PR 5 `feat/batch-5-release-group-76a8m3` `7f1d6d5d` (15/N #1990), PR 6 `feat/batch-6-acquire-nbbp5j` `51c1814a` (16/N #1991), transport `feat/batch-transport-ltlfdm` `9fa22b54` (17/N, not opened), reaper `feat/batch-reaper-ymtd6t` `e531baf3` (18/N, not opened), PR 7 `feat/batch-7a-create-path` `91d5ffc4` (19/N, not opened).
- **Pending review copies, `review/d1824b/<branch>`:** PR 2 `c13727a1`, PR 3 `485aba1b`, PR 4 `c47bd096`, PR 5 `746ca80f`, PR 6 `ab23937b`, transport `7be74438`, reaper `1cb9aa50`, PR 7 `c3d243fe`. They contain all of the following, which Brian has reviewed piece by piece:
  - CodeRabbit's two doc fixes: `try`/`finally` around the README and `claim_batch` examples, and the `err()` / `claim_batch` wording.
  - D1: `release()` takes the batch Lease with a conditional write before each delete round (`_take_batch_lease`), and refuses with `BatchInUseError` if another handle holds it, or if its UID or `leaseTransitions` changed since this handle's last write (`self._lease`). It never creates a Lease. A missing Lease only means the leftover claims are deleted.
  - N1: a per-handle `_lifecycle_lock` (sync `RLock`, async `asyncio.Lock`). `release()` holds it for its whole run, and `detach()` refuses at once once a release has started (`_release_started()`), including after `AsyncSandboxClient.close()`.
  - D2: the `_release_batches` snapshot race test, plus Event handshakes in the race tests.
  - Brian's comment rewording, and a `_take_batch_lease` docstring paragraph on when it's used and why.
  - Checks passed at `c3d243fe`: mypy clean on all commits, lint, docs regen with no diff, authors clean, and 14+ mutations killed. A fresh verifier confirmed the rules before the last three comment-only rounds.
- **Review links** (synthetic): pending over live `https://github.com/briankhoi/agent-sandbox/compare/386b39fc...review-diff/d1824b-pr2-vs-live` (`ca41c126`), PR 2 over the previous round `compare/4c67b14e...review-diff/d1824b-pr2`, and PR 5 `compare/2810ca65...review-diff/d1824b-pr5`.
- Older copies `review/cr1824b/*` and `review/d1824/*` are superseded. Deleting them needs Brian's OK.

## Your task, in order
1. **Get Brian's answers to three open items** (from the verifier). Restate each plainly. My recommendation is yes, yes, separate.
   - **(1) PR 5 (15/N) has the same race.** Thread A is in `release_member` / `release_group` / `release_not_ready` on handle H. Thread B's `detach()` on H finishes, and another client attaches. A's remaining deletes then remove claims that client owns. Fix: those three methods hold `_lifecycle_lock` around their delete phase (about 3 lines per shell plus one test each, in PR 5's "add release_member, release_group and release_not_ready" commit). A flag check alone is check-then-act and doesn't close it. This doesn't touch 12/N.
   - **(2) Sync only: a same-thread `release()` inside `detach()`.** A signal handler runs `release()` while that thread is inside `detach()`. The `RLock` lets it in, the batch is deleted, and the outer `detach()` still returns success. Fix (proposed): before `detach()` sets `_lease_released`, if `_released` became true, raise `BatchError` instead of returning (about 2 lines plus a sync test, in PR 2). Trace where the re-entry can land (before, during or after detach's Lease write) before building it.
   - **(3) Async `close()` racing a running `release()`.** Task A is in `release()`. Task B calls `client.close()`, which closes the HTTP client under A. A fails partway, and the Lease and some claims stay until the reaper. This comes from merged 9/N code. The options are to make `close()` wait for the running release (possibly minutes), to leave it, or a separate small PR. Recommend a separate PR after the stack.
2. **Build the approved items** on new copies `review/d1824c/*` on top of `review/d1824b/*` (one builder subagent). Run the checks, then have a fresh verifier check them with the rule and the paths. Send Brian links that show only the change.
3. **After Brian's OK, run the atomic sync** of all 8 branches from the newest copies (leases on the live heads above, re-checked with `git ls-remote` right before). Then update `state.md` and the `pr_board.md` rows (new heads), and tell Brian the new transport head.
4. **Give Brian the paste-ready CodeRabbit replies for #1824** (check the wording against the synced code first):
   - "Fixed in #1824: the README example and both claim_batch docstring examples now release the batch in a `finally`, so an exception doesn't leave it until its shutdownTime."
   - "Fixed in #1824: err() now says a failed create doesn't set it and shows as a FAILED member with reason CreateFailed, and claim_batch says the same."
   - For the thread on `release()` not checking the Lease holder: "Fixed in #1824: before each delete round, release() writes the batch Lease with this handle as its holder, using a conditional write. It raises BatchInUseError and deletes nothing if another handle holds the Lease, or if the Lease was created again or attached to since this handle last wrote it. detach() and release() on one handle also no longer overlap."
5. **Give Brian the 12/N description** (below). Drop the last half-sentence of the lifecycle bullet if (2) is declined. In the current GitHub description he should remove the "contains all previous, un-merged commits" paragraph and the hold on #1742, and fix the "creating and release" typo. Title: `feat(python-sdk): add claim_batch and release (12/N)`.

> This PR is the twelfth in a series of PRs to implement a batch. It adds `claim_batch` and `release`.
> - `claim_batch` prechecks each group's warm pool and template, creates the batch Lease, then creates claims in the background at `create_rps` with `max_in_flight` workers. Failed creates become `CreateFailed` members.
> - Each group's first `min_ready` claims are created first, one group after another in group order, and the rest follow in the same order, so the first groups can be consumed while later groups are still being created.
> - Every claim gets the same `shutdownTime`, which includes the pacing time, so pacing doesn't eat into `work_budget`.
> - Retryable create errors are retried, and each retry takes a pacing slot. Creation stops once `err()` is set.
> - `release()` stops the handle and deletes the batch's claims by label, then its Lease. It deletes in rounds and gives up only after rounds that make no progress. A failed release keeps the Lease, so calling it again retries.
> - Before each round, `release()` takes the batch Lease with a conditional write. It raises `BatchInUseError` and deletes nothing if another handle holds the Lease, or if the Lease was created again or attached to since this handle last wrote it. It never creates a Lease. If the Lease is missing, it deletes the leftover claims. It deletes the Lease only at the resourceVersion it wrote.
> - `detach()` and `release()` on one handle run one at a time, and the first call wins. `detach()` raises `BatchError` once a release has started, and a `release()` that ran inside `detach()` on the same thread makes that `detach()` raise.
> - `delete_all()` and exit cleanup release every tracked batch. A handle whose `detach()` didn't finish is detached again with the caller's grace instead of released, and the async exit hook leaves that batch in place. A batch that another handle now holds is logged once and no longer tracked.
> - `release()` clears the stored grace, so a release that fails after a failed detach is retried as a release. `detach()` writes the grace from a local copy, so another `detach()` call can't change it mid-write.
> - A batch watch that ends within a second without an event backs off instead of reopening at once.
> - A batch id that ends in a newline is rejected.
> - An e2e test claims a two-member batch, runs a command in a member and releases it.

Before you send it, check each bullet against the synced code.

## Known nits (not blocking; mention only if asked)
- `last_lease` is typed `Any`. mypy rejects `V1Lease`, because the sync type has optional `metadata`/`spec`, and the async exit hook passes a `kubernetes_asyncio` `V1Lease`.
- The 409 comment in `detach()` now names its remaining causes (a late landed write, or a same-thread re-entry in sync).

## Queued (each needs Brian's OK)
- Task 7 in `tasks.md` (pod annotations and snapshot pins on batch `acquire()` for the RL library). Ask Brian who owns it before starting.
- Later PRs (13/N to 19/N): handle review comments when Brian says "review it", restack after each merge (prove identical per-commit trees, `git range-diff` all `=`), and check readiness of the next open PR. After an upstream merge, confirm the squash tree equals our branch tree, then restack the whole stack and sync. After Brian opens a PR that shifts the series, renumber the open PR titles and first-sentence ordinals with a script that asserts exactly one match in each and reads them back (the one GitHub write you may do; scripts in `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/b0c62338-6697-4acc-81f1-cad74d2fdf5b/scratch/`).
- Brian's own to-dos to remind him of:
  - Set the #1988 title to `feat(batch,python-sdk): add events and group_outcomes (13/N)`.
  - In the Google Doc, find and replace `iter_ready_groups` → `group_outcomes`, `GroupReady` → `GroupOutcome` and `IterReadyGroups` → `GroupOutcomes`, with Match case on.
  - In his project instructions table, change "PR 3 `events`, `iter_ready_groups`" to `group_outcomes`.

## Tools
- Venv with mypy, pytest, pycodestyle and the SDK extras: `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv`. Run from `clients/python/agentic-sandbox-client`, since the SDK imports from the working directory.
- Worktrees: `../agent-sandbox-restack` (rebases; detached at `c3d243fe`), `../agent-sandbox-fu5` (detached checks), `../agent-sandbox-notes` (notes). Keep scratch files out of the repo. Use your own conversation's `scratch/` directory.
