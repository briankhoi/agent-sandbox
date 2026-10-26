# PR board (shared tracking board)

Both agents (local and cloud) update the row of a PR when its state changes: status, head, upstream number, title or description. Keep this file in sync with `/mnt/project-files/pr-descriptions/pr_board.md` (cloud copy) and say what changed in the commit message. Brian opens PRs himself, so a row is "ready, not opened" until he gives the number.

Heads checked 2026-10-23 00:30 UTC after `git fetch origin` and a fetch of upstream `feature/batch-claim` (`c56f0791`) and `main` (`840fdfde`). The upstream API is unreachable from the cloud, so merged state comes from git. Upstream squash-merges, so a branch is not an ancestor of upstream. Merged means the squash commit's tree equals the branch tree (checked in `state.md`) and the upstream log shows the PR number. Always re-fetch, since heads move.

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
| 11/N | `feat/batch-1-fu9-concurrent-detach` (contains fu8 `feat/batch-1-fu8-task-names`, `2b1d9268`) | fix(batch,python-sdk): concurrent detach() success, named batch tasks for debugging (11/N) | open upstream #1996 | `3e977d6a` | local agent |
| 12/N | `feat/batch-2-claim` (PR 2) | feat(python-sdk): add claim_batch and release (12/N) | open upstream #1824 | `3aad6b2e` | local agent |
| 13/N | `feat/batch-3-group-consumers` (PR 3) | feat(python-sdk): add events and iter_ready_groups (13/N) | open upstream #1988 | `180de8db` | local agent |
| 14/N | `feat/batch-4-group-quorum` (PR 4) | feat(python-sdk): add wait_for_quorum (14/N) | open upstream #1989 | `71fa54ab` | local agent |
| 15/N | `feat/batch-5-release-group-76a8m3` (PR 5) | feat(python-sdk): add release_member, release_group and release_not_ready (15/N) | open upstream #1990 | `cca9ae2c` | local agent |
| 16/N | `feat/batch-6-acquire-nbbp5j` (PR 6) | feat(python-sdk): add acquire (16/N) | open upstream #1991 | `af6c4c74` | local agent |
| 17/N | `feat/batch-transport-ltlfdm` | feat(python-sdk): connect batch members through their watched pod IP (17/N) | ready, not opened (mypy fix approved 2026-10-23: one-line `cast` folded into the pool commit; prompt `work/prompts/B2-types-fold.md`, review copies `review/types/*`, not run yet; the stack conversation syncs) | `c6951c81` | local agent (fix: coordinator 2) |
| 18/N | `feat/batch-reaper-ymtd6t` | feat(python-sdk): add the batch reaper (18/N) | ready, not opened (B2 folds the reaper's own loose types into its commits: `lease_owner_reference`, `owner_references`, the Lease list kwargs) | `547ce80c` | local agent (types: coordinator 2) |
| 19/N | `feat/batch-7a-create-path` (PR 7, "7a") | feat(python-sdk): skip create and acquire pacing for claims a warm pool covers (19/N) | ready, not opened | `f38a3651` | local agent |
| 20/N | not created (PR 8, "7b") | perf(python-sdk): page the claim list and stop parsing the deletecollection body (20/N) | planned (plan `work/pr8_plan.md` verified 2026-10-22, accept with fixes; Part 3 approved, other decisions pending; starts after B2 syncs) | n/a | local agent (coordinator 2) |
| 21/N | not created (types PR) | not drafted | planned (Brian approved 2026-10-23: one PR after PR 7 for loose types in open PRs and upstream code, including Lease typing; plan `work/type_tightening_plan.md`) | n/a | local agent (coordinator 2) |
| RL | `feat/rl-batch-claim-mode` (4 commits on PR 7) | feat(agent-sandbox-rl): add claim_mode="batch" to claim through the SDK batch | ready, not opened (needs Brian's review; sits on 18/N so it opens after the stack) | `166a15c8` | cloud (re-commit as Brian by local agent) |
| RL | `feat/rl-sandbox-client-race` (1 commit on upstream main) | fix(rl): build Cluster.sandbox_client once under concurrent first use | ready, not opened (independent of the stack, targets `main`) | `834656b9` | cloud |
| SDK fix | `fix/python-sdk-release-terminated-handles` (1 commit on upstream main `96ef188e`) | fix(python-sdk): drop terminated sandboxes from the client (#<issue A>) | ready, not opened (targets `main`; terminate() hook, see `work/results/G3-memleak-fix2.md`) | `c2c006ee` | local agent (item G) |
| SDK fix | `fix/python-sdk-free-closed-connectors` (1 commit on upstream main `96ef188e`) | fix(python-sdk): free closed sandbox handles without the cyclic GC (#<issue B>) | ready, not opened (targets `main`; see G3) | `b3574fa9` | local agent (item G) |
| SDK fix | `fix/python-sdk-release-client-exit-hook` (not created) | fix(python-sdk): stop the exit hook from keeping closed clients alive (#<issue 4>) | planned, waiting for Brian's option choice (G3) | n/a | local agent (item G) |

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

### 11/N fu8 + fu9 (open #1996)
This PR is the eleventh in a series of PRs to implement a batch. It names the async batch tasks and lets two `detach()` calls on the same handle both succeed.
- The async handle's watch and Lease renewal start with `asyncio.create_task`, named `batch-<id>-watch` and `batch-<id>-renew`, so they are easy to tell apart in task dumps.
- Both `detach()` calls can read the Lease while this handle holds it, so the second release write used to fail with a 409 even though the batch was released. On a 409, `detach()` now reads the Lease again and only raises if this handle still holds it.

### 12/N PR 2 (open #1824)
This PR is the twelfth in a series of PRs to implement a batch. It adds `claim_batch` and `release`.
- `claim_batch` prechecks each group's warm pool and template, creates the batch Lease, then creates claims in the background at `create_rps` with `max_in_flight` workers. Failed creates become `CreateFailed` members.
- Each group's first `min_ready` claims are created first, one group after another in group order, and the rest follow in the same order, so the first groups can be consumed while later groups are still being created.
- `release()` stops the handle and deletes the batch's claims by label, then its Lease.
- `delete_all()` and exit cleanup release every tracked batch.

### 13/N PR 3 (open #1988)
This PR is the thirteenth in a series of PRs to implement a batch. It adds the ways to consume a batch as it fills.
- `events()` yields member events as they happen.
- `iter_ready_groups()` yields each group once it has `min_ready` Ready members, or raises when it can't.
- Fill accounting is O(1) per watch event.
- Pending rename (Brian approved 2026-10-22): `iter_ready_groups`/`GroupReady` become `group_outcomes`/`GroupOutcome`. Update this row and title when the local agent applies it.

### 14/N PR 4 (open #1989)
This PR is the fourteenth in a series of PRs to implement a batch. It adds `wait_for_quorum()`.
- It blocks until every group has `min_ready` Ready members and returns them in one list, or raises `QuorumUnreachableError` or `BatchTimeoutError`.
- Members beyond the quorum are handed out through `events()`.

### 15/N PR 5 (open #1990)
This PR is the fifteenth in a series of PRs to implement a batch. It lets a caller free part of a batch.
- `release_member(member)` deletes one member's claim.
- `release_group(warmpool)` deletes a group's claims with one label-scoped `deletecollection`, using a hashed group label.
- `release_not_ready()` ends the fill and deletes every fill member that isn't Ready and was never handed out.

### 16/N PR 6 (open #1991)
This PR is the sixteenth in a series of PRs to implement a batch. It adds `acquire()` so a caller can replace a released member.
- `acquire(warmpool, timeout=None)` adds one member from any warm pool in the namespace and blocks until it is Ready. It never leaves its claim behind. Concurrent first acquires check a pool once and share the outcome.
- `size=0` groups are allowed for pools filled only through `acquire()`.
- The Lease records labels, `create_rps` and the next ordinal, so a re-attached handle behaves like the original.

### 17/N Transport (ready, not opened)
This PR is the seventeenth in a series of PRs to implement a batch. It makes fan-out cheaper and safer.
- `connect()` uses the pod IP the watch already knows, so it skips one Sandbox GET per member.
- A cached connection closes when its member is lost, failed, moves to another IP or is bound to another sandbox.
- `claim_batch` fails early when the client's connection pool is smaller than `max_in_flight + 2`. `SandboxClient` and `AsyncSandboxClient` take `pool_size`.

### 18/N Reaper (ready, not opened)
This PR is the eighteenth in a series of PRs to implement a batch. It cleans up batches whose driver died.
- Batch claims carry an ownerReference to their Lease, so the garbage collector deletes them once the Lease is gone.
- `python -m k8s_agent_sandbox.batch_reaper` runs once and finds Leases past their duration plus a skew margin. It deletes only those Leases, each with a resourceVersion precondition, and never touches claims, so its RBAC is `list`, `get` and `delete` on Leases. `--dry-run` only logs.

### 19/N PR 7 (ready, not opened)
This PR is the nineteenth in a series of PRs to implement a batch. It stops pacing claims that a warm pool already covers.
- `claim_batch` and `acquire` read the pool's `status.readyReplicas` in the precheck and skip `create_rps` pacing for as many claims as there are ready warm sandboxes. Claims beyond that stay paced, and `max_in_flight` still bounds creates.
- Measured on kind for the RL prototype, pacing never bound on an all-warm run (0 429s). Without this PR the batch loses to per-claim creates (3.37 vs 1.43 s).
- Docstrings and the SDK reference describe warm credit.

### 20/N PR 8 (planned, no code)
Planned scope from `work/pr8_plan.md`.
- Page `list_sandbox_claim_objects` at `limit=500` inside the helper, following `continue`, with a restart on an expired token.
- Make the `release()` re-list metadata-only, and stop parsing the `deletecollection` response body.
- The `members()` cache and wake-up items stay dropped.
- Expected win is the transient part of the 69 MB peak at 5,000 claims (inferred, not measured). Needs before and after memory numbers.
- Update 2026-10-23 (coordinator 2): the plan's new reading is that the `release()` peak is the deletecollection response, not the re-list (inferred from timing, to be measured). Async sends the metadata-only header on the existing client; the sync route (a dedicated client like `_api_client_without_retries`) awaits Brian. The pool check `max_in_flight + 2` needs no change (verified). Decisions: `work/coordinator2_decisions.md`.

### 21/N Types PR (planned, no code)
Not drafted. Replaces `Any` with exact types where an exact type exists, for code in the open PRs (typed `helper` parameters from PR 2, 5, 6) and in upstream code, including the Lease methods as `V1Lease` with a policy for a Lease without `spec`. Types introduced by transport, the reaper and PR 7 are folded into those branches instead (B2). Plan: `work/type_tightening_plan.md`.

### SDK fix: terminated sandbox handles (in progress, not opened, targets `main`)
Draft; final text comes with the fix. The async client keeps every sandbox handle after `terminate()`, about 0.7 MiB each (an HTTP client with an SSL context), so a long-running driver grew from 94 MB to 1.75 GB over 2,400 sandboxes (measured, `bench/async-mem-probe`). The sync client keeps handles the same way (about 0.06 MiB each).
- Planned shape (pending Brian's decision): `terminate()` removes its own entry from the client after a successful delete; a second change clears the handle-to-connector references at final close so a dropped handle is freed without waiting for a full GC.
- Upstream issue text is drafted in `work/results/G2-memleak-decision.md` and `work/results/leak-fix-audit.md`; Brian files it.

### RL batch claim mode (ready, not opened)
Branch `feat/rl-batch-claim-mode`, 4 commits on 18/N. Targets the RL library under `examples/agent-sandbox-rl/`.
- Title: feat(agent-sandbox-rl): add claim_mode="batch" to claim through the SDK batch.
- `FleetConfig(claim_mode="batch")` makes `fleet.acquire` and `fleet.release` claim through a per-cluster SDK batch (Design C, approved 2026-10-21). The default stays per-claim.
- Recycled batch claims rotate before they expire (RL Lease default 300 s, approved 2026-10-21).
- Documented in the RL README.
- Results, kind, 160 sandboxes per step (measured): sync batch 1.27 s, async batch 1.32 s (0 errors, no leaks), so the async path needs no changes. Async per-claim 1.75 s, sync per-claim 3.88 s. The 3.1x sync headline is inflated by the client race below, so the honest no-limit win is about 1.3x (inferred). Cost wins stand: 243 vs 640 API requests, 1 vs 160 watches. The batch is at its floor on kind (requests done at about 0.4 s, controller adoption to 1.08 s).
- Detail: `rl-design/async_speed_gap.md`, `rl-design/rl_design_decision.md`.

### RL sandbox client race (ready, not opened)
Branch `feat/rl-sandbox-client-race`, 1 commit on upstream `main` (`834656b9`). Draft: `/mnt/project-files/pr-descriptions/rl_sandbox_client_race.md`.
- Title: fix(rl): build Cluster.sandbox_client once under concurrent first use.
- `Cluster.sandbox_client` was built lazily with no lock. With 160 concurrent first acquires the sync fleet built 64 to 118 SandboxClients, and every build loads the kubeconfig. It now builds under a per-cluster lock with a double check, and later calls take the unlocked fast path.
- Test: `test_concurrent_first_sandbox_client_builds_one_client` starts 16 threads on a barrier and asserts one build. It fails without the lock (16 builds) and without the inner check. 142 tests in the touched files pass.
- Measured on kind, 3 runs per arm, ranges do not overlap: clients built 160 to 1, last start 4.22 to 1.37 s (`bench/rl-sandbox-client-race`).
