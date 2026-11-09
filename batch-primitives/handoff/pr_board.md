# PR board (shared tracking board)

Both agents (local and cloud) update the row of a PR when its state changes: status, head, upstream number, title or description. Keep this file in sync with `/mnt/project-files/pr-descriptions/pr_board.md` (cloud copy) and say what changed in the commit message. Brian opens PRs himself, so a row is "ready, not opened" until he gives the number.

Heads checked 2026-11-09 05:20 UTC after `git fetch origin` and a fetch of upstream `feature/batch-claim` (`98258d58`) and `main` (`993fd4aa`). The upstream API is unreachable from the cloud, so merged state comes from git. Upstream squash-merges, so a branch is not an ancestor of upstream. Merged means the squash commit's tree equals the branch tree (checked in `state.md`) and the upstream log shows the PR number. Always re-fetch, since heads move.

Status values: merged / open upstream #n / ready, not opened / planned. Owner: local agent (SDK stack) or cloud (RL work). Titles use the literal "(k/N)". Commits are authored by Brian Nguyen <brianknguyen@google.com> with no trailers.

## Board

| # | Branch | Title | Status | Head (10-23) | Owner |
| :-- | :-- | :-- | :-- | :-- | :-- |
| 1/N | `feat/batch-1-core` | feat(python-sdk): re-attach to a batch with get_batch (1/N) | merged #1742 (`3a1607bc`) | `1addb161` | local agent |
| 2/N | `feat/batch-1-fu1-watch-retries` | fix(python-sdk): keep the batch watch alive across network failures (2/N) | merged #1924 (`77c05c26`) | `cc4e8873` | local agent |
| 3/N | `feat/batch-1-fu2-lease-renewal` | fix(python-sdk): report batch Lease expiry on time and add lease_degraded() (3/N) | merged #1925 (`31a47728`) | `625d4a58` | local agent |
| 4/N | `feat/batch-1-fu3-member-state` | fix(python-sdk): replace Member's ready/terminal/lost flags with a state enum (4/N) | merged #1982 (`4bb0db30`) | `1ae848e3` | local agent |
| 5/N | `feat/batch-1-fu4-terminating-claims` | fix(python-sdk): treat a terminating batch claim as lost (5/N) | merged #1983 (`cc4a40bf`) | `2b8424a1` | local agent |
| 6/N | `feat/batch-1-fu5-connect-checks` | fix(python-sdk): check a batch member before returning its cached connection (6/N) | merged #1984 (`255d15fc`) | `e295ca75` | local agent |
| 7/N | `feat/batch-1-fu6-group-order` | fix(python-sdk): keep a batch's group order when re-attaching (7/N) | merged #1985 (`d07e8941`) | `6e966b1d` | local agent |
| 8/N | `feat/batch-ssl-eof-retry` | fix(python-sdk): retry the batch watch on a TLS EOF (8/N) | merged #2033 (`c56f0791`) | `0e586d47` | local agent |
| 9/N | `feat/batch-1-fu7-client-cleanup` | fix(python-sdk): close cached batch connections on client cleanup (9/N) | merged #1986 (`b03ba91b`, tree equals `a1e009ee`) | `a1e009ee` | local agent |
| 10/N | `feat/batch-1-fu7b-cleanup-retry` | fix(python-sdk): keep closing batch connections when cleanup fails (10/N) | merged #2049 (`74cd8fa5`, tree equals `078843dd`) | `078843dd` | local agent |
| 11/N | `feat/batch-1-fu9-concurrent-detach` (contains fu8 `feat/batch-1-fu8-task-names`, `2b1d9268`) | fix(batch,python-sdk): concurrent detach() success, named batch tasks for debugging (11/N) | merged #1996 (`7cfe8484`, tree equals `3e977d6a`) | `3e977d6a` | local agent |
| 12/N | `feat/batch-2-claim` (PR 2) | feat(python-sdk): add claim_batch and release (12/N) | merged #1824 (`522dad4c`, tree equals `93c1711a`) | `93c1711a` | local agent |
| 13/N | `feat/batch-3-group-consumers` (PR 3) | feat(batch,python-sdk): add events and group_outcomes (13/N) | merged #1988 (`8ad5a407`, tree equals `2d09db1e`) | `2d09db1e` | local agent |
| 14/N | `feat/batch-4-group-quorum` (PR 4) | feat(python-sdk): add wait_for_quorum (14/N) | merged #1989 (`3b57b191`, tree equals `823ae10e`) | `823ae10e` | local agent |
| 15/N | `feat/batch-5-release-group-76a8m3` (PR 5) | feat(python-sdk): add release_member, release_group and release_not_ready (15/N) | merged #1990 (`113fae28`, tree equals `9370f0b6`) | `9370f0b6` | local agent |
| 16/N | `feat/batch-6-acquire-nbbp5j` (PR 6) | feat(python-sdk): add acquire (16/N) | merged #1991 (`dc9ea439`, tree equals `692d7229`) | `692d7229` | local agent |
| 17/N | `feat/batch-transport-ltlfdm` | feat(python-sdk): connect batch members through their watched pod IP (17/N) | merged #2153 (`6feedc5c`, tree equals `1a0cee64`) | `1a0cee64` | local agent (fix: coordinator 2) |
| 18/N | `feat/batch-reaper-ymtd6t` | feat(python-sdk): add the batch reaper (18/N) | merged #2154 (`a97eeae7`, tree equals `73a16a7a`) | `73a16a7a` | local agent (types: coordinator 2) |
| 19/N | `feat/batch-7a-create-path` (PR 7, "7a") | feat(python-sdk): skip warm-pool create pacing and stop create retries on release (19/N) | merged #2166 (`5e379f8a`, tree equals `dcecf913`) | `dcecf913` | local agent |
| 20/N | `feat/batch-8-list-memory` (PR 8, "7b") | perf(python-sdk): page the claim list and stop parsing the deletecollection body (20/N) | merged #2175 (`28b839ba`, tree equals `50b690d0`) | `50b690d0` | local agent (coordinator 2) |
| 21/N | `feat/batch-9-types` (types PR) | refactor(python-sdk): replace Any with exact helper and Lease types in batch modules (21/N) | merged #2176 (`76d750e2`, tree equals `aa13ae7a`) | `aa13ae7a` | local agent (coordinator 2) |
| 22/N | `feat/batch-10-deletecollection-retry` (retry + TLA+ protocol fixes PR) | fix(python-sdk): harden batch Lease fencing, reaper wait, and custom-object retries (22/N) | **merged** upstream #2188 (`98258d58`, tree equals `d9df3a42`) | `d9df3a42` | local agent (coordinator 2) |
| 23/N | `feat/batch-11-acquire-annotations` (Task 7 SDK) | feat(python-sdk): pass pod_annotations in batch acquire (23/N) | open upstream #2189 (review copy `review/r-sweep/feat/batch-11-acquire-annotations` `2f709656` mechanically rebased on `98258d58`, awaiting sync approval) | `8a435b0f` (review `2f709656`) | local agent (coordinator 3) |
| 24/N | `feat/batch-12-lease-watch-fencing` (PR #2188 follow-ups + Lease/watch/pacing/complexity fixes) | fix(python-sdk): harden batch Lease takeover, watch restarts, and O(1) member lookups (24/N) | ready, not opened (pushed to `origin`, 8 commits on `2f709656`, awaiting review) | `05151df9` | local agent |
| 25/N | `feat/batch-13-client-handle-hardening` (client/handle lifecycle, `atexit`, PEP 561) | fix(python-sdk): harden batch client cleanup, handle connection close, and PEP 561 typing (25/N) | ready, not opened (pushed to `origin`, 4 commits on `05151df9`, awaiting review) | `7c59f18d` | local agent |
| 26/N | `merge/main-into-batch-claim-2` (renumbered from `24/N`, stacked on `25/N`) | merge(batch): merge main into feature/batch-claim (26/N) | open upstream #2194 (`review/r-sweep/merge/main-into-batch-claim-2` `68e6cab5` merging `7c59f18d` + `upstream/main` `993fd4aa` with `/label tide/merge-method-merge`, awaiting approval) | `51ba4e78` (review `68e6cab5`) | stack / coordinator 3 |
| 27/N | `feat/rl-batch-claim-mode` + `feat/rl-batch-hardening` (folded RL PR on `26/N`) | feat(agent-sandbox-rl): add hardened claim_mode="batch" with watchdog, Lease sweeps, and parallel scale-up (27/N) | ready, not opened (`review/r-sweep/feat/rl-batch-claim-mode` `1a6eac09` + `review/r-sweep/feat/rl-batch-hardening` `fc4b7101`, 14 commits on `68e6cab5`, awaiting approval) | `e84e964c` / `bb443e71` (review `1a6eac09` / `fc4b7101`) | local agent |
| RL | `feat/rl-sandbox-client-race` (1 commit on upstream main) | fix(rl): build Cluster.sandbox_client once under concurrent first use | **merged** upstream #2035 as squash `7f40dc1a` (independent of the stack, targets `main`) | `834656b9` | cloud |
| SDK fix | `fix/python-sdk-release-terminated-handles` (1 commit on upstream main `7f40dc1a`) | fix(python-sdk): untrack terminated sandbox handles from client registry | **merged** upstream #2085 as squash `7b1a9fbc` (Fixes #2082; merged by 2026-11-02; shepherd prompt `work/prompts/G7-pr-shepherd-2085-2087.md`) | `0a1eb20b` (CodeRabbit round 1 folded 2026-10-28) | cloud shepherd thread (item G) |
| SDK fix | `fix/python-sdk-free-closed-connectors` (1 commit on upstream main `6342c063`) | fix(python-sdk): break handle-to-connector reference cycle on close | **merged** upstream #2086 as squash `e7468986` (Fixes #2083; shepherd prompt `work/prompts/G7-pr-shepherd-2085-2087.md`) | `a996361e` (rebased on main 2026-11-02; tomergee nit answered, no change) | cloud shepherd thread (item G) |
| SDK fix | `fix/python-sdk-release-client-exit-hook` (1 commit on upstream main `6342c063`) | fix(python-sdk): avoid retaining closed clients in atexit cleanup hook | **merged** upstream #2087 as squash `ae6bb389` (Fixes #2084; shepherd prompt `work/prompts/G7-pr-shepherd-2085-2087.md`) | `4c391a53` (rebased on main 2026-11-02; tomergee items 2 and 3 folded) | cloud shepherd thread (item G) |

Notes
- fu8 has no PR of its own. It is folded into #1996 (fu9 branch contains it), so there are 10/N rows for two branches.
- 16/N to 19/N are inferred numbers. Renumber when Brian opens them.
- Bench branches are not PRs. `bench/batch-claim` holds the SDK harness, `bench/rl-claim-mode` and `bench/rl-claim-mode-async` the RL runs, `bench/rl-sandbox-client-race` the race measurement.
- 2026-10-23 22:59: the approved review rounds for #1986 and #1996 were synced. Because 9/N merged first, the #1986 follow-ups went into the new 10/N branch `feat/batch-1-fu7b-cleanup-retry`, and every branch above was restacked on it Brian opened it as #2049, and #1996, #1824, #1988, #1989, #1990 and #1991 were renumbered with `gh` to 11/N to 16/N at 23:04.
- The rows above supersede `refs/pr_table.md`, which is a stale 2026-10-19 snapshot kept for history.

## Tasks

Non-PR work items. Add a row when Brian asks, and fill the TBD cells only from his words.

| Task | Status | Owner | Detail |
| :-- | :-- | :-- | :-- |
| Cloud-cluster (GKE) benchmark of batch claiming vs per-claim | planned (Brian has the strategy and a plan with his local agent; not yet recorded here) | TBD (Brian) | see below |

### Cloud-cluster benchmark of batch claiming vs per-claim
- Why: on kind at 160 sandboxes per step the speed gain is small once the RL client race is fixed (measured: fixed sync per-claim 1.37 s vs batch 1.27 s). Kind also floors at controller adoption (about 1.08 s) and about 400 pods per node, so it cannot show scale effects. The cost wins are real on kind (measured: 1 watch vs 160, 1.5 vs 4.0 requests per env), but whether they become a speed win needs a real cluster (inferred).
- Compare: batch vs per-claim, sync and async, with the identical limiting mechanism on both arms and no-limit vs no-limit first (Brian 2026-10-21).
- Strategy: TBD (Brian)
- Scale (sandboxes per step, steps, runs): TBD (Brian)
- Cluster and project: TBD (Brian)
- Metrics: TBD (Brian)
- Owner: TBD (Brian)
- Prerequisites: the client race fix (`feat/rl-sandbox-client-race`) on the per-claim arm. The held controller-floor measurement (kind with more workers, GKE at 160 and 640) is the same item and folds in here.
- Results: not run

## Descriptions

Brian opens the PRs himself, so the attribution block is not included. Merged PRs keep their description for reference.

### 1/N (merged #1742)
This PR is the first in a series of PRs to implement a batch. It adds the handle for an existing batch.
- `SandboxClient.get_batch` and `AsyncSandboxClient.get_batch` take over the batch's Lease and return a handle.
- The handle exposes `members()`, `connect()`, `err()` and `detach()`, and keeps a watch and a Lease renewal running in the background.
- Nothing creates a batch yet. Tests attach to claims and a Lease created directly.

### 2/N fu1 (merged #1924)
This PR is the second in a series of PRs to implement a batch. It keeps the batch watch alive across network failures.
- A refused connection or failed DNS lookup (sync) and the watch's own timeout (async) are retried with the watch's backoff instead of ending it.
- Requests that had no timeout now have one, including the 410 re-list.

### 3/N fu2 (merged #1925)
This PR is the third in a series of PRs to implement a batch. It adjusts the expiry mechanism to ensure a handle never thinks it holds the batch's Lease after another client could take it over. Expiry is now timed from when the renewal was sent, renewal stops once the Lease expires, and Lease calls no longer retry silently, which stretched timeouts up to 4x. Renewals also stop reading the Lease before each write, so they make half the requests. A newly introduced `lease_degraded()` lets callers see renewal issues before the Lease expires.
- An empty `holderIdentity` counts as free.

### 4/N fu3 (merged #1982)
This PR is the fourth in a series of PRs to implement a batch. It replaces `Member`'s `ready`, `terminal` and `lost` flags with one `state`.
- `MemberState` is PENDING, READY, FAILED or LOST.
- A failed member stays FAILED if its claim is deleted, so its state still says why it never became Ready.

### 5/N fu4 (merged #1983)
This PR is the fifth in a series of PRs to implement a batch. It stops reporting a claim that is being deleted as Ready.
- The controller stops updating a terminating claim, so its Ready condition can stay True until it is gone.
- Such a claim now becomes LOST right away unless it already failed, and its pod IPs and FQDN are cleared, and its reason and message are cleared too.

### 6/N fu5 (merged #1984)
This PR is the sixth in a series of PRs to implement a batch. It makes `connect()` check a member before returning its cached connection.
- `connect()` raises `BatchError` once `err()` is set, since the Lease may be lost.
- A member that is no longer Ready raises `SandboxNotReadyError` even while its connection is cached.

### 7/N fu6 (merged #1985)
This PR is the seventh in a series of PRs to implement a batch. It keeps a batch's group order when `get_batch` re-attaches.
- Groups are ordered by their lowest claim ordinal, not by pool name.
- A claim without an ordinal sorts after numbered ones.
- Lease requests see a token that is rotated in place.

### 8/N TLS EOF (merged #2033)
This PR is the eighth in a series of PRs to implement a batch. It retries the batch watch when the connection ends with a TLS EOF.
- `ssl.SSLEOFError` joins the transport errors the watch retries, so a server that closes the TLS stream without a close_notify no longer ends the watch.
- The async watch uses the same helper as the sync one, so the EOF rule lives only in the error tuple.

### 9/N fu7 (merged #1986)
This PR is the ninth in a series of PRs to implement a batch. It makes client cleanup close what batches leave open.
- Cleanup closes each tracked batch's cached connections, which hold port-forward processes, and marks each batch detached.
- The async client's `close()` and `delete_all()` share one `_stop_batches()`, and its atexit hook closes batch connections.
- `detach()` unregisters only its own handle, so a newer handle for the same batch stays tracked.
- The sync client guards its tracked batches with a lock.

### 10/N fu7b (merged #2049)
This PR is the tenth in a series of PRs to implement a batch. It finishes the client cleanup follow-ups from #1986.
- Cleanup closes a batch's cached connections in a `finally`, so a failed stop can't leave port-forwards open.
- A connection whose close fails or is cancelled stays on the handle, and the batch stays tracked, so a later `close()` or `delete_all()` retries it.
- `close()` and `delete_all()` document that cleanup stops Lease renewal without releasing the Lease, so `get_batch` can't re-attach a batch that wasn't detached.
- `get_batch` documents the transport errors a failed request raises.

### 11/N fu8 + fu9 (merged #1996)
This PR is the eleventh in a series of PRs to implement a batch. It names the async batch tasks and lets two `detach()` calls on the same handle both succeed.
- The async handle's watch and Lease renewal start with `asyncio.create_task`, named `batch-<id>-watch` and `batch-<id>-renew`, so they are easy to tell apart in task dumps.
- Both `detach()` calls can read the Lease while this handle holds it, so the second release write used to fail with a 409 even though the batch was released. On a 409, `detach()` now reads the Lease again and only raises if this handle still holds it.

### 12/N PR 2 (merged #1824)
This PR is the twelfth in a series of PRs to implement a batch. It adds `claim_batch` and `release`.
- `claim_batch` prechecks each group's warm pool and template, creates the batch Lease, then creates claims in the background at `create_rps` with `max_in_flight` workers. Failed creates become `CreateFailed` members.
- Each group's first `min_ready` claims are created first, one group after another in group order, and the rest follow in the same order, so the first groups can be consumed while later groups are still being created.
- Every claim gets the same `shutdownTime`, which includes the pacing time, so pacing doesn't eat into `work_budget`.
- Retryable create errors are retried, and each retry takes a pacing slot. Creation stops once `err()` is set.
- `release()` stops the handle and deletes the batch's claims by label, then its Lease. It deletes in rounds and gives up only after rounds that make no progress. A failed release keeps the Lease, so calling it again retries.
- Before each round, `release()` takes the batch Lease with a conditional write. It raises `BatchInUseError` and deletes nothing if another handle holds the Lease, or if the Lease was created again or attached to since this handle last wrote it. It never creates a Lease. If the Lease is missing, it deletes the leftover claims. It deletes the Lease only at the resourceVersion it wrote.
- `detach()` and `release()` on one handle run one at a time, and the first call wins. `detach()` raises `BatchError` once a release has started, and a `release()` that ran inside `detach()` on the same thread makes that `detach()` raise.
- `delete_all()` and exit cleanup release every tracked batch. A handle whose `detach()` didn't finish is detached again with the caller's grace instead of released, and the async exit hook leaves that batch in place. A batch that another handle now holds is logged once and no longer tracked.
- `release()` clears the stored grace, so a release that fails after a failed detach is retried as a release. `detach()` writes the grace from a local copy, so another `detach()` call can't change it mid-write.
- A batch watch that ends within a second without an event backs off instead of reopening at once.
- A batch id that ends in a newline is rejected.
- An e2e test claims a two-member batch, runs a command in a member and releases it.

### 13/N PR 3 (merged #1988)
This PR is the thirteenth in a series of PRs to implement a batch. It adds the ways to consume a batch as it fills.
- `events()` yields member events as they happen.
- `group_outcomes()` yields each group once its outcome is known: its first `min_ready` Ready members, or an `error` (`QuorumUnreachableError` or `BatchTimeoutError`).
- Fill accounting is O(1) per watch event.

### 14/N PR 4 (merged #1989)
This PR is the fourteenth in a series of PRs to implement a batch. It adds `wait_for_quorum()`.
- It blocks until every group has `min_ready` Ready members and returns them in one list, or raises `QuorumUnreachableError` or `BatchTimeoutError`.
- Members beyond the quorum are handed out through `events()`.

### 15/N PR 5 (merged #1990)
This PR is the fifteenth in a series of PRs to implement a batch. It lets a caller free part of a batch.
- `release_member(member)` deletes one member's claim.
- `release_group(warmpool)` deletes a group's claims with label-scoped `deletecollection` rounds, using a hashed group label.
- `release_not_ready()` ends the fill and deletes every fill member that isn't Ready and was never handed out.
- Member deletes and claim creates share `max_in_flight`, releasing each slot before retry backoff, and `detach()`, `release()`, and async client cleanup wait for in-flight member and group deletes before stopping Lease renewal or releasing or deleting the Lease.

### 16/N PR 6 (merged #1991)
This PR is the sixteenth in a series of PRs to implement a batch. It adds `acquire()` so a caller can replace a released member.
- `acquire(warmpool, timeout=None)` adds one member from any warm pool in the namespace and blocks until it is Ready. It never leaves its claim behind. Concurrent first acquires check a pool once and share the outcome.
- `size=0` groups are allowed for pools filled only through `acquire()`.
- The Lease records labels, `create_rps` and the next ordinal, so a re-attached handle behaves like the original.

### 17/N Transport (merged #2153)
This PR is the seventeenth in a series of PRs to implement a batch. It makes fan-out cheaper and safer.
- `connect()` uses the pod IP the watch already knows, so it skips one Sandbox GET per member.
- A cached connection closes when its member is lost, failed, moves to another IP or is bound to another sandbox.
- `claim_batch` clamps an omitted `max_in_flight` down to `pool_size - 2` when the client's connection pool is smaller, and fails early when an explicit `max_in_flight` exceeds `pool_size - 2` or the pool holds fewer than 3 connections. `SandboxClient` and `AsyncSandboxClient` take `pool_size`.

### 18/N Reaper (merged #2154)
This PR is the eighteenth in a series of PRs to implement a batch. It cleans up batches whose driver died.
- Batch claims carry an ownerReference to their Lease, so the garbage collector deletes them once the Lease is gone.
- `python -m k8s_agent_sandbox.batch_reaper` runs once and finds Leases past their duration plus a skew margin. It deletes only those Leases, each with a resourceVersion precondition, and never touches claims, so its RBAC is `list`, `get` and `delete` on Leases. `--dry-run` only logs.

### 19/N PR 7 (merged #2166)
This PR is the nineteenth in a series of PRs to implement a batch. It skips `create_rps` pacing for claims that a warm pool's Ready sandboxes cover and stops pending create retries when a caller releases claims.
- `claim_batch` and `acquire()` read each pool's `status.readyReplicas` during the pool precheck and skip `create_rps` pacing for as many creates as the pool's Ready sandboxes cover. Claims beyond that count stay paced, `max_in_flight` still bounds concurrent creates, and a `429` create response pauses warm credit for numeric `Retry-After` values up to 30 s (with a 5 s minimum; missing or date-form headers use 5 s) while deducting paced creates from remaining credit so the batch backs off during active bursts and recovers warm speed afterward.
- When a claim created by this handle resolves its first controller status write with a cold-start `Ready=False` reason (`SandboxCreatePending`, `SandboxNotReady`, or `DependenciesNotReady`), the batch watch zeroes remaining warm credit for that pool immediately (sequence-fenced against concurrent pool refreshes) so shared warm pools fall back to `create_rps` without waiting for `status.readyReplicas` to update.
- Once callers spend the Ready count from a pool's precheck and `create_rps` pacing would delay the next create (`_next_create_at > now`), `acquire()` re-reads the pool's `status.readyReplicas` at most once per second per pool outside any `429` cooldown (unless an expired `timeout` cuts a read short before recording it). First-use pool checks, warm-credit refreshes, and creates all share `max_in_flight` and bound their waits by `acquire()`'s deadline without suppressing concurrent longer-deadline refreshes when a short-deadline read times out.
- Fill and `acquire()` create loops re-check detach, release, group failure, and watch-observed claims before and after waiting on `max_in_flight` and before retry backoffs and pacing-slot reservations, and never send past an expired `acquire()` deadline (excluding `BatchError` from async transport retries so `BatchTimeoutError` fails fast).
- `release_member()`, `release_group()`, and `release_not_ready()` stop pending create retries immediately, cleanup deletes an in-flight create retry that lands after `release_member()`'s delete, and `BatchState` sequences overlapping member deletes so an older delete finishing late cannot clear a newer failed cleanup delete.
- Measured on kind for the RL prototype, pacing never bound on an all-warm run (0 429s). Without this PR the batch loses to per-claim creates (3.37 vs 1.43 s).
- Updates `README.md`, docstrings, and the Python SDK reference.

### 20/N PR 8 (merged #2175)
This PR is the twentieth in a series of PRs to implement a batch. It lowers the memory peak of listing and releasing a large batch.
- `_claim_list_pages` lists claims in pages of 500 (client-go's page size). The first page's resourceVersion still starts the watch, and an expired continue token restarts the list once.
- `list_sandbox_claim_objects` drops `metadata.managedFields`, which nothing in the SDK reads, from each claim as its page arrives.
- The release rounds count the claims not yet being deleted page by page, freeing each page before fetching the next, instead of holding every remaining claim.
- `delete_sandbox_claims_by_label` drains raw `deletecollection` chunks unread without decompressing them, closes an unfinished sync response before `release_conn()` so a mid-stream interruption cannot return a dirty socket to the pool, and releases the connection in a `finally` block. The async helper raises `ApiException` for a non-2xx response itself (decoding non-UTF-8 error pages with replacement characters), because `kubernetes_asyncio` returns an unread response without checking its status.
- Measured on kind with 5,000 claims (sync): `release()` peak drops from 69.4 MB to 0.2 MB (and from 81.7 MB to 11.8 MB when deletecollection times out); claim-list peak drops from 69.4 MB to 48.7 MB with paging and to 24.1 MB (retained dicts 46.4 MB to 18.9 MB) after dropping `managedFields`. At 10,000 claims, `release()` peak drops from 138.8 MB to 9.9 MB and list peak from 138.9 MB to 43.6 MB. Release time is unchanged within noise.

### 21/N Types PR (merged #2176)
This PR is the twenty-first in a series of PRs to implement a batch. It replaces `Any` with exact helper and `V1Lease` types in the batch modules.
- Types the `helper` parameter on `_precheck_pools`, `_take_batch_lease`, `_delete_batch_objects`, and `_delete_claims_in_rounds` as `K8sHelper` in `sandbox_batch.py` and `AsyncK8sHelper` in `async_sandbox_batch.py`.
- Types `read_batch_lease`, `replace_batch_lease`, and `create_batch_lease` on `K8sHelper` and `AsyncK8sHelper`, along with `_write_renewal`, `_take_batch_lease`, and `_delete_batch_objects` on the batch handles, with `V1Lease` instead of `Any` (and narrows `BatchState.mark_lost`'s update dict to `dict[str, object]`).
- Adds overloaded `batch_utils.lease_parts(lease)` returning `(lease.metadata, lease.spec)` and raising `BatchError` if either is `None`, and handles `lease is None` up front in `_attach` so callers don't repeat `None` checks for fields the API server always populates.

### 22/N Lease fencing, reaper wait, and custom-object retry PR (merged #2188)
This PR is the twenty-second in a series of PRs to implement a batch. It stops `urllib3` from retrying sync batch custom-object requests, scopes post-Lease claim selectors to the Lease UID with local Lease expiry self-fencing, and makes the batch reaper wait a full Lease duration before deleting stale Leases.
- `kubernetes` leaves `configuration.retries` as `None` by default, so `urllib3` uses `Retry(3)`, which repeats connection failures and timed-out `GET` or `DELETE` requests three times inside one SDK call and sleeps up to 6 hours on `429`/`503` `Retry-After` headers before returning to batch callers. `K8sHelper` now shares the unretried `ApiClient` (`retries=False`) from `_api_client_without_retries(api_client)` between `coordination_v1_api` and `_batch_custom_objects_api`, routing all batch custom-object calls (`list_sandbox_claim_objects`, `count_live_sandbox_claims`, `delete_sandbox_claims_by_label`, `watch_sandbox_claims`, `get_sandbox_warmpool`, `get_sandbox_template`, and `batch=True` `create_sandbox_claim` / `delete_sandbox_claim`) through `_batch_custom_objects_api` so `SandboxBatch` owns its deadline- and Lease-bounded retry loops while sharing one `urllib3` connection pool. Non-batch calls keep default `urllib3` retries on `custom_objects_api`, and `AsyncK8sHelper` accepts `batch: bool = False` on `create_sandbox_claim` and `delete_sandbox_claim` for parity because `aiohttp` does not retry requests.
- Batch claims now carry `agents.x-k8s.io/batch-lease-uid` (`lease.metadata.uid`), scoping post-Lease `get_batch`, `_watch_loop`, `release_group`, and `release` / client-cleanup `deletecollection` selectors to `(batch-id, batch-lease-uid)` so a stalled `release()` or re-attached watch never touches a newer batch reusing `batch_id` (while pre-Lease `claim_batch` collision checks and `lease is None` `get_batch` checks stay on `batch-id`). `claim_batch` also bounds its pre-existing claim list by `deadline = renewed_at + lease_duration`, raises `BatchLeaseExpiredError` if setup crosses `lease_expires_at`, and fences rollback `delete_batch_lease` with `resource_version=created_rv`. Both shells check `_lease_expires_at` across in-flight create/wait/consumer loops, keep `lease_degraded()` sticky once expired, and bound claim and pool request timeouts by the remaining Lease budget.
- `batch_reaper.reap()` now waits each candidate Lease's full `leaseDurationSeconds` (`StaleLease.lease_duration`) instead of `renew_interval`, aligning its confirmation wait with the driver's `sent_at + leaseDurationSeconds` self-fencing deadline, and documents the stalled-driver clock-skew bound and CronJob `activeDeadlineSeconds` sizing in `batch_reaper.py` and `README.md`.

### 23/N Acquire annotations PR (open #2189)
This PR is the twenty-third in a series of PRs to implement a batch. It lets callers pass pod annotations when acquiring a batch member, bounds `acquire()`'s warm-pool precheck by `timeout` and Lease expiry, and documents batch error handling.
- `SandboxBatch.acquire` and `AsyncSandboxBatch.acquire` accept keyword-only `pod_annotations: dict[str, str] | None = None`, validate them with `build_pod_metadata(None, pod_annotations)` before touching the cluster, and set `spec.additionalPodMetadata` on the created `SandboxClaim` across create retries (omitting it when `None` or empty).
- `_check_pool()` and `_precheck_pools()` bound each `max_in_flight` wait, follower wait, and warm-pool or template `GET` by both `acquire(timeout=...)` and live Lease expiry, re-checking handle and group state before classifying a timeout and cancelling the shared single-flight future on caller-local `BatchError` so concurrent acquires with remaining budget can take over.
- Narrows `GroupOutcome.error` to `BatchError | None`, documents the `BatchError` / `SandboxError` / pass-through API and transport error contract in `README.md` and `acquire()` docstrings, and regenerates `docs/python_sdk_reference.md`.

### 24/N Lease takeover, watch restart, and O(1) lookup PR (ready, not opened)
Branch `feat/batch-12-lease-watch-fencing` (`05151df9`), 8 commits pushed to `origin` on rebased `23/N` (`2f709656`), targeting `feature/batch-claim`.
- Title: `fix(python-sdk): harden batch Lease takeover, watch restarts, and O(1) member lookups (24/N)`.
- Decouples terminal Lease loss (`_lease_error`) from fatal watch errors (`BatchState.error()`) while keeping `batch.err()` shared, so Lease renewal and member cleanup keep running while the handle holds the Lease, and closes active watch streams cleanly on teardown and Lease loss (`Watch.stop()` + bounded join in sync, `contextlib.aclosing` in async).
- Deletes late fill claims arriving on the watch or re-list after `release_not_ready()` or `release_group()` stops the creator, records timed-out `_waiting` group members in `_resolved` so `fill_settled()` never stalls, and clamps `release_group()` `deletecollection` rounds and live claim counts to `_lease_expires_at`.
- Checks connection-pool headroom and clamps Lease takeover expiry in `get_batch()`, handles `404` `replace_batch_lease` races across attach, detach, and release, drains in-flight dynamic `acquire()` creates before `release()` or `detach()`, and restarts `batch_reaper.find_stale_leases` pagination on `410 Gone` while isolating malformed `renewTime` entries per Lease.
- Adds `O(1)` `SandboxBatch.get_member(claim_name)` / `AsyncSandboxBatch.get_member(claim_name)`, indexes `BatchState` and planned/pacing fill creates per warm pool, stores `_stale_connections` as an insertion-ordered `dict`, wakes only the released pool's `acquire()` waiters in `release_group()`, accepts `ConnectTimeoutError` on BSD/macOS non-listening socket tests, and regenerates `docs/python_sdk_reference.md`.

### 25/N Client cleanup, handle connection close, and PEP 561 typing PR (ready, not opened)
Branch `feat/batch-13-client-handle-hardening` (`7c59f18d`), 4 commits pushed to `origin` on `24/N` (`05151df9`), targeting `feature/batch-claim`.
- Title: `fix(python-sdk): harden batch client cleanup, handle connection close, and PEP 561 typing (25/N)`.
- Validates HTTP status in `AsyncK8sHelper.watch_sandbox_claims` so non-2xx responses raise `ApiException` with `Retry-After` headers preserved, exposes local `K8sHelper.api_pool_size()` / `AsyncK8sHelper.api_pool_size()`, shares one owned `ApiClient` across `K8sHelper` custom and core APIs, and closes owned pools on `K8sHelper.close()` and client cleanup.
- Coordinates `AsyncSandboxClient.close()` with concurrent `claim_batch()` and `get_batch()`, snapshots tracked sandboxes before awaiting `close_connection()`, serializes `detach()` and `_stop_batches()` on `batch._lifecycle_lock` while retaining unreleased batches whose `release()` started and failed, and guards `SandboxClient` batch connection reads under `batch._lock`.
- Evicts closed batch-connected `Sandbox` / `AsyncSandbox` handles from `_connected` via weakref-backed callbacks without reference cycles, skips inactive cached handles in `connect()`, and routes `terminate()` on batch-connected handles through Lease-fenced `release_member()`.
- Declares explicit `__all__` exports in `k8s_agent_sandbox/__init__.py` so strict `mypy --no-implicit-reexport` consumers type-check cleanly.

### 26/N Branch sync merge PR (open #2194)
Branch `merge/main-into-batch-claim-2` (review copy `review/r-sweep/merge/main-into-batch-claim-2` `68e6cab5` merging `25/N` `7c59f18d` + `upstream/main` `993fd4aa`). Open with `/label tide/merge-method-merge`.
- Merges `main` into `feature/batch-claim`, combining `main`'s static `atexit` client cleanup (`#2087`), closed-connector cycle breaking (`#2086`), and terminated-handle eviction (`#2085`) with `25/N` batch client cleanup and handle eviction.
- Forwards `client.file_config` (`#2041`) and `AsyncSandboxClient._default_ssl_context` (`#2132`) in `SandboxBatch.connect()` and `AsyncSandboxBatch.connect()`.
- Regenerates `docs/python_sdk_reference.md` with `main`'s 4-level heading `sed` rule.

### 27/N RL batch claim mode and GKE scale-up hardening (ready, not opened)
Branches `feat/rl-batch-claim-mode` + `feat/rl-batch-hardening` (review copies `review/r-sweep/feat/rl-batch-claim-mode` `1a6eac09`, 4 commits, + `review/r-sweep/feat/rl-batch-hardening` `fc4b7101`, 10 commits, stacked on `26/N` `68e6cab5`), targeting `feature/batch-claim` under `examples/agent-sandbox-rl/`. Fold into a single RL PR when opening upstream.
- Title: `feat(agent-sandbox-rl): add hardened claim_mode="batch" with watchdog, Lease sweeps, and parallel scale-up (27/N)`.
- `FleetConfig(claim_mode="batch")` routes `fleet.acquire` and `fleet.release` through a per-cluster SDK batch (Design C, epoch-fenced against concurrent teardown), forwards snapshot `pod_annotations` through `batch.acquire(..., pod_annotations=...)` (`23/N`), rotates recycled batch claims before `claim_expires_at` (with sub-second ceiling alignment), and uses `O(1)` `batch.get_member` in `BatchClaim.still_reaches` and `O(1)` `id(handle)` indexing in `SandboxFleet._handles`.
- Adds `BatchWatchdog` so `FleetConfig.fail_fast` inspects cold-start pending claims via `O(1)` idle `_acquire_waiters` checks and batched cluster-wide `LIST`s, failing broken images or configs early via `batch.release_member` with `FailFastError`.
- Checks dry-run `create_namespaced_lease` RBAC in `preflight()` (treating missing namespaces as `warn_only`), sweeps batch `Lease`s (`agents.x-k8s.io/batch-id` + run-id label) in `reap(run_id=...)`, and verifies `Lease` cleanup in `teardown()` / `verify_teardown()`.
- Parallelizes `acquire_batch()` and `release_all()` (with `BaseException`-safe rollback, async cancellation worker-thread reclaim, and `FakeFleet` parity), skips repeat `ensure_template` / `ensure_warmpool` RPCs on cached on-demand warm pools, shares each cluster's `CoreV1Api` for REST pod status reads, auto-sizes cluster `urllib3` connection pools (including `_batch_custom_objects_api` and `coordination_v1_api`), and raises `AsyncSandboxFleet`'s executor ceiling to `4096`.
- Reconnects watch-evicted sandboxes cleanly in `SandboxHandle.refresh()`, cleans up snapshot triggers and fallback connections on batch `release()`, bounds fallback direct claim deletes, surfaces a `work_budget` hint with `SandboxLostError(expired=True)` after `claim_expires_at`, and updates `README.md` + `CHANGELOG.md`.

### SDK fix: terminated-handle memory leak (merged #2085, #2086, #2087; targets `main`, not in the batch stack)
Owner: coordinator 1 (item G). A long-running async driver grew from 94 MB to 1.75 GB over 2,400 sandboxes (measured, `bench/async-mem-probe`). With all three PRs, growth per 100 sandboxes went from 90.1 MB to 0.46 MB, and memory kept per terminated sandbox from 0.667 MB to 0.0001 MB (measured by G, verified by coordinator 1's checker).
- PR 1 (#2085, Fixes #2082, merged `7b1a9fbc`): `terminate()` removes its own registry entry through a hook bound to the registry (not the client). Both clients.
- PR 2 (#2086, Fixes #2083, merged `e7468986`): a closed handle's connector drops its links back to the handle, so it is freed without the cyclic GC. Async in the connector's final `close()`, sync in `Sandbox.close_connection()`.
- PR 3 (#2087, Fixes #2084, merged `ae6bb389`): the exit hook holds only the registry data, not the client, so closed clients are freed; exit cleanup unchanged. Both clients.
- Filed issue and PR texts: `work/results/issues-final.md`. Shepherd prompt for #2085 to #2087: `work/prompts/G7-pr-shepherd-2085-2087.md`.

### RL sandbox client race (merged #2035)
Branch `feat/rl-sandbox-client-race`, 1 commit merged on upstream `main` as `7f40dc1a` (`834656b9`). Draft: `/mnt/project-files/pr-descriptions/rl_sandbox_client_race.md`.
- Title: fix(rl): build Cluster.sandbox_client once under concurrent first use.
- `Cluster.sandbox_client` was built lazily with no lock. With 160 concurrent first acquires the sync fleet built 64 to 118 SandboxClients, and every build loads the kubeconfig. It now builds under a per-cluster lock with a double check, and later calls take the unlocked fast path.
- Test: `test_concurrent_first_sandbox_client_builds_one_client` starts 16 threads on a barrier and asserts one build. It fails without the lock (16 builds) and without the inner check. 142 tests in the touched files pass.
- Measured on kind, 3 runs per arm, ranges do not overlap: clients built 160 to 1, last start 4.22 to 1.37 s (`bench/rl-sandbox-client-race`).
