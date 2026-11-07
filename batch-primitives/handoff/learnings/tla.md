<a id="sec-tla"></a>

[Learnings home](../learnings.md) · [Verification](verification.md)

# Verifying the Lease protocol with TLA+

The in-process checker in this page's sibling ([verification.md](verification.md)) explores every interleaving of the threads inside one handle. The batch Lease protocol is a different beast: it runs across processes. Two handles in two Python processes, a reaper CronJob, the apiserver, and the garbage collector all act on one `coordination.k8s.io/v1` Lease, with clocks that disagree, requests that land late, and replies that never arrive. We wrote a TLA+ specification of that protocol, checked it with TLC, the standard TLA+ model checker, and found one real bug, one argument that needed an extra assumption, and a clearer picture of which defenses carry the load. The spec lives at `handoff/work/tla/BatchLease.tla`, next to its configs, run scripts, and full results (`results.md`).

## Why TLA+ here, and not another BFS checker

Our hand-rolled checker ([VER-1](verification.md#ver-1) through [VER-11](verification.md#ver-11)) works because one handle's threads share memory and their atomic steps are easy to name ([VER-2](verification.md#ver-2)). The Lease protocol has none of that. Its safety rests on four mechanisms that live in different processes:

- **Time thresholds** that three parties evaluate on three clocks ([LEASE-4](field-guide/lease.md#lease-4)): `get_batch` refuses at `renewTime + D - 5 s`, the holder gives up at `last send + D` ([LEASE-5](field-guide/lease.md#lease-5)), and the reaper picks a Lease only after `renewTime + D + margin`.
- **Optimistic concurrency**: every Lease write carries the last-seen resourceVersion, and the apiserver rejects a stale one with 409 ([LEASE-2](field-guide/lease.md#lease-2), [K8S-1](field-guide/k8s.md#k8s-1)).
- **The reaper's confirmation wait**: list, wait one full `leaseDurationSeconds`, then delete with a resourceVersion precondition ([LEASE-8](field-guide/lease.md#lease-8)).
- **UID-scoped claim selectors and release fencing** on Lease UID plus `leaseTransitions` ([LEASE-16](field-guide/lease.md#lease-16), [LEASE-17](field-guide/lease.md#lease-17), [LEASE-18](field-guide/lease.md#lease-18)), and GC by owner UID ([K8S-2](field-guide/k8s.md#k8s-2)).

The prose arguments for these each hold one variable still and reason about the rest. A model checker moves everything at once. TLA+ is built for exactly this: a small language for describing state machines with time, messages, and failures, plus a checker that visits every reachable state of a bounded instance. [VER-11](verification.md#ver-11) says when to graduate to a real tool; a cross-process protocol with clocks is that moment.

## A short TLA+ primer, through our spec

TLA+ describes a system as a set of **variables**, an **initial-state predicate** `Init`, and a **next-state relation** `Next`. A behavior is a sequence of states where the first satisfies `Init` and every step satisfies `Next`. That's it. There are no threads or function calls; concurrency is just "any enabled action may happen next".

**Variables** hold the whole world. Ours include the true time `now`, the apiserver's Lease, the set of Lease UIDs that still own claims, the requests in flight, and per-handle fields (functions from a handle to a value):

```tla
VARIABLES
  now,        \* true time; handles read it directly
  rskew,      \* reaper wall clock = now + rskew
  lease,      \* [exists, uid, rv, holder, renew, dur, trans]
  claims,     \* UIDs of Leases that still own live claims
  net,        \* requests in flight to the apiserver
  pc, myUid, myTrans, myRv, expAt, err, ...
```

**Actions are predicates on two states.** An unprimed variable means its value now; a primed one (`lease'`) means its value after the step. An action is true of a step when the step does what the action describes, so an action is enabled exactly when its guard conjuncts are satisfiable. Here is the reaper picking a stale Lease (`batch_reaper.find_stale_leases` with `is_lease_reapable`):

```tla
Reapable == now + rskew > lease.renew + lease.dur + M
ReapList ==
  /\ rpc = "idle" /\ lease.exists /\ Reapable          \* guard
  /\ rpc' = "waiting" /\ rRv' = lease.rv /\ rAt' = now  \* effect
  /\ rInt' = lease.dur
  /\ UNCHANGED <<now, lease, claims, net, ...>>
```

`/\` is "and", `\/` is "or", and `UNCHANGED x` means `x' = x`. Forgetting a variable in an action leaves it unconstrained, and TLC reports that as an error, which catches a whole class of modeling slips.

**`Next` is the disjunction of every action**, with existential quantifiers for "some handle" or "some message":

```tla
Next ==
  \/ Tick \/ ReapList \/ ReapDelete
  \/ \E p \in Procs : Claim(p) \/ ClaimCreateLease(p) \/ ClaimFinish(p) \/ ClaimRollback(p)
                      \/ Attach(p) \/ Renew(p) \/ MemberDelete(p) \/ Detach(p)
                      \/ StartRelease(p) \/ TakeRead(p) \/ Count(p)
                      \/ Recv(p) \/ GiveUp(p) \/ Crash(p) \/ GoFlaky(p)
  \/ \E m \in net : Deliver(m)
  \/ \E u \in claims : GC(u)
Spec == Init /\ [][Next]_vars
```

**Invariants are predicates on one state**, the same idea as [VER-3](verification.md#ver-3). TLC checks each one in every reachable state and, on a violation, prints the shortest behavior that reaches it (TLC searches breadth-first). "A handle believes it holds the batch" becomes:

```tla
Believes(p) == alive[p] /\ pc[p] = "hold" /\ ~err[p] /\ now < expAt[p]
BelieverIsHolder == \A p \in Procs : Believes(p) =>
                      lease.exists /\ lease.uid = myUid[p] /\ lease.holder = p
```

**TLC** reads a `.cfg` file that picks concrete values for the `CONSTANTS` (lease duration, margins, how far the clock may run) and lists the invariants. Because our constants bound the clock and the number of Lease incarnations, the state space is finite and TLC explores all of it.

## The spec, action by action

We model two handles, one reaper, the apiserver, and GC. One tick stands for about 10 s, so `D = 6` is a 60 s Lease and the renew interval is `I = D div 3 = 2`. The reaper margin is `M = 2` and `get_batch`'s margin is `G = 1`. The reaper's clock runs ahead of the handles' by `SkewMax`, the worst case (a reaper further ahead may act at more times, so this covers smaller skews too).

| Action | Python it models | What it does |
| --- | --- | --- |
| `Claim(p)` | `SandboxBatch._claim` (atomic mode) | Create-if-absent the Lease with a fresh UID, refuse if any claim with `batch-id` exists, create claims owned by and labeled with the UID. Expiry `now + D`, read before stamping ([LEASE-5](field-guide/lease.md#lease-5)). |
| `ClaimCreateLease`, `ClaimFinish`, `ClaimRollback` | `SandboxBatch._claim` (`"claimSetup"`) | Split setup into Lease create, deadline-bounded pre-existing claim list plus post-list `now < expAt[p]` check, and `resource_version`-fenced rollback `delete_batch_lease`. |
| `Attach(p)` | `SandboxBatch._attach` | Require empty holder and `now <= renew + dur - G`; send a takeover write at the read resourceVersion with `leaseTransitions + 1` ([LEASE-14](field-guide/lease.md#lease-14)). |
| `Renew(p)` | `_renew_loop`, `_renew_once`, `_write_renewal` | Send a PUT at the last written resourceVersion, timeout `min(I, time left)`. On success, expiry is `sent_at + D`. On 409, read and rewrite only if still the holder ([LEASE-28](field-guide/lease.md#lease-28)); on 404, stop ([LEASE-7](field-guide/lease.md#lease-7)). |
| `MemberDelete(p)` | `_delete_claim`, `release_member` (`"memberOps"`) | While `Believes(p)`, block `detach()` and `release()` and send one unretried by-name claim delete bounded by `min(30s, expAt[p] - now)`. |
| `Detach(p)` | `SandboxBatch.detach` | Join renewal and member deletes, then read; if still holder, write `holder=None` with the grace; on 409 re-read and retry once ([LEASE-15](field-guide/lease.md#lease-15), [LEASE-16](field-guide/lease.md#lease-16)). |
| `StartRelease`, `TakeRead`, `Count` | `release`, `_delete_batch_objects`, `_take_batch_lease`, `_delete_claims_in_rounds` | Per round: read, fence on UID, `leaseTransitions`, and holder ([LEASE-17](field-guide/lease.md#lease-17)), conditional take, UID-scoped `deletecollection` by `(batch-id, batch-lease-uid)`, consistent count, and delete the Lease at the taken resourceVersion when 0 live claims remain ([LEASE-18](field-guide/lease.md#lease-18)). |
| `Recv`, `GiveUp` | the HTTP client | Deliver a reply, or time out (or, if flaky, raise a transport error at any time) while the request may still land later ([LEASE-16](field-guide/lease.md#lease-16)). |
| `Deliver(m)` | the apiserver | Apply a Lease write or delete only if its resourceVersion precondition matches (else 409, or 404 if gone), or apply a claim `deletecollection` or by-name `mdel`. |
| `ReapList`, `ReapDelete` | `find_stale_leases`, `reap`, `_delete_lease_if_unchanged` | Pick at `renew + dur + M` on the reaper's clock, wait the full `lease.dur` confirmation window, and delete with a resourceVersion precondition ([LEASE-8](field-guide/lease.md#lease-8), [LEASE-9](field-guide/lease.md#lease-9)). |
| `GC(u)` | the garbage collector | Delete claims whose owner UID no longer exists ([K8S-2](field-guide/k8s.md#k8s-2)). |
| `Crash(p)`, `GoFlaky(p)` | the environment ([VER-5](verification.md#ver-5)) | Kill a holding process; or let a handle's requests and replies drop, raise transport errors, and pause. |
| `Tick` | time | Advance `now` by one, but only if every connected handle is timely. |

**Modeling time.** A global integer clock plus one rule does most of the work. `Tick` may fire only when no connected (non-flaky) handle owes the world a step: finishing `claim_batch` setup within `Lat`, sending a due renewal, timing out an overdue wait, landing a request within `Lat`, or reading a reply within `Lat`. A flaky handle is exempt, which is how the model says "partitioned or paused".

```tla
Timely(p) ==
  alive[p] /\ ~flaky[p] =>
    /\ ~(pc[p] = "claiming" /\ now >= (expAt[p] - D) + Lat)
    /\ ~(pc[p] = "hold" /\ ~err[p] /\ wKind[p] = "none" /\ now >= nextRenew[p]
         /\ now < expAt[p])
    /\ ~(wKind[p] # "none" /\ now >= wBy[p])
    /\ (reply[p] # NoReply => reply[p].at + Lat > now)
Tick ==
  /\ now < MaxTime
  /\ \A p \in Procs : Timely(p)
  /\ \A m \in net : ~flaky[m.from] => m.landBy > now
  /\ now' = now + 1
  ...
```

**The seven invariants**, in plain English:

- `ReaperSafety`: the reaper never deletes the Lease of a live handle that still believes it holds it.
- `ReaperSafetyConnected`: the same guarantee restricted to connected, timely believers.
- `AtMostOneBeliever`: at most one live handle believes it holds the batch.
- `BelieverIsHolder`: the server backs every believer's belief: its Lease incarnation exists and names it as holder.
- `BelieverKeepsClaims`: every believer's claims still exist.
- `ReleaseFencing`: `release()` never deletes claims of a live Lease incarnation it does not own, nor of a batch another handle attached to since this handle last wrote the Lease.
- `ClaimSetupFencing`: `claim_batch()` never finishes setup after its Lease expired locally or was lost on the apiserver.

**Negative controls and fixes** are switches in two `CONSTANTS`, `Bugs` and `Fixes`, so one spec serves every run ([VER-6](verification.md#ver-6)). A third constant, `Features`, turns optional moves on and off, so each run checks a feature set that fits in a few minutes.

## Results

All runs use TLC 2.19 with 24 workers. "Trace" is the length of the shortest counterexample in states. After landing the 22/N fixes (`Bugs = {}, Fixes = {}` baseline), `BatchLease.tla` promotes UID-scoped `deletecollection` and full-duration reaper wait (`rInt = lease.dur`) to baseline, adds 2-step `claim_batch` setup (`ClaimCreateLease`, `ClaimFinish`, `ClaimRollback`, `ClaimSetupFencing`) and Lease-bounded single-claim deletes (`MemberDelete`), and checks all 31 configurations in `results.md`:

| Run | What it checks | Result | Distinct states | Trace |
| --- | --- | --- | --- | --- |
| main_core | claim, renew, attach, detach, crash, flaky, reaper, GC (`SkewMax=5`) | All seven invariants hold | 5,366,897 | - |
| main_release | claim, renew, UID-scoped release, crash, flaky, reaper, GC (`SkewMax=5`) | All hold | 24,233,858 | - |
| main_fence | claim, renew, attach, detach, release, flaky (`MaxTime=7`) | All hold | 19,655,559 | - |
| main_claim_setup | 2-step `claim_batch` setup, deadline expiry check, `rv`-fenced rollback (`MaxTime=14`) | All hold | 663,265 | - |
| main_member_ops | Lease-bounded unretried `_delete_claim` (`retries=False`, `min(30s, expAt - now)`, `MaxTime=14`) | All hold | 486,124 | - |
| skew6_connected / skew12_connected | connected holder, reaper 6 / 12 ticks ahead (`MaxTime=14`) | Holds | 10,447 / 32,965 | - |
| skew8_flaky / skew9_flaky / skew10_flaky | stalled holder, reaper 8 / 9 / 10 ticks ahead (`MaxTime=14`) | Holds (saturates at 9) | 1,233,135 / 1,589,514 / 1,589,514 | - |
| skew8_member_flaky / **skew9_member_flaky** | stalled holder with in-flight by-name `_delete_claim`, reaper 8 / 9 ticks ahead | Holds / **`BelieverKeepsClaims` violated** | 908,037 / 65,463 | - / 15 |
| **neg_unscopedReleaseDelete** | pre-22/N unscoped release delete (`finding_23n`) | **`BelieverKeepsClaims` violated** | 24,052 | 22 |
| neg_unscopedReleaseDelete_skipNone | pre-22/N unscoped release delete + `Fixes={skipOnNone}` | `BelieverKeepsClaims` violated | 183,522 | 18 |
| **neg_reaperWaitInterval_skew6** / flaky | pre-22/N reaper wait `Interval(D)` at `SkewMax=6` (connected / flaky) | **`ReaperSafety` violated** | 2,016 / 1,243 | 16 / 10 |
| **neg_unconditionalRollbackDelete** | `claim_batch` rollback omits `resource_version=created_rv` | **`BelieverIsHolder` violated** | 76,851 | 23 |
| **neg_claimNoExpiryCheck** | `claim_batch` setup omits `time.monotonic() >= lease_expires_at` check | **`ClaimSetupFencing` violated** | 1,589 | 10 |
| **neg_retryPastExpiry** | hidden `urllib3` retries stretch `_delete_claim` past `expAt` | **`BelieverKeepsClaims` violated** | 351,147 | 21 |

Additional negative controls (each breaks one rule on purpose; trace lengths match `summary.txt`):

| Control | Breaks | Result | Trace |
| --- | --- | --- | --- |
| `neg_reaperSubtracts` / `neg_reaperSubtracts_interval` | reaper uses `renew + D - M` ([LEASE-4](field-guide/lease.md#lease-4)) with full `D` wait vs `Interval(D)` wait | No violation (full `D` wait masks it) / **`ReaperSafety` violated** | - / 11 |
| `neg_reaperNoPrecond` (release scenario) | reaper deletes by name only ([LEASE-8](field-guide/lease.md#lease-8)) | **`ReaperSafety` violated**: deletes a recreated batch's Lease | 26 |
| `neg_reaperNoPrecond_hand` (handoff scenario) | same | **`BelieverIsHolder` violated**: deletes an attacher's Lease | 23 |
| `neg_reaperNoWait_skew` (`SkewMax=5`) | reaper skips the confirmation wait | **`ReaperSafety` violated** | 14 |
| `neg_reaperNoWait` (`SkewMax=2`) | same, skew within `M=2` | No violation: the skew margin covers it | - |
| `neg_expiryFromResp` (`SkewMax=2`) / `neg_expiryFromResp_skew9` (`SkewMax=9`) | holder sets expiry from reply time ([LEASE-5](field-guide/lease.md#lease-5)) | No violation at `SkewMax <= 8` / **`ReaperSafety` violated** at `SkewMax=9` | - / 16 |
| `neg_expiryFromResp_noWait` | reply-time expiry + reaper skips wait | **`ReaperSafety` violated** | 17 |
| `neg_holderOnlyFence` | release fences on holder only ([LEASE-17](field-guide/lease.md#lease-17)) | **`ReleaseFencing` violated**: deletes a batch another handle parked | 16 |
| `neg_getBatchAdds` | `get_batch` adds the margin ([LEASE-4](field-guide/lease.md#lease-4)) | No violation: empty-holder + `rv` precondition cover it | - |
| `neg_unconditionalRenew` | renewal as an unconditional write ([LEASE-2](field-guide/lease.md#lease-2)) | **`BelieverIsHolder` violated**: a late renewal steals the Lease back ([LEASE-16](field-guide/lease.md#lease-16)) | 13 |

## What we learned

<a id="tla-1"></a>

## TLA-1 A round that finds no Lease must not delete by batch-id alone

- **Principle:** "No Lease exists" at read time is not "no Lease exists" at delete time. A destructive step scoped by a reusable name (a label, a batch id) needs a fence that holds when the step lands, not when the decision happens.
- **What TLC found (`neg_unscopedReleaseDelete`):** 22 states, with every handle connected and no lost message. Handle h1 calls `release()`, but its Lease is gone: the release stalled long enough for the reaper to delete it ([LEASE-19](field-guide/lease.md#lease-19) bounds each round, but nothing bounds a process pause before the first take). GC then removes h1's claims. h1's `_take_batch_lease` reads no Lease and returns `None`. That round still counts as held, so pre-22/N `_delete_claims_in_rounds` sent `deletecollection` with `batch-id=<id>` alone. In between, h2 calls `claim_batch` with the same caller-chosen id: the Lease create succeeds (new UID), the claim list is empty, and h2 creates its claims. h1's `deletecollection` lands and deletes them. h1's next round would see the new UID and raise `BatchInUseError` ([LEASE-17](field-guide/lease.md#lease-17)), but Round 1's request has already landed.
  ```text
  t=0  h1 claim_batch           uid=1, claims of uid 1
  t=0  h1 release() starts, pauses before its first take
  t=10 reaper deletes Lease uid=1 (stale, unchanged for full D); GC deletes uid 1's claims
  t=10 h1 take: no Lease -> deletecollection batch-id=b1 (in flight)
  t=10 h2 claim_batch b1        uid=2, list empty, creates claims
  t=10 h1 deletecollection lands -> h2's claims deleted
  ```
- **Why our prose missed it:** [LEASE-18](field-guide/lease.md#lease-18) says each round's take means "every delete is sent while this handle holds the Lease, or no Lease exists". The first half fences the delete with a live Lease; the second half reads a point-in-time snapshot. [LEASE-1](field-guide/lease.md#lease-1)'s rule ("a claim without a Lease is an orphan") holds at any instant but says nothing about a delete decided at one instant and applied at the next.
- **Fixes we checked and shipped in 22/N:** Skipping the label delete in a no-Lease round (`neg_unscopedReleaseDelete_skipNone`) fails in 18 states because a `deletecollection` from Round 1 whose client saw a transport error can still land after Round 2 finishes and h2 creates a new batch. Labeling every batch claim with `agents.x-k8s.io/batch-lease-uid=<lease_uid>` and scoping `_attach` (when a Lease exists), `_watch_loop`, `_delete_batch_objects`, and `release_group` selectors to `(batch-id, batch-lease-uid)` (shipped baseline in 22/N) passes `main_release` (`24.2M` distinct states) and `main_fence` (`19.7M` distinct states).

<a id="tla-2"></a>

## TLA-2 Wait a full `leaseDurationSeconds` before reaping, and bound member deletes by expiry

- **Principle:** "Unchanged for one heartbeat interval" proves death only if a live writer's successive writes land at most one interval apart on the server. Waiting a full `leaseDurationSeconds` aligns the reaper's confirmation window with the holder's local expiry budget (`sent_at + D`).
- **What TLC found (`neg_reaperWaitInterval_skew6` / `neg_reaperWaitInterval_flaky_skew6`):** Pre-22/N `batch_reaper.py` waited only `max(1, D // 3)` (`I = 2`). With the reaper 6 ticks ahead, `neg_reaperWaitInterval_skew6` violates `ReaperSafety` in 16 states even for a connected, timely holder, because `_renew_loop` sleeps `I` *after* `_renew_once` returns, spacing server-side renewals up to `I + 2*Lat` apart; `neg_reaperWaitInterval_flaky_skew6` fails in 10 states for a stalled holder.
- **What 22/N shipped and what TLC proved:** Waiting the full `leaseDurationSeconds` (`rInt = lease.dur`, 22/N Commit 3) makes `skew6_connected` and `skew12_connected` pass (`10,447` and `32,965` distinct states), and even Lease-only `skew8_flaky`, `skew9_flaky`, and `skew10_flaky` pass (`1.23M` to `1.59M` states) because causal ordering (`rAt >= sentAt`) plus send-time expiry (`expAt = sentAt + D`) implies `rAt + D >= expAt` for arbitrary wall-clock skew. Where wall-clock skew still matters is in-flight by-name member deletes (`_delete_claim`), which carry no UID precondition: `skew8_member_flaky` (`SkewMax = D + M = 8`) passes (`908,037` states) while `skew9_member_flaky` (`SkewMax = D + M + 1 = 9`) violates `BelieverKeepsClaims` in 15 states when a delete sent at `expAt - 1` spends `Lat = 1` tick in flight, mechanically matching 22/N Commit 3's `skew_margin + leaseDurationSeconds - in_flight_request_latency` bound.

<a id="tla-3"></a>

## TLA-3 The resourceVersion precondition carries the load; the thresholds back it up

- **Principle:** When a negative control does not fire, find out why. Either the model cannot see the bug, or another defense masks it, and both tell you what carries the safety proof ([VER-6](verification.md#ver-6)).
- **What TLC showed:** Four single-fault controls pass at default parameters because another mechanism covers them: `neg_getBatchAdds` (adopt up to `renew + D + G`) is covered by the empty-holder rule ([K8S-16](field-guide/k8s.md#k8s-16)) and `replace` precondition; `neg_reaperSubtracts` (`renew + D - M`) is covered by the full `D` confirmation wait (reverting to `Interval(D)` in `neg_reaperSubtracts_interval` fails in 11 states); `neg_expiryFromResp` is covered by the full `D` wait while `SkewMax <= D + M = 8` (at `SkewMax = 9`, `neg_expiryFromResp_skew9` fails in 16 states); and `neg_reaperNoWait` is covered while `SkewMax <= M` (`neg_reaperNoWait_skew` fails in 14 states). Removing `resourceVersion` preconditions, by contrast, breaks safety immediately: `neg_reaperNoPrecond` (26 states), `neg_reaperNoPrecond_hand` (23 states), `neg_unconditionalRenew` (13 states), and `neg_unconditionalRollbackDelete` on `claim_batch` setup rollback (23 states).
- **What it means for the prose:** Keep send-time expiry ([LEASE-5](field-guide/lease.md#lease-5)), subtracted attach margin ([LEASE-4](field-guide/lease.md#lease-4)), post-list setup expiry check (`neg_claimNoExpiryCheck`, 10 states), and unretried batch custom-object calls (`neg_retryPastExpiry`, 21 states) alongside `resourceVersion` preconditions so each defense backs up the others.

<a id="tla-4"></a>

## TLA-4 Model time as a clock plus a timeliness rule

- **Principle:** You rarely need real-valued clocks. A global integer `now`, per-actor offsets, and a guard on `Tick` that says "time may not pass while a connected actor owes a step" give you bounded delay, timeouts, and partitions in a few lines.
- **How we applied it:** Connected handles finish setup within `Lat`, renew on schedule, and deliver messages within `Lat`; a flaky handle is exempt, modeling a partition, a lost reply, or a `SIGSTOP`. The two kinds of handle answer two different questions: `ReaperSafetyConnected` asks whether the confirmation wait protects a live holder, and `ReaperSafety` / `BelieverKeepsClaims` ask what skew bound protects a stalled one.
- **Watch out:** Model-level retries need the same backoff as the code. Our first draft retried a failed renewal in the same tick, and TLC never finished: each retry minted a new resourceVersion within one tick, leaving the state space unbounded. Modeling `backoff_delay` as a one-tick wait made the state space finite and fast to explore.

<a id="tla-5"></a>

## TLA-5 Shrink the state space with the problem, not against it

- **Principle:** Cut states only in ways you can argue are sound, and write the argument next to the cut.
- **How we applied it:** `SYMMETRY` over the two handles (they are interchangeable). The reaper's skew is fixed at `SkewMax`, because a reaper further ahead can act at every time a less-skewed one can. A crash only matters while holding (elsewhere it looks like a handle that stops). At most one handle is flaky. Message identity is the message record itself, not a global counter, so equivalent histories collapse. Each feature set runs separately (`Features`), keeping the largest run (`main_release`, `24.2M` distinct states) under 3 minutes on 24 workers.
- **Watch out:** Every cut can hide a bug ([VER-1](verification.md#ver-1)'s "too small proves nothing"). Size `MaxTime` so `ReapList` plus the full `D = 6` confirmation wait can fire (`MaxTime >= 10` at `SkewMax = 5`, `MaxTime = 14` in dedicated skew and setup/member runs).

<a id="tla-6"></a>

## TLA-6 Decompose intra-process subsystems into small specs instead of inflating one cross-process model

- **Principle:** Do not bolt intra-process state machines (watch relisting, consumer queues, create pacing, handle pooling) onto a cross-process clock-and-network spec. Prove the cross-process ownership boundary once (`BatchLease.tla`), then assume that boundary as an environment contract inside focused `4`-to-`7`-variable subsystem specs.
- **Why it works:** State spaces multiply across independent axes. `BatchLease.tla` already visits `24.2M` states for two handles, a reaper, and clock skew; crossing that with multi-page watch relists, three consumer modes, and `max_in_flight` workers would exceed `10^9` states. Splitting into `5` subsystem specs (`BatchHandle`, `BatchWatch`, `BatchConsumer`, `BatchAcquireRelease`, `RLBatchLifecycle` in `handoff/work/tla/`) checked all `22` positive and negative configurations (`3,275,347` distinct states) in `56 s`.
- **How to choose the seam:** Give each spec one contested boundary and abstract everything else to nondeterministic environment moves ([VER-5](verification.md#ver-5)):
  - `BatchHandle.tla` (`3,280` states): watch loop vs. Lease renew loop vs. active sandboxes.
  - `BatchWatch.tla` (`228,680` states): watch event stream, `410 Gone` multi-page relists, tombstones, and `_connected` cache eviction.
  - `BatchConsumer.tla` (`1,884,448` states): `wait_for_quorum`, `group_outcomes`, `events`, dynamic ordinals, and popped pacing slots.
  - `BatchAcquireRelease.tla` (`2,016` states): concurrent initial fill, caller-thread `acquire()`, `release_*`, and teardown drain barriers.
  - `RLBatchLifecycle.tla` (`643` states): `BatchClaims._batch_for` races, `release_all` retry retention, and `_closed` transitions.
- **Watch out:** Every seam is an assumption ([VER-12](verification.md#ver-12)). Write down what each subsystem spec assumes from its neighbors (for example, `BatchWatch` assumes Lease fencing from `BatchLease`) and check the glue in integration or fault-injection tests.

<a id="tla-7"></a>

## TLA-7 Model failure channels per authority domain so readiness errors cannot masquerade as ownership loss

- **Principle:** Whenever a handle runs multiple background loops (such as a status watch loop and a Lease heartbeat loop), give each authority domain its own error variable in the spec and assert an explicit `AuthoritySeparation` invariant.
- **What TLC found (`BatchHandle.tla`, `bh_neg_janet` & `bh_neg_janet_release`):** Modeling `watch_loop` writing fatal `403` errors into a single shared `stateError` read by `renew_loop` and `release_member` violated `ActiveWorkloadProtected` (`74` distinct states) and `CleanMemberReleaseAllowed` (`11` distinct states): a watch permission error stopped Lease heartbeats while sandboxes were actively executing user code, letting the reaper GC live pods (`F-01`, [CHAOS-5](chaos-verification-suite.md#chaos-5)).
- **Reusable invariant pattern:**
  ```tla
  \* Observation failure must never halt ownership renewal while workloads run.
  ActiveWorkloadProtected ==
    (activeSandboxes > 0 /\ ~detached /\ ~leaseReleased /\ leaseError = "NONE")
      => renewState /= "STOPPED"
  ```

<a id="tla-8"></a>

## TLA-8 Mind TLC's three CLI sharp edges: inverted `-deadlock`, `-workers auto` trace jitter, and `0`-hit vacuity (`-coverage 1`)

- **Principle:** Configure TLC differently for bounded-clock protocol specs, reactive subsystem specs, and negative controls, and always verify action coverage before trusting a green run.
- **Three sharp edges that bit us:**
  1. **`-deadlock` turns deadlock checking OFF:** TLC checks for deadlocks by default; passing `-deadlock` *disables* that check. Finite-clock specs (`BatchLease.tla`, where `Tick` stops at `MaxTime = 14`) need `-deadlock` so `now = MaxTime` is not flagged as a deadlock. Event-driven subsystem specs (`BatchHandle`, `BatchWatch`, `BatchConsumer`, `BatchAcquireRelease`, `RLBatchLifecycle`) must **omit `-deadlock`** and provide explicit clean-terminal stuttering (`Terminated == ... /\ UNCHANGED vars`) so genuine terminal hangs fail immediately.
  2. **`-workers auto` makes negative-control state counts jitter:** Parallel BFS workers explore queues concurrently, so counterexample state counts and trace lengths can vary slightly across runs. Use `-workers auto` on positive safety sweeps for throughput and `-workers 1` on negative controls for deterministic state counts and shortest traces.
  3. **Vacuous passes hide behind `0`-hit actions:** A typo in a Next-state guard leaves an invariant green because the buggy branch never executes. Run TLC with `-coverage 1` whenever adding or editing actions and confirm every action fires `> 0` times.

<a id="tla-9"></a>

## TLA-9 Small `2`-element domains catch almost every tombstone, pagination, and slot-accounting bug

- **Principle:** Under Jackson's small-scope hypothesis, `2` claims, `2` warm pools, `2` list pages, and sequence numbers `0..2` suffice to trigger nearly every set-partition, tombstone-resurrection, and mid-relist race without blowing up state counts.
- **What `2`-element domains caught (`handoff/work/tla/`):**
  - **Late `ADDED` tombstoned with `needs_delete=False` (`bw_neg_late_added`, `20` states):** `1` claim created just before `release_not_ready()` arriving via watch `ADDED` afterward violated `NoOrphanedLiveReleasedClaim` (`F-03`).
  - **Dropped page 1 on `410` relist & tombstone resurrection (`bw_neg_drop_page1` `412` states, `bw_neg_resurrect` `142` states):** `2` pages of `1` claim each caught premature `LOST` eviction when page 1 is discarded before page 2 finishes, and caught released claims coming back to life on relist.
  - **Popped pacing-slot stall (`bc_neg_popped_slot`, `33` states) & `_waiting` leak (`bc_neg_waiting_leak`, `77` states):** `2` groups with `1` popped worker slot caught `release_group()` missing already-popped items (`F-25`) and group timeout clearing `_waiting` without updating `_resolved` (`F-12`).
  - **Caller-thread `acquire()` vs. `release()` (`bar_neg_inflight_release`, `88` states):** `1` fill claim + `1` dynamic `acquire()` caught `release()` setting `_detached = True` while `acquire()`'s `POST` was in flight (`F-24`).

## Limits of the model

- **Bounded instance (`BatchLease.tla`).** Two handles, one reaper, at most two Lease incarnations, at most two release rounds, and 7 to 14 ticks (`MaxTime`). TLC proves the seven cross-process invariants within these bounds.
- **Coarse time.** One tick represents about 10 s and `Lat = 1`, so a single network leg can take a full tick. Ties within a tick resolve nondeterministically in either order.
- **Subsystem coverage (`runall_subsystems.sh`).** While `BatchLease.tla` abstracts intra-handle subsystems to keep cross-process state counts tractable, the `5` companion subsystem specs ([TLA-6](#tla-6)) model `_watch_loop` + `410` paged relisting (`BatchWatch.tla`), consumer modes + pacing slots (`BatchConsumer.tla`), dynamic `acquire()` + `release_*` drain barriers (`BatchAcquireRelease.tla`), watch/renew authority separation (`BatchHandle.tla`), and `BatchClaims` pooling (`RLBatchLifecycle.tla`). Client `atexit` hooks ([LEASE-22](field-guide/lease.md#lease-22)), HTTP library framing (`_preload_content=False`, [LIB-23](field-guide/lib.md#lib-23)), and OS socket behavior ([LIB-22](field-guide/lib.md#lib-22)) remain outside TLA+ and are covered by deterministic Python fault-injection and differential tests ([CHAOS-1](chaos-verification-suite.md#chaos-1)).
- **No temporal liveness (`<>`) in bounded-clock runs.** `BatchLease.tla`'s bounded clock stops at `MaxTime`, so it checks safety invariants; the `5` subsystem specs run with deadlock checking enabled (`0` deadlocks).
- **The model is not the code.** Every TLA+ finding and negative control has a corresponding deterministic Python test ([VER-9](verification.md#ver-9), [Automated batch verification and chaos suite](chaos-verification-suite.md)).

## How to rerun it

```bash
cd handoff/work/tla
curl -L -o tla2tools.jar https://github.com/tlaplus/tlaplus/releases/latest/download/tla2tools.jar
./configs.sh            # generates all 31 .cfg files
./runall.sh             # runs all 31 configs (~8 minutes on 24 cores) and writes summary.txt
java -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC -workers auto -deadlock -config main_claim_setup.cfg BatchLease.tla
python3 trace.py logs/neg_unscopedReleaseDelete.log   # prints only what changed in each step
```

`-deadlock` turns off deadlock checking, because the bounded clock stops every behavior at `MaxTime`. To try a new idea, add a name to `Bugs` or `Fixes`, guard the changed line with `"name" \in Bugs`, and add a line to `configs.sh`. Rerun the spec whenever a change touches Lease writes, reaper timing, or release rounds ([VER-7](verification.md#ver-7)).

## Further reading

- Leslie Lamport, [The TLA+ Home Page](https://lamport.azurewebsites.net/tla/tla.html): the language, the tools, the video course, and *Specifying Systems*.
- Hillel Wayne, [Learn TLA+](https://learntla.com): the friendliest free introduction, with PlusCal and pure TLA+.
- Hillel Wayne, *Practical TLA+: Planning Driven Development*, Apress 2018 ([doi:10.1007/978-1-4842-3829-5](https://doi.org/10.1007/978-1-4842-3829-5)).
- Chris Newcombe, Tim Rath, Fan Zhang, Bogdan Munteanu, Marc Brooker, Michael Deardeuff, "How Amazon Web Services Uses Formal Methods", *Communications of the ACM* 58(4), 2015 ([doi:10.1145/2699417](https://doi.org/10.1145/2699417)): why design-level model checking finds bugs that tests and reviews miss.

---

[Learnings home](../learnings.md) · [Verification](verification.md)
