# Two protocol issues found by the TLA+ model (for evaluation)

Status: **resolved in 22/N (`feat/batch-10-deletecollection-retry` `d9df3a42`, synced 2026-11-06)**.
- **Issue 1 (`BelieverKeepsClaims` / `NoForeignDelete` + `claim_batch` setup fencing):** Approved by Brian (`"1. yes approve"`). Every batch claim now carries `BATCH_LEASE_UID_LABEL = "agents.x-k8s.io/batch-lease-uid"` (`lease.metadata.uid`), scoping post-Lease `_attach`, `_watch_loop`, `release_group`, and `release` / client-cleanup `deletecollection` selectors to `(batch-id, batch-lease-uid)` while keeping pre-Lease `_claim` live-claim collision checks and `lease is None` `_attach` checks on `batch-id` alone. `claim_batch` also bounds its pre-existing claim list by `deadline = renewed_at + lease_duration`, rejects setup with `BatchLeaseExpiredError` if `time.monotonic() >= lease_expires_at`, and fences rollback `delete_batch_lease` with `resource_version=created_rv`. Both shells self-fence locally via `_expire_lease_if_due` across `_check_healthy`, `_can_still_send`, `_create_worker`, `_delete_released_create`, `_wait_acquired`, and `_next`, keep `lease_degraded()` sticky against late renewals, route all batch custom-object calls through the unretried `_batch_custom_objects_api` (`retries=False`), and cap per-claim request timeouts by the remaining Lease budget (`self._lease_expires_at - now`).
- **Issue 2 (`ReaperSafety` / confirmation wait):** Approved by Brian after independent agent review (`Option A`). `batch_reaper.reap()` now waits each candidate Lease's full `leaseDurationSeconds` (`StaleLease.lease_duration`) instead of `renew_interval`, documents the stalled-driver clock-skew bound (`--skew-margin` plus Lease duration minus any in-flight request latency) and CronJob `activeDeadlineSeconds` sizing (`>= 600` seconds for 300-second RL Leases) in `batch_reaper.py` and `README.md`, and updates `test_batch_reaper.py`.
- **TLC verification (`BatchLease.tla` at `Bugs = {}, Fixes = {}`):** Promoted UID-scoped `deletecollection` and full-duration reaper confirmation wait to baseline, modeled 2-step `claim_batch` setup (`ClaimCreateLease`, `ClaimFinish`, `ClaimRollback`, `ClaimSetupFencing`) and Lease-bounded by-name `_delete_claim` (`MemberDelete`), and verified all 31 TLC configurations in `work/tla/results.md`: all 11 positive and skew runs (`main_core` 5.37M, `main_release` 24.23M, `main_fence` 19.66M, `main_claim_setup` 663k, `main_member_ops` 486k, `skew6_connected`, `skew12_connected`, `skew8_flaky`, `skew9_flaky`, `skew10_flaky`, `skew8_member_flaky`) pass with 0 errors and 0 states left on queue, while all bug-injection controls (`neg_unscopedReleaseDelete`, `neg_reaperWaitInterval_skew6`, `neg_unconditionalRollbackDelete`, `neg_claimNoExpiryCheck`, `neg_retryPastExpiry`, and `skew9_member_flaky` at `SkewMax = D + M + 1 = 9`) fail with their expected invariant violations.

Sources:
- Spec and runs: [`work/tla/BatchLease.tla`](tla/BatchLease.tla), [`work/tla/results.md`](tla/results.md), traces in `work/tla/traces/`.
- Write-up: [`learnings/tla.md`](../learnings/tla.md) (TLA-1, TLA-2). Related entries: LEASE-8 and LEASE-18 in `learnings/field-guide/lease.md`.
- Code references are to the 23/N head `feat/batch-11-acquire-annotations` (`dd66c5fd`), file `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py` unless noted. The async shell (`async_sandbox_batch.py`) has the same structure, so every fix needs both shells and mirrored tests.

| Issue | Severity if hit | Likelihood | Fix size |
| :-- | :-- | :-- | :-- |
| 1. A stalled `release()` can delete a newer batch's claims | High: deletes another batch's live sandboxes | Low: needs a reused caller-chosen `batch_id` and a release stalled for minutes | Medium, and API-visible if it adds a label |
| 2. The reaper's confirmation wait has an unstated clock-skew bound | High: reaps a live batch | Low with defaults (needs over ~80 s of skew), higher with `--skew-margin 5` (~25 s for a stalled holder) | Small (longer wait), or docs only |

---

## Issue 1: a stalled `release()` can delete a newer batch's claims

### What happens

`release()` deletes claims in rounds. Each round first calls `hold()`, which takes the Lease, then sends a label-scoped deletecollection.

