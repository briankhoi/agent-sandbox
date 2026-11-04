<a id="sec-tla"></a>

[Learnings home](../learnings.md) · [Verification](verification.md)

# Verifying the Lease protocol with TLA+

The in-process checker in this page's sibling ([verification.md](verification.md)) explores every interleaving of the threads inside one handle. The batch Lease protocol is a different beast: it runs across processes. Two handles in two Python processes, a reaper CronJob, the apiserver, and the garbage collector all act on one `coordination.k8s.io/v1` Lease, with clocks that disagree, requests that land late, and replies that never arrive. We wrote a TLA+ specification of that protocol, checked it with TLC, the standard TLA+ model checker, and found one real bug, one argument that needed an extra assumption, and a clearer picture of which defenses carry the load. The spec lives at `handoff/work/tla/BatchLease.tla`, next to its configs, run scripts, and full results (`results.md`).

## Why TLA+ here, and not another BFS checker

Our hand-rolled checker ([VER-1](verification.md#ver-1) through [VER-11](verification.md#ver-11)) works because one handle's threads share memory and their atomic steps are easy to name ([VER-2](verification.md#ver-2)). The Lease protocol has none of that. Its safety rests on four mechanisms that live in different processes:

- **Time thresholds** that three parties evaluate on three clocks ([LEASE-4](field-guide/lease.md#lease-4)): `get_batch` refuses at `renewTime + D - 5 s`, the holder gives up at `last send + D` ([LEASE-5](field-guide/lease.md#lease-5)), and the reaper picks a Lease only after `renewTime + D + margin`.
- **Optimistic concurrency**: every Lease write carries the last-seen resourceVersion, and the apiserver rejects a stale one with 409 ([LEASE-2](field-guide/lease.md#lease-2), [K8S-1](field-guide/k8s.md#k8s-1)).
- **The reaper's confirmation wait**: list, wait one renew interval, then delete with a resourceVersion precondition ([LEASE-8](field-guide/lease.md#lease-8)).
- **Release fencing** on Lease UID plus `leaseTransitions` ([LEASE-16](field-guide/lease.md#lease-16), [LEASE-17](field-guide/lease.md#lease-17), [LEASE-18](field-guide/lease.md#lease-18)), and GC by owner UID ([K8S-2](field-guide/k8s.md#k8s-2)).

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
  /\ rInt' = Interval(lease.dur)
  /\ UNCHANGED <<now, lease, claims, net, ...>>
```

`/\` is "and", `\/` is "or", and `UNCHANGED x` means `x' = x`. Forgetting a variable in an action leaves it unconstrained, and TLC reports that as an error, which catches a whole class of modeling slips.

**`Next` is the disjunction of every action**, with existential quantifiers for "some handle" or "some message":

```tla
Next ==
  \/ Tick \/ ReapList \/ ReapDelete
  \/ \E p \in Procs : Claim(p) \/ Attach(p) \/ Renew(p) \/ Detach(p) \/ StartRelease(p)
                      \/ TakeRead(p) \/ Count(p) \/ Recv(p) \/ GiveUp(p) \/ Crash(p) \/ GoFlaky(p)
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
| `Claim(p)` | `SandboxBatch._claim` | Create-if-absent the Lease with a fresh UID, refuse if any claim with the label exists, create claims owned by the UID. Expiry `now + D`, read before stamping ([LEASE-5](field-guide/lease.md#lease-5)). |
| `Attach(p)` | `SandboxBatch._attach` | Only if the holder is empty and `now <= renew + dur - G`; send a takeover write at the read resourceVersion with `leaseTransitions + 1` ([LEASE-14](field-guide/lease.md#lease-14)). |
| `Renew(p)` | `_renew_loop`, `_renew_once`, `_write_renewal` | Send a PUT at the last written resourceVersion, timeout `min(I, time left)`. On success, expiry is `sent_at + D`. On 409, read and rewrite only if still the holder ([LEASE-28](field-guide/lease.md#lease-28)); on 404, stop ([LEASE-7](field-guide/lease.md#lease-7)). |
| `Detach(p)` | `SandboxBatch.detach` | After renewal is joined, read; if still holder, write holder=None with the grace; on 409 re-read and retry once ([LEASE-15](field-guide/lease.md#lease-15), [LEASE-16](field-guide/lease.md#lease-16)). |
| `StartRelease`, `TakeRead`, `Count` | `release`, `_delete_batch_objects`, `_take_batch_lease`, `_delete_claims_in_rounds` | Per round: read, fence on UID and `leaseTransitions` and holder ([LEASE-17](field-guide/lease.md#lease-17)), conditional take, deletecollection by label, consistent count, delete the Lease at the taken resourceVersion when 0 are left ([LEASE-18](field-guide/lease.md#lease-18)). |
| `Recv`, `GiveUp` | the HTTP client | A reply arrives, or the client times out (or, if flaky, errors at any time) while the request may still land later ([LEASE-16](field-guide/lease.md#lease-16)). |
| `Deliver(m)` | the apiserver | Apply a write only if the resourceVersion matches (else 409, or 404 if gone). |
| `ReapList`, `ReapDelete` | `find_stale_leases`, `reap`, `_delete_lease_if_unchanged` | Pick at `renew + dur + M` on the reaper's clock, wait `Interval(dur)`, delete with a resourceVersion precondition ([LEASE-8](field-guide/lease.md#lease-8), [LEASE-9](field-guide/lease.md#lease-9)). |
| `GC(u)` | the garbage collector | Delete claims whose owner UID no longer exists ([K8S-2](field-guide/k8s.md#k8s-2)). |
| `Crash(p)`, `GoFlaky(p)` | the environment ([VER-5](verification.md#ver-5)) | A holder dies; or a handle's requests and replies may be lost, it may see transport errors, and it may pause. |
| `Tick` | time | Advance `now` by one, but only if every connected handle is timely. |

**Modeling time.** A global integer clock plus one rule does most of the work. `Tick` may fire only when no connected (non-flaky) handle owes the world a step: a due renewal, a request that must land within `Lat`, or a reply that must arrive within `Lat`. A flaky handle is exempt, which is how the model says "partitioned or paused".

```tla
Timely(p) ==
  alive[p] /\ ~flaky[p] =>
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

**The invariants**, in plain English:

- `ReaperSafety`: the reaper never deletes the Lease of a live handle that still believes it holds it. `ReaperSafetyConnected` is the same, restricted to connected, timely believers.
- `AtMostOneBeliever`: at most one live handle believes it holds the batch.
- `BelieverIsHolder`: a believer's belief is backed by the server: its Lease incarnation exists and names it as holder.
- `BelieverKeepsClaims`: a believer's claims still exist.
- `ReleaseFencing`: `release()` never deletes claims of a live Lease incarnation it does not own, nor of a batch another handle attached to since this handle last wrote the Lease (a ghost flag set when a deletecollection lands).

**Negative controls and fixes** are switches in two `CONSTANTS`, `Bugs` and `Fixes`, so one spec serves every run ([VER-6](verification.md#ver-6)). A third constant, `Features`, turns optional moves on and off, so each run checks a feature set that fits in a few minutes.

## Results

All runs use TLC 2.19 with 24 workers. "Trace" is the length of the shortest counterexample in states. Full numbers, commands, and condensed traces are in `results.md`.

| Run | What it checks | Result | Distinct states | Trace |
| --- | --- | --- | --- | --- |
| main_core | claim, renew, attach, detach, crash, flaky, reaper, GC (23/N code) | All six invariants hold | 2,517,908 | - |
| main_release | claim, renew, release, crash, flaky, reaper, GC, with the `uidScoped` fix | All hold | 11,091,843 | - |
| main_fence | claim, renew, attach, detach, release, flaky, `uidScoped`, `MaxTime=7` | All hold | 19,655,559 | - |
| **finding_23n** | release as shipped, no failures at all | **`BelieverKeepsClaims` violated** | 7,844 | 19 |
| fix_skipOnNone | release, alternative fix "skip the label delete when there is no Lease" | `BelieverKeepsClaims` violated | 110,056 | 18 |
| skew5_connected | connected holder, reaper 5 ticks (50 s) ahead, margin 2 | Holds | 1,165 | - |
| **skew6_connected** | same, 6 ticks ahead | **`ReaperSafety` violated** | 2,046 | 16 |
| skew6 / skew12_connected_waitLonger | reaper waits `I + 2*Lat + 1` instead of `I` | Holds | 2,339 / 8,846 | - |
| skew5_flaky / skew6_flaky | holder that stops renewing, reaper 5 / 6 ticks ahead | Holds / violated | 50,756 / 1,272 | - / 11 |

Negative controls (each breaks one rule on purpose):

| Control | Breaks | Result | Trace |
| --- | --- | --- | --- |
| `reaperSubtracts` | reaper uses `renew + D - M` ([LEASE-4](field-guide/lease.md#lease-4)) | `ReaperSafety` violated | 10 |
| `reaperNoPrecondition` (release scenario) | reaper deletes by name only ([LEASE-8](field-guide/lease.md#lease-8)) | `ReaperSafety` violated: it deletes a recreated batch's Lease | 23 |
| `reaperNoPrecondition` (handoff scenario) | same | `ReaperSafety` violated: it deletes an attacher's Lease | 20 |
| `reaperNoWait`, skew 5 | reaper skips the confirmation wait | `ReaperSafety` violated | 13 |
| `reaperNoWait`, skew 2 | same, skew within the margin | No violation: the margin covers it | - |
| `expiryFromResponse` | holder expiry from the reply time ([LEASE-5](field-guide/lease.md#lease-5)) | No violation: the reaper's wait covers it | - |
| `expiryFromResponse` + `reaperNoWait` | both | `ReaperSafety` violated | 18 |
| `holderOnlyFence` | release fences on holder only ([LEASE-17](field-guide/lease.md#lease-17)) | `ReleaseFencing` violated: deletes a batch another handle parked | 17 |
| `getBatchAdds` | `get_batch` adds the margin ([LEASE-4](field-guide/lease.md#lease-4)) | No violation: the precondition covers it | - |
| `unconditionalRenew` | renewal as an unconditional write ([LEASE-2](field-guide/lease.md#lease-2)) | `BelieverIsHolder` violated: a late renewal steals the Lease back ([LEASE-16](field-guide/lease.md#lease-16)) | 13 |

## What we learned

<a id="tla-1"></a>

## TLA-1 A round that finds no Lease must not delete by label

- **Principle:** "No Lease exists" at read time is not "no Lease exists" at delete time. A destructive step scoped by a reusable name (a label, a batch id) needs a fence that holds when the step lands, not when the decision was made.
- **What TLC found:** `finding_23n`, 19 states, with every handle connected and no lost message. Handle h1 calls `release()`, but its Lease is gone: the release stalled long enough for the reaper to take it ([LEASE-19](field-guide/lease.md#lease-19) bounds each round, but nothing bounds a process pause before the first take). GC then removes h1's claims. h1's `_take_batch_lease` reads no Lease and returns `None`. That round still counts as held, so `_delete_claims_in_rounds` sends deletecollection with `batch-id=<id>`. In between, h2 calls `claim_batch` with the same caller-chosen id: the Lease create succeeds (new UID), the claim list is empty, and h2 creates its claims. h1's deletecollection lands and deletes them. h2's next round never runs; h1's next round would see the new UID and raise `BatchInUseError` ([LEASE-17](field-guide/lease.md#lease-17)), but the first request has already landed.
  ```text
  t=1  h1 claim_batch           uid=1, claims of uid 1
  t=1  h1 release() starts, stalls before its first take
  t=10 reaper deletes Lease uid=1 (stale, unchanged for one interval); GC deletes uid 1's claims
  t=10 h1 take: no Lease -> deletecollection batch-id=b1 (in flight)
  t=10 h2 claim_batch b1        uid=2, list empty, creates claims
  t=10 h1 deletecollection lands -> h2's claims deleted
  ```
- **Why our prose missed it:** [LEASE-18](field-guide/lease.md#lease-18) says each round's take means "every delete is sent while this handle holds the Lease, or no Lease exists". The first half is a lease the delete is fenced by; the second half is a snapshot. [LEASE-1](field-guide/lease.md#lease-1)'s rule ("a claim without a Lease is an orphan") is true at any instant but says nothing about a delete decided at one instant and applied at the next.
- **Fixes we checked:** Skipping the label delete in a no-Lease round (`skipOnNone`) is not enough: TLC found an 18-state trace where a deletecollection from an earlier round, whose client saw a transport error, lands after the release finished and a new batch was created. Scoping every release delete to the Lease UID (`uidScoped`: label claims with the Lease UID and select on both labels, or delete by name with a `preconditions.uid` per claim whose ownerReference matches) passes `main_release` and `main_fence` (11.1M and 19.7M states). Adding a label is API-visible, so treat it as a design change. The window needs a reused batch id and a stalled release, so it is narrow, but the damage is another batch's sandboxes.

<a id="tla-2"></a>

## TLA-2 The confirmation wait bounds skew; it does not make skew irrelevant

- **Principle:** "Unchanged for one heartbeat interval" proves death only if a live writer's successive writes land at most one interval apart, as the server sees them. Check that against how the writer actually schedules its heartbeats.
- **What TLC found:** With the reaper 6 ticks ahead and a connected, timely holder, `skew6_connected` violates `ReaperSafety` in 16 states. The renew loop sends the next renewal `I` after the previous call returns (`_renew_loop` waits `_renew_interval` after `_renew_once`), so on the server two renewal writes land up to `I + reply leg + request leg` apart, not `I`. The reaper waits exactly `I` after its list returns. If its list lands just after one renewal, its precondition delete can beat the next one. At skew 5 the timestamp check delays the list enough and the run passes.
- **The extra assumption:** [LEASE-8](field-guide/lease.md#lease-8) says a timestamp check alone cannot tell a dead driver from a slow-clock driver "however large the margin is", and the confirmation wait fixes that. The model agrees the wait helps a lot, but it shows the protocol is safe only while the reaper's clock is ahead by less than about `D + M - (reply leg + request leg)` for a connected holder, and less than `M + I` for a holder that has stopped renewing (`skew6_flaky`, 11 states). With the defaults (D = 60 s, M = 60 s, I = 20 s, each renewal call bounded by `min(I, time left)`), both bounds come to about 80 s in the worst case. That is generous for NTP-synced nodes, but it is a bound, and a reaper started with the minimum `--skew-margin 5` shrinks the stalled-holder bound to about 25 s.
- **A cheap fix we checked:** waiting `I` plus the longest reply-plus-request leg (`reaperWaitLonger`) passes at skew 6 and skew 12. In the real code that is about `3 * renew_interval`, since each renewal call is bounded by one interval. A stalled holder stays protected only by timing, as it must; the longer wait raises that bound to `M + 3I`.

<a id="tla-3"></a>

## TLA-3 The resourceVersion precondition carries the load; the thresholds are a second line

- **Principle:** When a negative control does not fire, find out why. Either the model cannot see the bug, or another defense masks it, and both are worth knowing ([VER-6](verification.md#ver-6)).
- **What TLC showed:** Three controls passed on their own. `getBatchAdds` (adopt up to `renew + D + G`) is harmless because `get_batch` only adopts a Lease with an empty holder ([K8S-16](field-guide/k8s.md#k8s-16)), and any reaper delete or takeover race is settled by the resourceVersion precondition. `expiryFromResponse` (the pre-3/N [LEASE-5](field-guide/lease.md#lease-5) bug) is masked by the reaper's wait: as long as the reply arrives within one interval, the holder gives up before the reaper's delete. `reaperNoWait` is masked by the margin while skew stays within it. Pair any two and TLC finds a violation (`expiryFromResponse` + `reaperNoWait`: 18 states). Removing the precondition, by contrast, breaks safety on its own in two different ways (20 and 23 states). One of them surprised us: a holder whose renewal stalled can still call `detach()` after its own expiry, and detach rewrites `renewTime`, so a Lease the reaper listed as stale becomes fresh and adoptable before the reaper's delete. Only the precondition stops that delete.
- **What it means for the prose:** [LEASE-4](field-guide/lease.md#lease-4)'s first argument (skew lets an attacher adopt a batch whose holder still thinks it owns it) no longer applies at 23/N, because the empty-holder rule already excludes that. The subtracted margin and the send-time expiry stay worth keeping as defense in depth, and the model tells you exactly which other defense each one backs up.

<a id="tla-4"></a>

## TLA-4 Model time as a clock plus a timeliness rule

- **Principle:** You rarely need real-valued clocks. A global integer `now`, per-actor offsets, and a guard on `Tick` that says "time may not pass while a connected actor owes a step" give you bounded delay, timeouts, and partitions in a few lines.
- **How we applied it:** Connected handles renew on schedule and their messages land within `Lat`; a flaky handle is exempt, which models a partition, a lost reply, or a SIGSTOP. The two kinds of handle answer two different questions: `ReaperSafetyConnected` asks whether the confirmation wait protects a live holder, `ReaperSafety` asks whether timing alone protects a stalled one.
- **Watch out:** Model-level retries need the same backoff as the code. Our first draft retried a failed renewal in the same tick, and TLC never finished: each retry minted a new resourceVersion within one tick, so the state space was unbounded. The real loop backs off (`backoff_delay`). Modeling that one-tick backoff took a 6-tick core run from 5.7M states and still growing (search depth 78) to 134,591 states in 3 seconds.

<a id="tla-5"></a>

## TLA-5 Shrink the state space with the problem, not against it

- **Principle:** Cut states only in ways you can argue are sound, and write the argument next to the cut.
- **How we applied it:** `SYMMETRY` over the two handles (they are interchangeable). The reaper's skew is fixed at its maximum, because a reaper further ahead can act at every time a less-skewed one can. A crash only matters while holding (elsewhere it looks like a handle that stops). At most one handle is flaky. Message identity is the message itself, not a global counter, so equivalent histories collapse. Each feature set runs separately (`Features`). An all-features run at `MaxTime = 10` passed 36M distinct states without finishing; split, the largest run takes 2.5 minutes.
- **Watch out:** Every cut is a place a bug can hide ([VER-1](verification.md#ver-1)'s "too small proves nothing"). Our fence scenario runs to `MaxTime = 7`, which covers detach, attach, detach, release in a few ticks but not the reaper.

## Limits of the model

- **Bounded instance.** Two handles, one reaper, at most two Lease incarnations, at most two release rounds, 7 to 10 ticks. TLC proves the invariants for these bounds only.
- **Coarse time.** One tick is about 10 s and `Lat = 1`, so a single network leg can take a full tick. That exaggerates latency against the 20 s renew interval, which makes the [TLA-2](#tla-2) race easier to hit than in production; the bound it reveals is still real. Ties within a tick go either way.
- **Abstractions.** `claim_batch` (create, list, the fill's creates) is one atomic step with an instant reply. Reads are atomic snapshots. The reaper's list and delete are instant (in reality their latency helps safety). Claims are a set of owner UIDs, not individual objects, and deletecollection is one atomic step. Handles share the true clock; only the reaper is skewed (relative skew is what matters). Member deletes (`release_member`, `release_group`), `acquire()`, the watch, the claim count's terminating claims, client cleanup ([LEASE-22](field-guide/lease.md#lease-22)), and detach retries from cleanup are not modeled.
- **No liveness.** The bounded clock makes "eventually" meaningless here, so we did not check that a crashed holder's claims are eventually collected. A liveness check would need an unbounded clock with fairness, or a view that abstracts time.
- **The model is not the code.** We checked the spec against 23/N by reading it; the spec cannot catch a divergence between itself and the Python. [VER-9](verification.md#ver-9) applies: each counterexample above should become a deterministic unit test with doubles, starting with [TLA-1](#tla-1).

## How to rerun it

```bash
cd handoff/work/tla
curl -L -o tla2tools.jar https://github.com/tlaplus/tlaplus/releases/latest/download/tla2tools.jar
./configs.sh            # writes one .cfg per run
./runall.sh             # about 6 minutes on 24 cores; summary.txt has one line per run
java -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC -workers auto -deadlock -config finding_23n.cfg BatchLease.tla
python3 trace.py logs/finding_23n.log   # prints only what changed in each step
```

`-deadlock` turns off deadlock checking, because the bounded clock stops every behavior at `MaxTime`. To try a new idea, add a name to `Bugs` or `Fixes`, guard the changed line with `"name" \in Bugs`, and add a line to `configs.sh`. Rerun the spec whenever a change touches Lease writes, reaper timing, or release rounds ([VER-7](verification.md#ver-7)).

## Further reading

- Leslie Lamport, [The TLA+ Home Page](https://lamport.azurewebsites.net/tla/tla.html): the language, the tools, the video course, and *Specifying Systems*.
- Hillel Wayne, [Learn TLA+](https://learntla.com): the friendliest free introduction, with PlusCal and pure TLA+.
- Hillel Wayne, *Practical TLA+: Planning Driven Development*, Apress 2018 ([doi:10.1007/978-1-4842-3829-5](https://doi.org/10.1007/978-1-4842-3829-5)).
- Chris Newcombe, Tim Rath, Fan Zhang, Bogdan Munteanu, Marc Brooker, Michael Deardeuff, "How Amazon Web Services Uses Formal Methods", *Communications of the ACM* 58(4), 2015 ([doi:10.1145/2699417](https://doi.org/10.1145/2699417)): why design-level model checking finds bugs that tests and reviews miss.

---

[Learnings home](../learnings.md) · [Verification](verification.md)
