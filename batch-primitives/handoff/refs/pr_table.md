# Batch claiming PR stack: table, titles and descriptions

Bottom to top, 16 PRs plus PR 7, each diffed against the one below it. Branch heads checked after `git fetch origin` on 2026-10-19, after PR 1 merged upstream as 3a1607b and the post-merge fixes: fu1 cc4e887, fu2 2d71b26, fu3 08aaa75, fu4 4f82252, fu5 728a159, fu6 c8b58ce, fu7 9cfd4ea, fu8 b3b29ec, fu9 4cbc6c1, PR 2 a242cf8, PR 3 1f14246, PR 4 4ea07cf, PR 5 2d2a4e7, PR 6 8a72827, transport 6207034, reaper 44fb487.

Comment sources are Janet's five comments on #1742 (J1 to J5), CodeRabbit's four (C1 to C4), and the manager's feedback. The manager's items are the two err() and Lease-staleness items folded into fu2 and the group-release feedback of 2026-10-13 (`design/pr3_feedback_and_pr1_followups.md`). C2, C3 and C4 were rejected with reasons, so no PR addresses them.

## Table

| # | Branch | Suggested title | What it adds or fixes | Review comments addressed |
| :-- | :-- | :-- | :-- | :-- |
| 1 | `feat/batch-1-core` (#1742) | feat(python-sdk): re-attach to a batch with get_batch (1/N) | Batch models, constants and exceptions. Claim list/watch and Lease helpers. I/O-free batch state. `SandboxBatch`/`AsyncSandboxBatch` with `get_batch`, `members()`, `connect()`, `err()`, `detach()`, a watch and a Lease renewal loop. Module logger. | Is the PR the comments are on (J1 to J5). |
| 2 | `feat/batch-1-fu1-watch-retries` | fix(python-sdk): keep the batch watch alive across network failures (2/N) | The watch retries `MaxRetryError` (sync) and `TimeoutError` (async) instead of dying. Request timeouts on the Lease read, claim list, takeover write, `detach()` and the 410 re-list. | None (Brian's best-practice review of PR 1). |
| 3 | `feat/batch-1-fu2-lease-renewal` | fix(python-sdk): report batch Lease expiry on time and add lease_degraded() (3/N) | Expiry timed from before `renewTime` is stamped. Renewal wakes at expiry, reports it through `err()` and stops. Renewals write without reading first. New `lease_degraded()`. Sync Lease calls no longer retry silently. Empty `holderIdentity` counts as free. | J4 (renewal stops after expiry). Manager item 4 (`is_lease_stale` docstring on which way the margin goes). |
| 4 | `feat/batch-1-fu3-member-state` | fix(python-sdk): replace Member's ready/terminal/lost flags with a state enum (4/N) | `Member.state` as `MemberState` (PENDING, READY, FAILED, LOST). A failed member stays FAILED when deleted. | None (Brian's best-practice review). |
| 5 | `feat/batch-1-fu4-terminating-claims` | fix(python-sdk): treat a terminating batch claim as lost (5/N) | A claim with a `deletionTimestamp` becomes LOST unless it already failed, and its pod IPs and FQDN are cleared. | J3 (terminating claims reported Ready). |
| 6 | `feat/batch-1-fu5-connect-checks` | fix(python-sdk): check a batch member before returning its cached connection (6/N) | `connect()` raises `BatchError` once `err()` is set, then checks the member is READY, before using the cache. | J1 and J2 (readiness before the cached connection). Manager item 1 (`BatchError` once `err()` is set, even for a cached member). |
| 7 | `feat/batch-1-fu6-group-order` | fix(python-sdk): keep a batch's group order when re-attaching (7/N) | `get_batch` orders groups by lowest claim ordinal instead of pool name. A claim without an ordinal sorts last. | None (Brian's best-practice review). |
| 8 | `feat/batch-ssl-eof-retry` (#2033) | fix(python-sdk,batch): retry the batch watch on a TLS EOF (8/N) | `ssl.SSLEOFError` is retried like a connection reset in both shells, through one `_is_transport_error` per shell that the watch uses. Certificate and other TLS failures stay fatal. | None (Brian's best-practice review). |
| 9 | `feat/batch-1-fu7-client-cleanup` | fix(python-sdk): close cached batch connections on client cleanup (9/N) | Client cleanup closes each tracked batch's cached connections. Async `close()` and `delete_all()` share `_stop_batches()`, and async atexit closes batch connections. `_unregister_batch` removes only its own handle. | J5 (cleanup leaks connections). C1 (async `_stop_batches()`). |
| 10 | `feat/batch-1-fu9-concurrent-detach` (#1996, contains fu8) | fix(python-sdk): let concurrent detach() calls on a batch handle both succeed (10/N) | The async handle's watch and renewal tasks start with `asyncio.create_task` and are named (fu8). On a 409 from the release write, `detach()` re-reads the Lease and treats the release as done unless this handle still holds it. | barney-s's concurrent `detach()` comment on #1742. |
| 11 | `feat/batch-2-claim` (#1824) | feat(python-sdk): add claim_batch and release (11/N) | `claim_batch` on both clients: prechecks, batch Lease, paced background creates with retries, `CreateFailed` members. `release()` with deletecollection rounds. Tracked handles are released by `delete_all()` and exit cleanup. | Carries the J5 atexit follow-through (atexit no longer skips batch-only clients). |
| 12 | `feat/batch-3-group-consumers` | feat(python-sdk): add events and iter_ready_groups (12/N) | O(1) fill accounting in the batch state. `events()` and `iter_ready_groups()` with per-group quorum, `GroupReady`, `QuorumUnreachableError` and `BatchTimeoutError`. | None. |
| 13 | `feat/batch-4-group-quorum` | feat(python-sdk): add wait_for_quorum (13/N) | Batch-wide quorum mode in the state. `wait_for_quorum()` returns each group's first `min_ready` members at once. README example for the third mode. | None. |
| 14 | `feat/batch-5-release-group-76a8m3` | feat(python-sdk): add release_member, release_group and release_not_ready (14/N) | Released members in the state. `release_member`, `release_group` (hashed group label, deletecollection) and `release_not_ready`. Unsent creates are skipped and late creates deleted. | Manager's group-release feedback: caller-called `release_group` replaces an automatic delete, with a per-group label and `deletecollection`. |
| 15 | `feat/batch-6-acquire-nbbp5j` | feat(python-sdk): add acquire (15/N) | `acquire(warmpool)` adds one Ready member after the fill. `size=0` groups. `TerminalMemberError`. The Lease records labels, `create_rps` and the next ordinal for re-attach. | None. |
| 16 | `feat/batch-transport-ltlfdm` | feat(python-sdk): connect batch members through their watched pod IP (16/N) | `connect()` seeds the Sandbox with the watched pod IP, saving one GET per member. Cached connections close when a member is lost, failed or changes IP. `claim_batch` rejects a connection pool smaller than `max_in_flight + 2`, and the clients take `pool_size`. | None. |
| 17 | `feat/batch-reaper-ymtd6t` | feat(python-sdk): add the batch reaper (17/N) | Claims carry an ownerReference to their batch Lease. `python -m k8s_agent_sandbox.batch_reaper` deletes only stale Leases (with a resourceVersion precondition). The garbage collector deletes the claims through the ownerReference, so the reaper needs no claim RBAC. | None. |
| 18 | `feat/batch-7a-create-path` ("PR 7") | feat(python-sdk): skip create pacing for claims a warm pool covers (18/N) | Warm-aware pacing for the fill and `acquire()`: a pool's ready replicas at the precheck pay no pacing slot. | None. |

## Suggested descriptions

Brian opens the PRs himself, so the attribution block is not included.

### PR 1 (1/N)
This PR is the first in a series of PRs to implement a batch. It adds the handle for an existing batch.
- `SandboxClient.get_batch` and `AsyncSandboxClient.get_batch` take over the batch's Lease and return a handle.
- The handle exposes `members()`, `connect()`, `err()` and `detach()`, and keeps a watch and a Lease renewal running in the background.
- Nothing creates a batch yet. Tests attach to claims and a Lease created directly.

### fu1 (2/N)
This PR is the second in a series of PRs to implement a batch. It keeps the batch watch alive across network failures.
- A refused connection or failed DNS lookup (sync) and the watch's own timeout (async) are retried with the watch's backoff instead of ending it.
- Requests that had no timeout now have one, including the 410 re-list.

### fu2 (3/N)
This PR is the third in a series of PRs to implement a batch. It adjusts the expiry mechanism to ensure a handle never thinks it holds the batch's Lease after another client could take it over. Expiry is now timed from when the renewal was sent, renewal stops once the Lease expires, and Lease calls no longer retry silently, which stretched timeouts up to 4x. Renewals also stop reading the Lease before each write, so they make half the requests. A newly introduced `lease_degraded()` lets callers see renewal issues before the Lease expires.
- An empty `holderIdentity` counts as free.

### fu3 (4/N)
This PR is the fourth in a series of PRs to implement a batch. It replaces `Member`'s `ready`, `terminal` and `lost` flags with one `state`.
- `MemberState` is PENDING, READY, FAILED or LOST.
- A failed member stays FAILED if its claim is deleted, so its state still says why it never became Ready.

### fu4 (5/N)
This PR is the fifth in a series of PRs to implement a batch. It stops reporting a claim that is being deleted as Ready.
- The controller stops updating a terminating claim, so its Ready condition can stay True until it is gone.
- Such a claim now becomes LOST right away unless it already failed, and its pod IPs and FQDN are cleared.

### fu5 (6/N)
This PR is the sixth in a series of PRs to implement a batch. It makes `connect()` check a member before returning its cached connection.
- `connect()` raises `BatchError` once `err()` is set, since the Lease may be lost.
- A member that is no longer Ready raises `SandboxNotReadyError` even while its connection is cached.

### fu6 (7/N)
This PR is the seventh in a series of PRs to implement a batch. It keeps a batch's group order when `get_batch` re-attaches.
- Groups are ordered by their lowest claim ordinal, not by pool name.
- A claim without an ordinal sorts after numbered ones.

### TLS EOF (8/N, #2033)
This PR is the eighth in a series of PRs to implement a batch. It keeps the batch watch running when a peer closes the TLS connection early.
- A load balancer or proxy that drops the connection mid-handshake or mid-stream raises `ssl.SSLEOFError`. The watch treated it as fatal and stopped on the first such failure, even though the same drop seen as a connection reset is retried.
- Both shells now treat `SSLEOFError` like a connection reset, through one `_is_transport_error` helper per shell that the watch uses. Certificate and other TLS failures stay fatal, so a misconfigured cluster still fails fast.

### fu7 (9/N)
This PR is the ninth in a series of PRs to implement a batch. It makes client cleanup close what batches leave open.
- Cleanup closes each tracked batch's cached connections, which hold port-forward processes.
- The async client's `close()` and `delete_all()` share one `_stop_batches()`, and its atexit hook closes batch connections.
- `detach()` unregisters only its own handle, so a newer handle for the same batch stays tracked.

### fu9, containing fu8 (10/N)
This PR is the tenth in a series of PRs to implement a batch. It lets two `detach()` calls on the same handle both succeed, and names the async batch handle's background tasks.
- The watch and Lease renewal start with `asyncio.create_task` instead of `ensure_future`, named `batch-<id>-watch` and `batch-<id>-renew`, so they are easy to tell apart in task dumps (fu8).
- Both calls can read the Lease while this handle holds it, so the second release write used to fail with a 409 even though the batch was released.
- On a 409, `detach()` now reads the Lease again and only raises if this handle still holds it.

### PR 2 (11/N)
This PR is the eleventh in a series of PRs to implement a batch. It adds `claim_batch` and `release`.
- `claim_batch` prechecks each group's warm pool and template, creates the batch Lease, then creates claims in the background at `create_rps` with `max_in_flight` workers. Failed creates become `CreateFailed` members.
- Each group's first `min_ready` claims are created first, one group after another in group order, and the rest follow in the same order, so the first groups can be consumed while later groups are still being created.
- `release()` stops the handle and deletes the batch's claims by label, then its Lease.
- `delete_all()` and exit cleanup release every tracked batch.

### PR 3 (12/N)
This PR is the twelfth in a series of PRs to implement a batch. It adds the ways to consume a batch as it fills.
- `events()` yields member events as they happen.
- `iter_ready_groups()` yields each group once it has `min_ready` Ready members, or raises when it can't.
- Fill accounting is O(1) per watch event.

### PR 4 (13/N)
This PR is the thirteenth in a series of PRs to implement a batch. It adds `wait_for_quorum()`.
- It blocks until every group has `min_ready` Ready members and returns them in one list, or raises `QuorumUnreachableError` or `BatchTimeoutError`.
- Members beyond the quorum are handed out through `events()`.

### PR 5 (14/N)
This PR is the fourteenth in a series of PRs to implement a batch. It lets a caller free part of a batch.
- `release_member(member)` deletes one member's claim.
- `release_group(warmpool)` deletes a group's fill claims with one label-scoped `deletecollection`, using a hashed group label.
- `release_not_ready()` ends the fill and deletes every member that isn't Ready and was never handed out.

### PR 6 (15/N)
This PR is the fifteenth in a series of PRs to implement a batch. It adds `acquire()` so a caller can replace a released member.
- `acquire(warmpool, timeout=None)` adds one member from any warm pool in the namespace and blocks until it is Ready. It never leaves its claim behind.
- `size=0` groups are allowed for pools filled only through `acquire()`.
- The Lease records labels, `create_rps` and the next ordinal, so a re-attached handle behaves like the original.

### Transport (16/N)
This PR is the sixteenth in a series of PRs to implement a batch. It makes fan-out cheaper and safer.
- `connect()` uses the pod IP the watch already knows, so it skips one Sandbox GET per member.
- A cached connection closes when its member is lost, failed or moves to another IP.
- `claim_batch` fails early when the client's connection pool is smaller than `max_in_flight + 2`. `SandboxClient` and `AsyncSandboxClient` take `pool_size`.

### Reaper (17/N)
This PR is the seventeenth in a series of PRs to implement a batch. It cleans up batches whose driver died.
- Batch claims carry an ownerReference to their Lease, so the garbage collector deletes them once the Lease is gone.
- `python -m k8s_agent_sandbox.batch_reaper` runs once and finds Leases past their duration plus a skew margin. It deletes only those Leases, each with a resourceVersion precondition, and never touches claims, so its RBAC is `list`, `get` and `delete` on Leases. `--dry-run` only logs.

### PR 7 (18/N, number inferred from stack order; Brian decides)
This PR is the eighteenth in a series of PRs to implement a batch. It stops create pacing from slowing batches that a warm pool already covers.
- `claim_batch` and `acquire()` read each warm pool's `status.readyReplicas` at the precheck and give that many creates no pacing slot, so only `max_in_flight` bounds them. The rest stay at `create_rps`.

## Notes
- **Numbering (renumbered 2026-10-22 for plan C).** Merged: 5/N #1983, 6/N #1984, 7/N #1985. Open, titles renumbered with `gh pr edit` on 2026-10-22: 8/N TLS EOF #2033, 9/N fu7 #1986, 10/N fu9 #1996 (contains fu8), 11/N PR 2 #1824, 12/N PR 3 #1988, 13/N PR 4 #1989, 14/N PR 5 #1990, 15/N PR 6 #1991. Transport, reaper and PR 7 are not opened; 16/N to 18/N are inferred from the stack order.
- The PR 7 text above was drafted from the commit titles ("skip create pacing for claims a warm pool covers", "skip acquire pacing ..."), not from Brian's wording. Check it against the code.- Titles use his "(k/N)" form with a literal N, numbered by stack position.
- PR 1's diff against `origin/main` also lists upstream commits because the fork's main lags. The row covers only its five batch commits.
- The follow-ups were split by topic on 2026-10-19. The old `feat/batch-1-fu4-group-order` and `feat/batch-1-fu5-logging` branches are stale; fu6 and fu7 replace them.
- Row 17 describes the branch tip 2dcf9fd, with the reaper deleting only Leases (option B, approved 2026-10-19).
