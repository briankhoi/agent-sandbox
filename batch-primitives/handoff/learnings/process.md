<a id="sec-process"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

# Process, git, and working with agents

These lessons are about how the work went, not what the code does. Most came from a mistake that cost a review round. The big idea: **reviewers and verifiers answer the questions you give them, so write the rule down, list every path that touches it, and check facts against the source before anything goes public.**

<a id="sec-proc"></a>

## Reviewing and deciding

<a id="proc-1"></a>

### PROC-1 Write the rule first, then check every path against it

**Takeaway:** For lifecycle or state changes, write the promise in one sentence, list every path that touches it (concurrent interleavings and later PRs that reroute it included), and give that list to the reviewer.

<details>
<summary>The full story (10/N)</summary>

- **Situation:** A cleanup fix grew over three rounds: close in `finally`, keep failed closes, then take out and put back. Each round was reviewed against its own question.
- **What goes wrong:** CodeRabbit then found two paths that broke the overall promise ("a failed close is always retried"): a `detach()` path we had scoped out, and overlapping cleanups that no verifier was asked about.
- **What we do:** State the promise once, enumerate the paths, and hand the verifier both. Reviewers answer the questions they are given.
- **Related:** [CLEAN-1](field-guide/clean.md#clean-1), [PROC-2](#proc-2).

</details>

<a id="proc-2"></a>

### PROC-2 Never scope a gap out silently

**Takeaway:** For every valid gap, either fold the fix into the owning PR or propose a separate PR, and let the owner decide. Narrowing a promise (a comment, docstring, or contract) is the owner's decision, not the implementer's.

<details>
<summary>The full story</summary>

- **What goes wrong:** To keep a round small, an agent narrowed a comment instead of fixing a real gap.
- **What we do:** Brian's rule (2026-10-23): never scope a finding out. Fold it in or propose a PR, and put the choice to him.
- **Related:** [PROC-9](#proc-9) (verify the gap first).

</details>

<a id="proc-3"></a>

### PROC-3 Trace the end state before proposing a fix

**Takeaway:** Before proposing, trace what happens to each piece of state (Lease, claims, handle, registry) and what the user can do next, with a code reference for each step. Mark anything unchecked as inferred.

<details>
<summary>The full story</summary>

- **What goes wrong:** A proposed fix ("let the Lease expire") was justified with "the batch can still be re-attached". That contradicted an already verified fact: `get_batch` rejects a stale Lease.
- **Related:** [LEASE-4](field-guide/lease.md#lease-4), [LEASE-14](field-guide/lease.md#lease-14).

</details>

<a id="proc-4"></a>

### PROC-4 "It was like that before" is not a reason during design

**Takeaway:** While a design is still open, judge it on its merits and its documented promises. Precedent in your own unmerged or recent code is not evidence.

<details>
<summary>The full story</summary>

- **What goes wrong:** We declined a reviewer comment because "the merged 1/N code already does clear-then-close". The comment was right ([CLEAN-1](field-guide/clean.md#clean-1)).

</details>

<a id="proc-5"></a>

### PROC-5 Verify identifiers and numbers before they go public

**Takeaway:** Check anything that will be posted publicly (PR numbers, commit hashes, file:line) against the source. Never recall it.

<details>
<summary>The full story</summary>

- **What goes wrong:** A subagent invented a PR number ("#2052") in a paste-ready reply.

</details>

<a id="proc-6"></a>

### PROC-6 Recommendations: list alternatives with costs, pick one, and hold it

**Takeaway:** List the alternatives with their cost and pick one. Hold that pick under questions, and change it only on a new fact, which you name. When wrong, say "I was wrong about X because Y". A question is not pushback.

<a id="proc-7"></a>

### PROC-7 "Minimal" means the smallest resulting code, not the smallest diff

**Takeaway:** State each rule once. Before adding state, list the data already available. After applying any change, a reviewer's suggestion taken literally included, re-read the whole function and module for a condition now expressed twice.

<details>
<summary>The full story (8/N)</summary>

- **Example:** Adding `ssl.SSLEOFError` to the transient tuple duplicated a helper's own EOF branch ([LIB-2](field-guide/lib.md#lib-2)).

</details>

<a id="proc-8"></a>

### PROC-8 Explain in "what happens / issue / fix"

**Takeaway:** Explain each item as what is happening, the issue, and the proposed fix (or "being worked out"). Never refer to "finding 3" without restating it.

<details>
<summary>The full story</summary>

- **What goes wrong:** A numbered list of findings without context confused the reader. This is also the shape the field guide uses.

</details>

<a id="proc-9"></a>

### PROC-9 Verify a finding is a real bug before proposing a PR for it

**Takeaway:** "Never scope things out" applies after you verify that a gap is real. Check caller entrypoints, the documented contract, and standard library practice first, and recommend no change when the behavior is intended.

<details>
<summary>The full story</summary>

- **What goes wrong:** A verifier noted that async `close()` didn't take `_lifecycle_lock`, and a handoff recommended a separate PR. Checking `__aexit__()`, `delete_all()`, and `aiohttp`/`httpx` afterwards showed that the cluster-cleanup paths already waited on the lock, and that bare `close()` matched standard immediate transport teardown.
- **Related:** [PROC-2](#proc-2), [LEASE-23](field-guide/lease.md#lease-23).

</details>

<a id="proc-10"></a>

### PROC-10 Audit cross-cutting lock invariants across the whole stack and against real bounds

**Takeaway:** When you introduce a teardown lock at the base of a stack, audit every delete and teardown site across all PRs in one pass. Check real timeout and TTL bounds before you flag a race as reachable, and check whether an exclusive lock serializes concurrent single-item I/O.

<details>
<summary>The full story (12/N, 15/N, 16/N)</summary>

- **What goes wrong:** Adding `_lifecycle_lock` to 12/N (`detach()` and `release()`) surfaced follow-on findings in 15/N (`release_*`) and 16/N (`acquire()`), one round at a time. A verifier finding that `detach()` stops `_renew_loop` while waiting behind a slow delete was proposed as a fix before anyone checked that `get_batch` rejects an expired Lease, and that the reaper margin (`leaseDurationSeconds + 60s >= 120s`) exceeds the 90 s maximum delete retry time. Putting an exclusive `_lifecycle_lock` around `release_member` would also have serialized concurrent single-member HTTP deletes.
- **Related:** [LOCK-17](field-guide/lock.md#lock-17), [LEASE-19](field-guide/lease.md#lease-19), [LEASE-21](field-guide/lease.md#lease-21).

</details>

<a id="proc-11"></a>

### PROC-11 SDK defaults must compose, and a validator guards only the paths that call it

**Takeaway:** Two defaults from the same SDK must work together without raising. Adapt (and log) when the caller omits a knob, and fail fast only when an explicit caller value contradicts capacity. Before you claim a validator makes a branch unreachable, check every constructor path, injected objects included.

<details>
<summary>The full story (17/N, 18/N)</summary>

- **What goes wrong:** Triaging CodeRabbit on 18/N, we defended `check_pool_size` raising `ValueError` on hosts with 4 or fewer cores (`cpu_count() * 5 <= 20 < 20 + 2`) as intended fail-fast behavior. We also dismissed `or None` on sync `api_pool_size()` because `validate_pool_size` rejects `pool_size <= 0`. Upstream review on 17/N caught both. Zero-argument `SandboxClient().claim_batch(...)` and `BatchClaimer` failed out of the box on 2- and 4-core machines. And `validate_pool_size` never inspects an injected `ApiClient` whose `configuration.connection_pool_maxsize = 0` (urllib3's `LifoQueue(maxsize=0)` is unbounded).
- **What we do:** `batch_utils.check_pool_size` clamps an omitted `max_in_flight` to `pool_size - 2` and logs it, and raises only for an explicit value that doesn't fit. `K8sHelper.api_pool_size()` maps 0 to `None` (unbounded).
- **Related:** [PERF-16](field-guide/perf.md#perf-16), [PERF-18](field-guide/perf.md#perf-18), [RETRY-19](field-guide/retry.md#retry-19).

</details>

<a id="proc-12"></a>

### PROC-12 Audit changelogs and release notes clause by clause against the PR's own diff

**Takeaway:** Update package changelogs (such as `examples/agent-sandbox-rl/CHANGELOG.md`) in the PR that ships the behavior, and verify every clause against that PR's own diff rather than upper branches or unconditional summaries. Qualify conditional paths that still make an extra RPC or bypass a fast path.

<details>
<summary>The full story (25/N)</summary>

- **What goes wrong:** Upstream PR #2114 had to revert a changelog claim in `agent-sandbox-rl` for an SDK change that never landed. On 25/N (`claim_mode="batch"`), the first draft of the `CHANGELOG.md` entry omitted two qualifiers present in the code: `snapshot_uid` still reads the `Pod` to check `PodRestored` even though normal batch `acquire()` makes no per-claim `Sandbox` or `Pod` `GET`s, and `handle.is_alive()` checks the batch watch only until `record_pod_identity()` or `refresh()` runs.
- **What we do:** Have an independent verifier audit every sentence of `CHANGELOG.md` against `base..HEAD` of the owning PR, checking both conditional feature paths (`snapshot_uid`, `refresh()`, `fail_fast`) and upper branches (`feat/rl-batch-hardening`) so unshipped behavior never leaks into release notes.
- **Related:** [PROC-5](#proc-5), [WATCH-10](field-guide/watch.md#watch-10).

</details>

<a id="proc-13"></a>

### PROC-13 Design a state model once before patching edge cases, and end every fix loop with a holistic review

**Takeaway:** When a fix needs new bookkeeping, write its invariants and every add and remove path first, then review the whole model at once. Don't let verifiers drip edge cases into incremental patches. Close each fix loop with a cohesion and redundancy review, not only bug hunts.

<details>
<summary>The full story (25/N, 2026-11-12)</summary>

- **What happened:** The #2252 round took 9 verifier rounds (2 angles each). Most of rounds 3 to 7 were a cascade from patching the pending-delete model one edge case at a time: each patch added state (`_pending_deletes_by_pool`, `_label_deletes`) that the next round found a new hole in.
- **What we do:** A holistic cohesion review collapsed the cascade: it removed both structures and derived pending deletes from `_released` (claim to pool) plus `_release_sweeps`. One final confirm round then passed.
- **Related:** [PROC-1](#proc-1), [PROC-3](#proc-3), [PROC-7](#proc-7), [AGENT-6](#agent-6).

</details>

<a id="proc-14"></a>

### PROC-14 Review moved code as new code

**Takeaway:** Code moved between commits or PRs lands in a new context with new callers and invariants. Review it as fresh code, not as "already reviewed".

<details>
<summary>The full story (25/N)</summary>

- **What happened:** `release_members()` and the parallel pool prechecks moved from 27/N into 25/N. The #2252 round still had to fix them: released claims' deletes had to stay pending until their label delete succeeds, and acquires and queued outcomes had to be fenced against release sweeps.
- **Related:** [PROC-2](#proc-2), [PROC-13](#proc-13).

</details>

<a id="proc-15"></a>

### PROC-15 Check dependency behavior at the oldest supported version

**Takeaway:** When a fix relies on a library call, check what that call does at the oldest version the package supports, not only the installed one.

<details>
<summary>The full story (25/N)</summary>

- **What happened:** CodeRabbit flagged that the owned-client close in 25/N must clear `pool_manager` itself because `ApiClient.close()` on kubernetes 35 doesn't. The same class of bug hit 24/N: `Watch.stop()` only sets a flag before kubernetes 37, so a quiet watch blocked `release()` for the full join timeout.
- **Related:** [PROC-5](#proc-5).

</details>

<a id="proc-16"></a>

### PROC-16 Model multi-task interleavings for any new lock or gate

**Takeaway:** Before adding a lock, gate, or fence, write out the interleavings of every task that can reach it (caller, watch, cleanup, concurrent acquires) and check each for deadlock and lost wakeups.

<details>
<summary>The full story (25/N)</summary>

- **What happened:** CodeRabbit found a close/detach deadlock in async close serialization and an acquire that bypassed the release-sweep fence. Both were two-task interleavings no test covered.
- **Related:** [PROC-10](#proc-10).

</details>

<a id="sec-git"></a>

## Stacked PRs and git

<a id="git-1"></a>

### GIT-1 Stacked PRs show the whole stack's diff

**Takeaway:** Every open PR in a stack targets the same base, so its diff and its bot review include all lower PRs. For each comment, find the owning commit (`git log -S`, blame), say which PR owns it, and fix it there.

<a id="git-2"></a>

### GIT-2 Squash merges change hashes but keep trees

**Takeaway:** After a squash merge, confirm the squash commit's tree equals the branch tree before restacking: `git rev-parse <squash>^{tree} <branch>^{tree}`.

<a id="git-3"></a>

### GIT-3 Prove a restack is mechanical before syncing without review

**Takeaway:** Check that every branch tree and every per-commit tree is identical, that `git range-diff` is all `=`, and that authors are clean. Only then sync without a new review.

<a id="git-4"></a>

### GIT-4 `git rebase --update-refs` moves every local branch that points at rebased commits

**Takeaway:** It moves old review copies too. Delete stale local copies first, after checking they exist on the remote.

<a id="git-5"></a>

### GIT-5 Keep a long stack consistent with review copies and one atomic push

**Takeaway:** Push with one lease per branch, `git push --atomic --force-with-lease=<b>:<old> <new>:refs/heads/<b> ...`, so either every branch moves or none does.

<a id="git-6"></a>

### GIT-6 When a PR merges before its review fixes land, insert a small PR and renumber

**Takeaway:** Put the fixes in a new small PR on the merged base, insert it into the series, and renumber the open PRs with a script that asserts exactly one "(k/N)" in each title and one ordinal in each body, then reads both back.

<details>
<summary>The full story (9/N, 10/N)</summary>

- **What happened:** 9/N merged first, so its review follow-ups went into the new 10/N branch `feat/batch-1-fu7b-cleanup-retry`, and every branch above was restacked and renumbered.

</details>

<a id="git-7"></a>

### GIT-7 Reordering commits causes semantic conflicts that git doesn't flag

**Takeaway:** When a commit moves below others, later commits may delete symbols it uses. After any non-mechanical restack, grep for removed symbols and type-check every commit.

<a id="git-8"></a>

### GIT-8 Wrap network git commands in `timeout`, and verify with `git ls-remote`

**Takeaway:** SSH agent warnings can appear even when a push succeeded, so trust `git ls-remote`, not the terminal output.

<a id="git-9"></a>

### GIT-9 A three-dot compare between stacked branches shows the whole PR

**Takeaway:** A three-dot compare diffs from the merge base, not from the left commit. For rewritten history, link a synthetic commit, and check its size first.

<details>
<summary>The full story</summary>

- **What goes wrong:** A review link compared the live PR branch with its review copy using `compare/<live>...<copy>`. Both sit on the same upstream base, so GitHub diffed from that base and showed +3k lines instead of the ~1k under review.
- **What we do:** Build a synthetic commit with the old commit as parent and the new tree (`git commit-tree`), push it as `review-diff/<topic>`, and check `git diff --shortstat` before sending.

</details>

<a id="git-10"></a>

### GIT-10 When several agents push to one stack, re-check live heads and diff the diffs

**Takeaway:** Run `git ls-remote` across all stack branches right before syncing, rebase concurrent upper-branch commits onto the new lower head, and verify zero dropped hunks with a diff-of-diffs before the atomic push.

<details>
<summary>The full story (15/N to 20/N)</summary>

- **What happened:** While a review round on 12/N and 15/N waited for approval, another agent pushed per-claim `_acquire_waiters` to 16/N through 19/N and added 20/N on top of the pre-review 15/N head.
- **What we do:** `git ls-remote` caught the moved and added branches before pushing. Rebasing the live 16/N to 20/N commits onto the new 15/N head and running `diff -u <(git diff old_top new_live_top) <(git diff our_old_top our_rebased_top)` caught two carry-forward hunks from the lower review round that would otherwise have been dropped.

</details>

<a id="git-11"></a>

### GIT-11 A commit message that names a PR shows up on that PR

**Takeaway:** Never put `#<number>` in a commit message on any branch of the fork. Refer to stack positions (13/N) instead, and grep new commit messages for `#[0-9]` before pushing.

<details>
<summary>The full story</summary>

- **What goes wrong:** Notes-branch commits such as "paste-ready replies for #1824 and #1988" appeared on those upstream PRs as "briankhoi added a commit that referenced this pull request". PR numbers inside file contents are fine, because GitHub doesn't cross-link file contents.

</details>

<a id="git-12"></a>

### GIT-12 Audit non-conflicting lifecycle hooks when `upstream/main` moves under a merge branch

**Takeaway:** Whenever `upstream/main` moves beneath a feature branch or merge PR, diff `old_main..new_main` across shared SDK modules and audit every new lifecycle hook, destructor registration, and handle factory, even in files with zero merge conflicts.

<details>
<summary>The full story (24/N)</summary>

- **What happened:** 24/N (`merge/main-into-batch-claim-2`) originally merged `upstream/main` at `355803af`. Later, `upstream/main` merged static `functools.partial` `atexit` hooks (`ae6bb389`), `_on_terminated` callbacks (`7b1a9fbc`), and the client-shared `_DefaultSSLContext` (`9f7999b3`), all touching `SandboxClient` and `AsyncSandboxClient`.
- **What goes wrong:** git flags only textual overlaps. A new client hook such as `_attach_handle` setting `connector._default_ssl_context` or `create_sandbox` forwarding `file_config` produces zero conflict in `AsyncSandboxBatch.connect()` ([PERF-19](field-guide/perf.md#perf-19)). Even inside the same function, non-overlapping hunks auto-merge cleanly when `main` turns `_delete_automatic_sandboxes(self)` and `_atexit_cleanup(self)` into `@staticmethod` functions at the top while the feature branch appends `self._release_batches(exiting=True)` or `self._active_batches` 30 lines lower, leaving a latent `NameError: name 'self' is not defined` inside the exit hook (`5a3cc71f`, [CLEAN-12](field-guide/clean.md#clean-12)).

</details>

<a id="git-13"></a>

### GIT-13 Re-parent a two-parent merge commit with `commit-tree` only when its first parent's tree is unchanged

**Takeaway:** `git commit-tree` re-parents an unchanged tree in O(1). A changed first-parent tree needs a real three-way merge, so lower-stack fixes survive.

<details>
<summary>The full story (24/N)</summary>

- **Situation:** 24/N sits on 23/N as a true two-parent merge commit (`/label tide/merge-method-merge`), so Prow keeps main's commit ancestry instead of squashing main onto `feature/batch-claim`.
- **What goes wrong:** Plain `git rebase` drops merge commits (or replays every commit from main). When 23/N's tree changes from a lower-stack review fix, reusing the old merge tree drops those lower-stack changes.
- **What we do:** When 19/N to 23/N restack mechanically with an unchanged 23/N tree, re-parent with `git commit-tree M^{tree} -p <new-23N> -p <main>` and cherry-pick 24/N's follow-up commits. When 23/N's tree changes, merge `<main>` into `<new-23N>` (carrying the conflict resolutions forward) and regenerate the docs.

</details>

<a id="sec-agents"></a>

## Working with subagents

<a id="agent-1"></a>

### AGENT-1 Use a fresh, independent verifier for every non-trivial change

**Takeaway:** Give it the rule, the paths, and the base and head, but not your own findings, so it forms its own view.

<a id="agent-2"></a>

### AGENT-2 Ask for measurements, not opinions

**Takeaway:** Ask the verifier for mutation results ("removing X must fail a test"), not for whether the code looks right.

<a id="agent-3"></a>

### AGENT-3 Ask the minimality question explicitly

**Takeaway:** Ask "Is there a smaller change with the same behavior? Does it add state, duplicate a rule, or widen a type?"

<a id="agent-4"></a>

### AGENT-4 Briefs carry the binding rules, the exact heads, and what not to touch

**Takeaway:** Say no live-branch pushes and no GitHub posts. Have the agent verify heads before acting, because server restarts can interrupt work midway.

<a id="agent-5"></a>

### AGENT-5 Fan out by slice, then merge with a coverage check

**Takeaway:** When many agents sweep one codebase, give each a slice and a shared primer, have them report stable finding ids, and check afterwards that every id landed somewhere or was dropped with a reason.

<details>
<summary>The full story (this doc)</summary>

- **What happened:** Ten read-only agents swept 1/N to 23/N in slices and returned 312 findings. Thirteen writers turned them into sections. Because the writers ran in parallel, 35 findings they handed to other sections were never picked up, until a coverage check (every finding id included or dropped with a reason) caught them and a second round integrated them.
- **Related:** [AGENT-1](#agent-1).

</details>

<a id="agent-6"></a>

### AGENT-6 Converge verification sweeps by pairing three orthogonal adversarial roles until unanimous `ZERO_FINDINGS`

**Takeaway:** One reviewer persona misses whole defect axes. Loop three specialized, independent subagents in parallel until all three return `ZERO_FINDINGS` on the same revision: an un-anchored code hunter (reads raw source before reading your report), a `/tmp` post-fix simulator (applies your proposed fixes in `/tmp` to break your tests and static rules), and an AST/line-number fact-checker.

<details>
<summary>The full story (Chaos & Verification Sweep, Passes 1-5)</summary>

- **What happened:** Our initial verification sweep found `12` bugs (`F-01`..`F-12`). Running `5` iterative adversarial rounds expanded coverage to `29` verified production bugs (`48` failing test reproductions) while catching `18` defects in our own tests, TLA+ flags, static AST checks, and surgical fix recipes before a single line of production code changed:
  - The **un-anchored code hunter** read the raw SDK and RL modules cold before reading `VERIFICATION_SWEEP_RESULTS.md`, uncovering `F-24` (in-flight `acquire()` escaping `release()`), `F-25` (popped pacing-slot starvation), `F-28` (`recycle.py` `held[img]` `-1` underflow), and `F-29` (Lease `PUT` `404` TOCTOU).
  - The **`/tmp` post-fix simulator** patched a temporary copy of the codebase with our own report's proposed fixes, catching `6` tests/AST rules that failed *after* fixing the bug (`CHAOS-1`) plus two proposed lock-order self-deadlocks (`_exit_detaches` and `_stop_batches`).
  - The **AST/prose fact-checker** verified every commit SHA, line range, lock-order claim, and symbol name against the source tree, catching Pass 4's shared `V1Lease` test mutation and `4` memory-recalled identifier typos before Pass 5 achieved unanimous `ZERO_FINDINGS` (`3/3`).
- **Related:** [AGENT-1](#agent-1), [AGENT-2](#agent-2), [CHAOS-1](chaos-verification-suite.md#chaos-1).

</details>

<a id="agent-7"></a>

### AGENT-7 Verify every backticked symbol, default argument, and lock-order claim against the AST instead of recalling from context

**Takeaway:** Long audit sessions cause agents to substitute plausible-sounding symbol names (`reap_batches` instead of `batch_reaper.reap`, `BATCH_CLOCK_SKEW_MARGIN_SECONDS` instead of `CLOCK_SKEW_MARGIN`) or miss indirect lock acquisitions when writing prose fix recipes. Grep or AST-check every identifier and caller lock chain right before writing it down.

<details>
<summary>The full story (Chaos & Verification Sweep, Passes 1-4)</summary>

- **What goes wrong:** Even when the test code itself runs and passes, prose fix recommendations are not executed by Python unless tested in `/tmp`. Across Passes 1-4, memory-based recall produced `4` drifted symbol/signature citations (`reap_batches` vs `reap`, `BATCH_CLOCK_SKEW_MARGIN_SECONDS` vs `CLOCK_SKEW_MARGIN`, `check_pool_size` default `explicit=False` vs `True`, and `w.resp` after `Watch.close()` clears `self.resp = None`) and `2` lock self-deadlocks where the caller already held `batch._lock` or `batch.detach()` re-entered `batch._lifecycle_lock`.
- **What we do:** Treat prose fix recipes as code: verify exact symbol spellings against `ast.parse` / `git show`, trace every caller's held locks before proposing a lock acquisition, and test non-trivial recipes in an isolated `/tmp` tree ([CHAOS-1](chaos-verification-suite.md#chaos-1)).
- **Related:** [PROC-10](#proc-10), [CHAOS-1](chaos-verification-suite.md#chaos-1).

</details>

<a id="agent-8"></a>

### AGENT-8 Record verified non-bugs ("intended contracts") explicitly so fresh auditors do not invert existing tests

**Takeaway:** Fresh adversarial subagents have no memory of why subtle design choices exist and will repeatedly flag intentional contracts as "bugs" (or propose "fixes" that break existing unit tests) unless your findings doc includes a cited **Verified Intended Contracts** table.

<details>
<summary>The full story (Chaos & Verification Sweep, Passes 2-3)</summary>

- **What happened:** In Pass 1, an auditor flagged `BatchClaims.acquire` raising `BatchError` instead of `RuntimeError` when un-opened (`F-21a`), missing that `test_batch_claims.py:282` and `:357` explicitly pin `BatchError` (`class BatchError(RuntimeError)`). Similarly, fixing `F-08` (`AsyncSandboxClient.__aexit__` retaining failed batches) broke `cleanup=False` (`test_async_sandbox_batch.py:1544`) until gated on `batch._release_started() and not batch._lease_released`, and fixing `F-11a` (`BatchClaims._teardown` sweeping all batches) broke `test_batch_claims.py:238` until `_teardown` re-raised `first_exc` after completing the sweep.
- **What we do:** Maintain an explicit "Verified Intended Contracts (Audited & Confirmed Sound)" section citing exact source lines and owning tests (such as `cleanup=False` detach semantics, `check_pool_size(explicit=False)` skipping `403` pool reads under least-privilege RBAC, and `BatchClaims.release_all()` keeping batches open for multi-step reuse). Subsequent audit passes stopped re-reporting settled contracts and checked new fix recipes against them.
- **Related:** [AGENT-1](#agent-1), [TEST-2](testing.md#test-2).

</details>

<a id="agent-9"></a>

### AGENT-9 Pair every subagent dispatch with a `schedule` watchdog and wrap shell calls in `timeout --kill-after`

**Takeaway:** Subagents can stall silently on interactive prompts, SSH port-22 hangs, unbounded `pytest` socket waits, or mid-turn model pauses without firing a completion event. Pair every subagent invocation with `schedule(DurationSeconds=..., TimerCondition="<subagent-id>")` and require `timeout --kill-after=10 <secs>` on every shell command.

<details>
<summary>The full story (23/N..27/N Remediation & 14-Stage Verification Campaign)</summary>

- **What happened:** During long verification runs, two failure modes stalled unattended subagents: (1) `git ls-remote origin` against an SSH remote (`git@github.com:...`) hung until killed whereas HTTPS with `!gh auth git-credential` finished in `1 s`, and (2) a 14-stage pipeline runner paused after an intermediate tool step without sending its final report until nudged.
- **What we do:** Enforce a three-layer anti-hang protocol across all builders and verifiers: (1) wrap every shell command in `timeout --kill-after=10 <secs>` and pass `GIT_TERMINAL_PROMPT=0` on git network calls, (2) pair every `invoke_subagent` or `send_message` call with `schedule(DurationSeconds=..., TimerCondition=...)` that auto-cancels on timely return and wakes the coordinator to inspect `transcript.jsonl` if the subagent stalls, and (3) combine deterministic 14-stage pipeline execution with three parallel domain-partitioned un-anchored verifiers (`Concurrency/Rollback`, `Watchdog/Handles/SDK`, `Minimalism/Hygiene/Test-Audit`) until all three return `ZERO_FINDINGS`.
- **Related:** [AGENT-1](#agent-1), [AGENT-8](#agent-8), [CHAOS-1](chaos-verification-suite.md#chaos-1).

</details>

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