- `_take_batch_lease` (`sandbox_batch.py:1911`) returns `None` when the Lease doesn't exist (`:1940-1941`).
- `_delete_batch_objects.take()` (`:1997`) stores that `None` as `resource_version`. `finish()` (`:2003-2005`) skips the Lease delete, but nothing stops the round.
- `_delete_claims_in_rounds` (`:2023`) sets `held = True` after `hold()` returns (`:2053-2054`) and sends `delete_sandbox_claims_by_label(namespace, "agents.x-k8s.io/batch-id=<id>")` (`:2058`), whether or not a Lease existed.

The label selects by batch id, not by Lease UID. A batch id can be reused once the old Lease and claims are gone, because `claim_batch` only refuses an id when its Lease exists (`BatchExistsError` on the Lease create) or claims with that label still exist (the list check in `_claim`).

### Shortest trace (TLC, `finding_23n.cfg`, 19 states, no failures or lost messages)

```text
t=1   handle A: claim_batch(batch_id="b1")       Lease uid=1, claims owned by uid 1
t=1   handle A: release() starts, then stalls before its first round
            (a process pause: GC, laptop sleep, SIGSTOP, a slow thread)
t=10  reaper: Lease b1 is stale, unchanged for one renew interval -> delete (precondition ok)
      GC: deletes uid 1's claims
t=10  handle A wakes: _take_batch_lease reads no Lease -> returns None -> round counts as held
      handle A: sends deletecollection batch-id=b1 (in flight)
t=10  handle B: claim_batch(batch_id="b1")       Lease create ok (uid=2), claim list empty, creates claims
t=10  handle A's deletecollection lands          -> deletes handle B's claims
```

A's next round would read B's Lease, see a different UID, and raise `BatchInUseError` (the LEASE-17 fence). But the first request has already landed.

Reproduce: `cd handoff/work/tla`, download `tla2tools.jar` (see `results.md`), then `java -cp tla2tools.jar tlc2.TLC -workers auto -deadlock -config finding_23n.cfg BatchLease.tla`. TLC reports `BelieverKeepsClaims` violated (B believes it holds the batch, but its claims are gone).

### Preconditions (why the likelihood is low)

1. **A caller reuses a batch id on purpose.** Generated ids are `b` plus 11 random characters, so they practically never collide. Checked: the RL integration (`feat/rl-batch-claim-mode`, `examples/agent-sandbox-rl`) does not pass `batch_id` to `claim_batch`.
2. **A `release()` stalls for at least `D + skew_margin + renew_interval`** (about 140 s with the defaults) between its call and its first round. That's long enough for the reaper to delete the Lease and the GC to remove every old claim; terminating claims still show in the list and would make B's `claim_batch` raise `BatchExistsError`.
3. **The new `claim_batch` runs inside the window** between A reading "no Lease" and A's deletecollection landing.

### Why the existing reasoning missed it

LEASE-18 says every delete is sent "while this handle holds the Lease, or no Lease exists". The first half is a fence. The second half is only a snapshot: "no Lease" can become "a new Lease" before the delete lands.

### Fix options (checked in the model where noted)

