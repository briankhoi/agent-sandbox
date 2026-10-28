# Learnings board (shared)

Edge cases, concurrency lessons, trade-offs and process lessons from the batch-claiming work in the agent-sandbox Python SDK, written so they transfer to other projects. Any agent may add entries. Keep each entry short and in the same shape. Base claims on code or measurements, and cite the PR or commit when it helps.

Entry shape: **What happened** (the situation), **Issue** (what went wrong or could), **Fix** (what we settled on, and rejected alternatives if useful), **Takeaway** (the reusable lesson).

Started 2026-10-26 by the stack conversation (SDK stack, PRs 8/N to 11/N and transport).

---

## 1. Resource cleanup and ownership

### 1.1 A failed stop skipped the close
- **What happened:** client cleanup stopped a batch's background work, then closed its cached connections (which hold port-forward subprocesses).
- **Issue:** if the stop raised, the close never ran, and the port-forwards leaked.
- **Fix:** close in a `finally` after the stop (#2049).
- **Takeaway:** when cleanup has several independent steps, a failure in one must not skip the others. Release resources in `finally`, not in sequence.

### 1.2 Clear-then-close lost connections that failed to close
- **What happened:** cleanup cleared the cache, then closed each connection.
- **Issue:** a close that raised or was cancelled lost its connection for good. Nothing could retry it, which broke the client's own documented promise that a connection that fails to close stays tracked for a retry.
- **Fix:** take one entry out of the cache under the lock, close it, and put it back only if the close did not succeed (#2049).
- **Takeaway:** a resource is either owned by exactly one closer or still registered. Never let it be neither. Check what your API already promises (docstrings) and keep that promise on every path.

### 1.3 Keeping entries in the cache while closing caused double closes
- **What happened:** a first attempt kept each entry in the cache until its close succeeded.
- **Issue:** two cleanups running at once (detach() and the client's close(), or two threads) could close the same connection twice.
- **Fix:** take the entry out under the lock before closing, so exactly one caller owns it, then put it back on failure.
- **Takeaway:** "remove under lock, act, restore on failure" gives single ownership. "Leave in place until done" gives double work under concurrency.

### 1.4 Overlapping cleanups untracked a connection still being closed
- **What happened:** cleanup A took the last connection and was closing it. Cleanup B saw an empty cache and unregistered the batch. A's close failed and put the connection back on a handle nobody tracked.
- **Fix:** a put-back also tells the client to keep tracking the handle, and the client keeps finished handles with leftovers in a separate set.
- **Takeaway:** "empty right now" is not "done". When a removal is decided from a snapshot, check what an in-flight operation can add back afterwards.

### 1.5 One registry slot per key pushed an old handle out
- **What happened:** the client registry holds one handle per batch. After a detach() whose close failed, re-attaching with get_batch replaced the old handle, and its leftover connection became unreachable.
- **Constraint:** keeping both handles in the main registry was unsafe, because cleanup calls release() there and release() deletes the batch's claims by label without checking who holds the Lease.
- **Fix:** a second small set of finished handles that only need their connections closed. Cleanup retries them and never calls release() or stop on them. The main registry stays one live handle per batch.
- **Takeaway:** separate "the live owner" from "leftovers still to clean up". Mixing them forces either data loss (overwrite) or destructive actions on the wrong object.

### 1.6 Stale connections must never be handed out, but must still be closed
- **What happened:** the watch drops a cached connection when its member is lost or moves pods, then closes it.
- **Issue:** a failed or cancelled close dropped the connection, and putting it back in the main cache would let connect() hand out a stale connection.
- **Fix:** a separate stale list that connect() never reads, closed by later cleanups (transport round).
- **Takeaway:** if an object must be both "unusable" and "still owed cleanup", give it a place readers never look.

### 1.7 Cleanup must stop new resources from being created
- **What happened:** client cleanup stopped a batch but left it usable.
- **Issue:** a caller still holding the handle could call connect() afterwards and create a connection that no cleanup would ever close.
- **Fix:** cleanup marks the handle detached first, so connect() raises. Where waiters sleep on a condition variable, also notify them, or they hang forever (PR 3).
- **Takeaway:** shutting down means "refuse new work" first, then "finish old work". When you flip a state flag that waiters depend on, wake the waiters.

---

## 2. Cancellation and interrupts

### 2.1 A cancelled task can be cancelled again while awaiting a lock
- **What happened:** the async put-back did `async with lock` in a `finally` after a cancelled close.
- **Issue:** a second cancel while waiting for the lock skipped the put-back.
- **Fix:** do the put-back with no await. In asyncio, code without an await cannot be interrupted, so a plain `dict.setdefault` is atomic.
- **Takeaway:** in asyncio, the safest critical section is one with no await at all. Don't take a lock just to make a single dict operation "safe".

### 2.2 KeyboardInterrupt is not an Exception
- **What happened:** the sync put-back ran only for `Exception`.
- **Issue:** Ctrl-C during a close (a `BaseException`) skipped it.
- **Fix:** `closed = False; try: closed = close() finally: if not closed: put back`, the same shape async needed for `CancelledError`.
- **Takeaway:** restore-on-failure logic belongs in `finally` keyed on a success flag, not in `except Exception`. Async `CancelledError` and sync `KeyboardInterrupt` both bypass `except Exception`.

### 2.3 Don't force network cleanup into `finally` on cancellation
- **What happened:** a reviewer asked to run batch cleanup in a `finally` when delete_all() is cancelled.
- **Why we declined:** nothing became unreachable (the batches stayed registered, so the next close() or context exit stops them), and a `finally` would run network calls with up to 90 s timeouts after the caller asked to cancel.
- **Takeaway:** cancellation-safe means "nothing is lost and a retry works", not "do everything anyway". Prefer retryable state over blocking a cancel.

---

## 3. Distributed state (Leases, retries, races with yourself)

### 3.1 Concurrent detach() on one handle got a 409
- **What happened:** two code paths (a signal handler and a `finally`, or two async tasks) called detach() at once. Both read the Lease, the first released it, and the second's write had a stale resourceVersion.
- **Issue:** the second call raised although the release had succeeded.
- **Fix:** on 409, re-read. If this handle no longer holds the Lease, treat the release as done (11/N).
- **Takeaway:** on an optimistic-concurrency conflict, re-read and check whether the goal is already met before failing.

### 3.2 Racing your own late write
- **What happened:** detach() cancels Lease renewal first, but an in-flight renewal (or a timed-out sync request) can still be applied by the server afterwards and bump the resourceVersion.
- **Issue:** after a 409, the re-read shows this handle still holding the Lease, and detach() raised for a benign race.
- **Fix:** retry the release once on the fresh Lease, then raise if it still fails (Vicente's suggestion on #1996).
- **Takeaway:** cancelling a request client-side does not cancel it server-side. After you stop a writer, assume one more of its writes may land. Retry exactly once, so real conflicts still surface.

### 3.3 Cleanup released (deleted) a batch the caller was handing off
- **What happened:** detach() failed at the Lease write and left the handle registered. Later client cleanup treated it as abandoned and called release(), which deletes the batch.
- **Fix:** cleanup retries detach() with the caller's original grace (stored on the handle), and never releases a half-detached handle. release() clears the stored grace, so the latest call wins.
- **Limits:** it is a safety net. Cleanup only runs on explicit close/delete_all, a context exit, or atexit with `cleanup=True`. Async atexit cannot await detach(). After a crash nothing runs, the Lease expires, and the reaper frees the batch. A late retry either completes the handoff or quietly finds the batch already reaped. `get_batch` then fails clearly (`BatchLeaseExpiredError` / `BatchNotFoundError`) rather than returning a cleaned-up batch.
- **Takeaway:** cleanup must distinguish "abandoned" from "in the middle of a handoff", and honour the caller's latest intent. Destructive defaults need an explicit check of what the caller was trying to do.

### 3.3b A shared field read twice across a lock gap
- **What happened:** detach() resolved the grace under the lock, then read the field again at write time. release() clears that field when it starts, and it can run from another thread or task while detach() is between its lock block and its write.
- **Issue:** detach() wrote a Lease with no duration. A retry loop added later read the field once more, which widened the window.
- **Fix:** copy the value into a local inside the lock block, and use the local for every write attempt (PR 2).
- **Takeaway:** a value decided under a lock must be carried in a local, not re-read from shared state after the lock is released. Every added retry or loop that re-reads shared state is a new race window.

### 3.4 Lease expiry is the leak guard; don't defeat it
- **Option rejected:** restart Lease renewal after a failed detach() so the batch stays alive. It would keep a batch held for the whole process lifetime even if nobody retries.
- **Takeaway:** a lease-based design needs something that eventually frees resources when the holder disappears. Don't add "keep alive on failure" paths that bypass it.

### 3.5 Library error wrapping is not what the docs or names suggest
- **What happened:** get_batch's docstring said a network failure raises `ApiException`.
- **Verified:** sync `kubernetes` wraps only an unretried urllib3 `SSLError` into `ApiException(status=0)`. With default retries, urllib3 raises `MaxRetryError` first (measured against an expired certificate). Async `kubernetes_asyncio` wraps nothing from aiohttp.
- **Also:** a TLS EOF (`ssl.SSLEOFError`) mid-stream is a dropped connection, not a certificate problem, so the watch retries it like a reset while other TLS failures stay fatal (8/N).
- **Takeaway:** verify which exceptions actually escape a client library by reading its code and, if cheap, by measuring. A plausible docstring fix (`ApiException ... or a TLS failure`) was wrong and got reverted.

---

## 4. Thread safety and registries

### 4.1 Check-then-act on a shared dict
- **What happened:** `if registry.get(key) is batch: del registry[key]` in a sync client used from several threads.
- **Issue:** another thread could register a new handle between the check and the delete, and the delete removed the new one.
- **Fix:** one lock around registration, the identity check plus delete, and cleanup snapshots. Async needs no lock when there is no await between check and act.
- **Takeaway:** under the GIL single dict operations are atomic, but two in a row are not. Make the lock cover every access to the structure (we missed `claim_batch` and a snapshot at first, so the lock rule was stated in half the places).

### 4.2 Deterministic race tests
- **What we did:** a dict subclass whose `get` or `clear` starts the competing operation on another thread and joins with a short timeout. With the lock, the other thread blocks. Without it, it interleaves, and the test fails every time.
- **Takeaway:** race tests can be deterministic if a test double triggers the interleaving at the exact point. Keep such doubles minimal, and adjust them only when the code under test legitimately changes (our double later had to race only on its first lookup).

---

## 5. Testing

- **Mutation-check every new or changed test.** Break the code it protects and confirm it fails for the intended reason. Several tests passed against broken code until checked: `assert_called_once` on an async mock passes without the await (fix: `assert_awaited_once`); a sync put-back had no owning test once a later PR rerouted cleanup; a no-grace retry branch had no test.
- **Fix the test double, never production code, when a double misbehaves.** A `MagicMock` attribute is truthy, so `if batch._connected` broke a test, and the fix was setting `_connected={}` on the mock. Earlier, production code had been widened to suit a mock; that was reverted.
- **One owning test per contract at the strongest boundary.** When a later PR reroutes a path (sync cleanup becoming release-based), re-check that each contract still has an owning test there.
- **Lint "changed lines only" can miss errors.** pycodestyle `--diff` reports E303 on the unchanged line after an added blank line, so it slipped through. Also run the check on the whole changed file for blank-line rules.
- **Measure before trusting results after an environment incident.** After a disk-full reboot we reran every check instead of reusing outputs that might be partial.

---

## 6. Process lessons (how the work went wrong and what fixed it)

### 6.1 Write the rule first, then check every path against it
- **What happened:** a cleanup fix grew over three rounds (close in `finally`, keep failed closes, take-out-and-put-back). Each round was reviewed against its own question. CodeRabbit then found two paths that broke the overall promise ("a failed close is always retried"): a detach() path we had scoped out, and overlapping cleanups that no verifier was asked about.
- **Takeaway:** for lifecycle or state changes, write the promise in one sentence, list every path that touches it (including concurrent interleavings and later PRs that reroute it), and give that list to the reviewer. Reviewers answer the questions they are given.

### 6.2 Never scope a gap out silently
- **What happened:** to keep a round small, a comment was narrowed instead of fixing a real gap.
- **Takeaway:** for every valid gap, either fold the fix into the owning PR or propose a separate PR, and let the owner decide. Narrowing a promise is a decision for the owner, not for the implementer.

### 6.3 Trace the end state before proposing a fix
- **What happened:** a proposed fix ("let the Lease expire") was justified with "the batch can still be re-attached". That contradicted an already verified fact: get_batch rejects a stale Lease.
- **Takeaway:** before proposing, trace what happens to each piece of state (Lease, claims, handle, registry) and what the user can do next, with a code reference for each step. Mark anything unchecked as inferred.

### 6.4 "It was like that before" is not a reason during design
- **What happened:** a reviewer comment was declined because "the merged 1/N code already does clear-then-close".
- **Takeaway:** while a design is still open, judge it on its merits and its documented promises. Precedent in your own unmerged or recent code is not evidence.

### 6.5 Verify identifiers and numbers
- **What happened:** a subagent invented a PR number ("#2052") in a paste-ready reply.
- **Takeaway:** anything that will be posted publicly (PR numbers, commit hashes, file:line) must be checked against the source, not recalled.

### 6.6 Recommendations: alternatives, costs, and holding a position
- **Takeaway:** list the alternatives with their cost and pick one. Hold that pick under questions. Change it only on a new fact, and name the fact. When wrong, say "I was wrong about X because Y". A question is not pushback.

### 6.7 Minimal fixes
- **Takeaway:** "smallest" means the smallest resulting code with each rule stated once, not the smallest diff. Before adding state, list the data already available. After applying any change, including a reviewer's suggestion taken literally, re-read the whole function and module for a condition now expressed twice. Example: adding `ssl.SSLEOFError` to a tuple duplicated a helper's own EOF branch.

### 6.8 Communicate in "what happens / issue / fix"
- **What happened:** a list of numbered findings without context confused the reader.
- **Takeaway:** explain each item as what is happening, the issue, and the proposed fix (or "being worked out"). Never refer to "finding 3" without restating it.

### 6.9 Verify a finding is a bug before proposing a PR for it
- **What happened:** a verifier noted that async `close()` did not take `_lifecycle_lock`, and a handoff recommended a separate PR to avoid scoping a finding out. Checking `__aexit__()`, `delete_all()`, and `aiohttp`/`httpx` afterwards showed that cluster-cleanup paths already waited on the lock and bare `close()` matched standard immediate transport teardown.
- **Takeaway:** "never scope things out" applies after a gap is verified to be real. Check caller entrypoints, the documented contract, and standard library practice before proposing a fix or follow-up PR, and recommend no change when the behavior is intended.

### 6.10 Audit cross-cutting lock invariants across the stack and against system bounds
- **What happened:** adding `_lifecycle_lock` to 12/N (`detach()` and `release()`) surfaced follow-on findings in 15/N (`release_*`) and 16/N (`acquire()`) one round at a time. A verifier finding that `detach()` stops `_renew_loop` while waiting behind a slow delete was also proposed as a fix before checking that `get_batch` rejects an expired Lease and the reaper margin (`leaseDurationSeconds + 60s >= 120s`) exceeds the 90s max delete retry time. And putting an exclusive `_lifecycle_lock` around `release_member` serializes concurrent single-member HTTP deletes.
- **Takeaway:** when introducing a new teardown lock at the base of a stack, audit every delete and teardown site across all PRs in one pass, check real timeout and TTL bounds before flagging a race as reachable, and check whether an exclusive lock serializes concurrent single-item I/O.

---

## 7. Stacked PRs and git

- **Stacked PRs show the whole stack's diff.** Every open PR targets the same base, so its diff and its bot review include all lower PRs. For each comment, find the owning commit (`git log -S`, blame), say which PR owns it, and fix it there.
- **Squash merges change hashes but keep trees.** After a merge, confirm the squash commit's tree equals the branch tree before restacking (`git rev-parse <squash>^{tree} <branch>^{tree}`).
- **Prove a restack is mechanical.** Check that every branch tree and every per-commit tree is identical, that `git range-diff` is all `=`, and that authors are clean. Only then sync without a new review.
- **`git rebase --update-refs` moves every local branch that points at rebased commits**, including old review copies. Delete stale local copies (after checking they are on the remote) first.
- **Review copies plus an atomic push with one lease per branch** (`git push --atomic --force-with-lease=<b>:<old> <new>:refs/heads/<b> ...`) keeps a long stack consistent: either every branch moves or none does.
- **A PR merged before its review fixes landed.** Put the fixes in a new small PR on the merged base, insert it into the series, and renumber the open PRs with a script that asserts exactly one "(k/N)" in the title and one ordinal in the body, then reads both back.
- **Semantic conflicts after reordering.** When a commit moves below others, later commits may delete symbols it uses. After any non-mechanical restack, grep for removed symbols and type-check every commit.
- **Wrap network git commands in `timeout` and verify with `git ls-remote`.** SSH agent warnings can appear even when the push succeeded.

---

## 8. Working with subagents

- Use a fresh, independent verifier for every non-trivial change. Give it the rule, the paths, and the base and head, but not your own findings.
- Ask the verifier for mutation measurements, not opinions ("removing X must fail a test").
- Ask explicitly: "Is there a smaller change with the same behavior? Does it add state, duplicate a rule, or widen a type?"
- Briefs must carry the binding rules and exact heads, and must say what not to touch (no live-branch pushes, no GitHub posts). Verify heads before acting, because server restarts can interrupt work midway.

### 7.1 A three-dot compare between stacked branches shows the whole PR
- **What happened:** a review link compared the live PR branch with its review copy using `compare/<live>...<copy>`. Both sit on the same upstream base, so GitHub diffed from that base and showed +3k lines instead of the ~1k under review.
- **Fix:** build a synthetic commit with the old commit as parent and the new tree (`git commit-tree`), push it as `review-diff/<topic>`, and check `git diff --shortstat` before sending.
- **Takeaway:** a three-dot compare diffs from the merge base, not from the left commit. For rewritten history always link a synthetic commit, and check the size first.

### 7.2 Re-check live stack heads and run a diff-of-diffs when multiple agents sync
- **What happened:** while a review round on 12/N and 15/N waited for approval, another agent pushed per-claim `_acquire_waiters` to 16/N through 19/N and added 20/N (`feat/batch-8-list-memory`) on top of the pre-review 15/N head.
- **Fix:** `git ls-remote` caught the moved and added branches before pushing. Rebasing the live 16/N through 20/N commits onto the new 15/N head and running `diff -u <(git diff old_top new_live_top) <(git diff our_old_top our_rebased_top)` caught two carry-forward hunks from the lower review round that would otherwise have been dropped.
- **Takeaway:** when multiple agents push to a stack, always run `git ls-remote` across all stack branches right before syncing, rebase concurrent upper-branch commits onto the new lower head, and verify zero dropped hunks with a diff-of-diffs before pushing atomically.

### 7.3 Commit messages that name a PR show up on that PR
- **What happened:** notes-branch commits such as "paste-ready replies for #1824 and #1988" appeared on those upstream PRs as "briankhoi added a commit that referenced this pull request".
- **Takeaway:** never put `#<number>` in a commit message on any branch of the fork. Refer to stack positions (13/N) instead, and grep new commit messages for `#[0-9]` before pushing.