| Option | Model result | Cost and notes |
| :-- | :-- | :-- |
| A. Skip the label delete in a round whose take finds no Lease | **Fails** (18-state trace, `fix_skipOnNone.cfg`): a deletecollection from an earlier round, whose client saw a transport error, lands late after a new batch exists. | Small, but not sufficient alone. |
| B. Scope release deletes to the Lease UID: label every claim with the Lease UID (e.g. `agents.x-k8s.io/batch-lease-uid`) and select on batch id plus UID | **Passes** `main_release` (11.1M states) and `main_fence` (19.7M states). | API-visible: new label, README/RBAC docs, and claims created before the change lack the label (migration: fall back to the batch-id selector only while the Lease UID matches, or accept that old batches rely on GC). |
| C. Delete by name with `preconditions.uid` per claim whose ownerReference matches the Lease UID | Equivalent to B in the model (UID-scoped). | No new label, but N deletes instead of one deletecollection, which undoes OPT-2's request savings. It needs a list per round. |
| D. Once a round finds no Lease, stop and let GC finish (the claims' owner is gone, so GC deletes them anyway) | Not modeled separately. It behaves like A for the current round, so the late-landing request from an earlier round remains. | Smallest. It relies on GC speed (about 20 claims/s with default controller-manager settings, per the proposal). |
| E. Refuse caller-chosen ids that existed recently, or document "never reuse a batch id" | Not modeled. | Doc-only or a weak check. Doesn't close the earlier-round case. |

**Recommendation to evaluate:** B is the only modeled option that closes both paths. If an API-visible label is unwanted, consider combining A or D with a documented "never reuse a batch id within D + margin + reaper period" rule, and say plainly that a late request from an earlier round remains a residual risk.

### Tests a fix needs

- A unit test in both shells: `release()` whose take returns `None`, then a new batch with the same id creates claims, and the stale round's delete does not touch them (fake apiserver that applies selectors).
- An e2e (optional): SIGSTOP a releasing driver past the reaper window, recreate the id, SIGCONT, and assert the new batch's claims survive.
- Rerun `main_release`/`main_fence` with the chosen fix's toggle, and keep `finding_23n` as a negative control.

---

## Issue 2: the reaper's confirmation wait has an unstated clock-skew bound

### What happens

The reaper picks a Lease when `now > renewTime + D + skew_margin` by its own clock (`batch_reaper.py: is_lease_reapable`, `:59`). It then waits one renew interval (`renew_interval = max(1, D // 3)`, `:122`) after its list returns (`listed_at`, `:197`; sleep at `:200`), and deletes with a resourceVersion precondition. The idea (LEASE-8): a live holder renews within one interval, so the precondition fails.

But the holder doesn't write every `I` seconds on the server. `_renew_loop` (`sandbox_batch.py:1508`) waits `_renew_interval` after the previous `_renew_once` returns (`:1518-1521`), and each renewal request may take up to `min(I, time left)` (`_renew_request_timeout`, `:1542-1547`). So two renewal writes can land up to `I + reply leg + request leg` apart, longer than the reaper's wait of `I`. If the reaper's clock is far enough ahead to pick the Lease early, its list can land just after one renewal, and its delete can beat the next one.

### Model results

| Run | Reaper skew (ticks, 1 tick ~ 10 s, D = 6, M = 2) | Result |
| :-- | :-- | :-- |
| `skew5_connected` | 5 | passes |
| `skew6_connected` (a connected, timely holder) | 6 | **ReaperSafety violated**, 16 states |
| `skew5_flaky` | 5 | passes |
| `skew6_flaky` (a holder that stopped renewing but hasn't expired) | 6 | **violated**, 11 states |
| `skew6_connected_waitLonger` / `skew12_connected_waitLonger` (reaper waits I plus the longest reply and request legs) | 6 / 12 | passes |

Derived bound: the protocol is safe only while the reaper's clock runs ahead of the holder's by less than about `D + M - (reply leg + request leg)` for a connected holder, and less than `M + I` for a stalled holder. With the defaults (D = 60 s, M = 60 s, I = 20 s, requests bounded by I), both come to **about 80 s** in the worst case. With `--skew-margin 5` (the minimum the reaper accepts, `_skew_margin` at `:225`), the stalled-holder bound drops to **about 25 s**.

### Why it matters

NTP-synced nodes rarely drift by 80 s, so the default setup is safe in practice. But LEASE-8 and the README present the confirmation wait as making the reaper safe against a driver "whose clock runs behind", without stating a bound. An operator who sets `--skew-margin 5`, or runs the reaper on a node with a broken clock, gets a much tighter bound than they think.

### Fix options

| Option | Model result | Cost and notes |
| :-- | :-- | :-- |
| A. Reaper waits about `3 * renew_interval` (one interval plus two request timeouts, each bounded by one interval) instead of one interval | **Passes** at skew 6 and 12. The stalled-holder bound rises to `M + 3I`. | One-line change in `batch_reaper.reap`. Crash cleanup takes up to 2 intervals longer (about 40 s with defaults). README text changes. |
| B. Document the bound: the reaper is safe while clock skew stays below about `D + M - 2I` (connected) and `M + I` (stalled); raise the `--skew-margin` floor | n/a | Docs only. Consider a floor well above 5 s. |
| C. Renew on a fixed cadence measured from each send time, not after each call returns | Not modeled. It shrinks the gap toward `I + request leg`, so it is not a full fix alone. | Changes renewal timing in both shells. More risk than A. |

**Recommendation to evaluate:** A plus the doc part of B. They're small, they fit the "waiting is safe, deleting early is not" rule (LEASE-4), and A is already checked in the model.

### Tests a fix needs

- `test_batch_reaper.py`: the wait before each delete is the new interval (the existing test pins `renew_interval`; update it to the new rule).
- README: state the skew bound next to `--skew-margin`.
- Rerun `skew6_connected` and `skew6_flaky` with the new wait and expect them to pass. Keep the one-interval wait as a negative control.

---

## How to proceed

1. Verify each claim above against the code at the current head (the line numbers are for `dd66c5fd`) and rerun the TLC configs.
2. For each issue, decide: fix in a new PR on top of the stack, fix in an owning PR, document only, or close with reasoning. List the alternatives and their costs (see `handoff/working_rules.md`).
3. Propose to Brian and wait for approval before changing code. If approved, follow the stack rules: both shells, mirrored tests, mutation-check each new test, no `#<number>` in commit messages.
