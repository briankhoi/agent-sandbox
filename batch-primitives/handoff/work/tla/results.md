# BatchLease.tla: TLC results

All runs: TLC2 2.19 (08 Aug 2024, rev 5a47802) from the official `tla2tools.jar`, OpenJDK 26.0.2.1, 24 workers, 21 GB heap, breadth-first search, `-deadlock` (the clock stops at `MaxTime`, so terminal states are expected and are not errors).

## Commands

```bash
cd scratch/tla
curl -L -o tla2tools.jar https://github.com/tlaplus/tlaplus/releases/latest/download/tla2tools.jar
./configs.sh      # writes one NAME.cfg per run below (mkcfg.sh = defaults + overrides)
./runall.sh       # runs them all, one line each in summary.txt, full output in logs/NAME.log
# a single run:
java -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC -workers auto -deadlock -config main_core.cfg BatchLease.tla
python3 trace.py logs/finding_23n.log    # condensed counterexample (only changed variables)
```

Defaults (`mkcfg.sh`): `D=6` (one tick is about 10 s, so D stands for 60 s), `I = D div 3 = 2`, `Grace=6`, `G=1` (get_batch margin), `M=2` (reaper margin), `SkewMax=2` (reaper clock ahead of the handles by exactly this, the worst case), `Lat=1` (a request lands within 1 tick, its reply arrives within 1 more), `MaxTime=10`, `MaxInc=2` (two Lease incarnations), `MaxRounds=2`, `SYMMETRY` over the two handles. Every run checks all six invariants: `ReaperSafety`, `ReaperSafetyConnected`, `AtMostOneBeliever`, `BelieverIsHolder`, `BelieverKeepsClaims`, `ReleaseFencing`. TLC stops at the first violated invariant.

Features: `core` = {crash, flaky, attach, detach}; `release` = {release, flaky, crash}; `fence` = {release, detach, attach, flaky}; `handoff` = {detach, attach, flaky}. Claim, renew, the reaper, and GC are always on.

> State counts of runs that stop at a violation vary a little between runs (parallel BFS stops wherever the first worker finds it); the trace length is stable because BFS returns a shortest counterexample. Completed runs are exact.

## Results (summary.txt of the final run)

| Run | Features, bugs, fixes | Result | States generated / distinct | Depth | Trace | Time |
| --- | --- | --- | --- | --- | --- | --- |
| main_core | core, 23/N code | No error | 5,342,061 / 2,517,908 | 60 | - | 20 s |
| main_release | release, Fixes={uidScoped} | No error | 24,549,537 / 11,091,843 | 72 | - | 84 s |
| main_fence | fence, Fixes={uidScoped}, MaxTime=7 | No error | 55,991,793 / 19,655,559 | 60 | - | 151 s |
| **finding_23n** | {release} only, 23/N code | **BelieverKeepsClaims violated** | 9,903 / 7,844 | 21 | 19 states | 2 s |
| fix_skipOnNone | release, Fixes={skipOnNone} | BelieverKeepsClaims violated | 207,653 / 110,056 | 21 | 18 | 4 s |
| skew5_connected | {} (connected holder), SkewMax=5 | No error | 1,517 / 1,165 | 27 | - | 1 s |
| **skew6_connected** | {}, SkewMax=6 | **ReaperSafety violated** (connected holder) | 2,926 / 2,046 | 23 | 16 | 2 s |
| skew6_connected_waitLonger | {}, SkewMax=6, Fixes={reaperWaitLonger} | No error | 3,036 / 2,339 | 28 | - | 2 s |
| skew12_connected_waitLonger | {}, SkewMax=12, Fixes={reaperWaitLonger} | No error | 14,626 / 8,846 | 29 | - | 2 s |
| skew5_flaky | {flaky}, SkewMax=5 | No error | 102,926 / 50,756 | 49 | - | 3 s |
| skew6_flaky | {flaky}, SkewMax=6 | ReaperSafety violated (stalled holder) | 1,775 / 1,272 | 15 | 11 | 1 s |
| neg_reaperSubtracts | {flaky}, Bugs={reaperSubtracts} | ReaperSafety violated | 1,417 / 1,020 | 14 | 10 | 2 s |
| neg_reaperNoPrecond | {release}, Bugs={reaperNoPrecondition} | ReaperSafety violated | 31,049 / 23,876 | 24 | 23 | 2 s |
| neg_reaperNoPrecond_hand | handoff, Bugs={reaperNoPrecondition} | ReaperSafety violated | 381,865 / 201,536 | 21 | 20 | 4 s |
| neg_reaperNoWait_skew | {}, SkewMax=5, Bugs={reaperNoWait} | ReaperSafety violated | 648 / 534 | 21 | 13 | 1 s |
| neg_reaperNoWait | {flaky}, Bugs={reaperNoWait} | No error (masked by the margin) | 33,547 / 17,781 | 49 | - | 2 s |
| neg_expiryFromResp | {flaky}, Bugs={expiryFromResponse} | No error (masked by the wait) | 31,265 / 16,684 | 49 | - | 3 s |
| neg_expiryFromResp_noWait | {flaky}, Bugs={expiryFromResponse, reaperNoWait} | ReaperSafety violated | 14,863 / 8,461 | 21 | 18 | 2 s |
| neg_holderOnlyFence | fence, Bugs={holderOnlyFence}, MaxTime=7 | ReleaseFencing violated | 350,201 / 178,160 | 19 | 17 | 4 s |
| neg_getBatchAdds | handoff, Bugs={getBatchAdds} | No error (masked by the precondition) | 4,499,843 / 2,131,339 | 60 | - | 18 s |
| neg_unconditionalRenew | handoff, Bugs={unconditionalRenew} | BelieverIsHolder violated | 27,056 / 15,359 | 15 | 13 | 2 s |

Not in the final table (exploration while sizing the model): `main_release` with the 23/N code (`Fixes={}`) and all features violates `BelieverKeepsClaims` with the same 18-to-19-state trace as `finding_23n`; `skew_connected` with SkewMax 7, 8, 9, 12 all violate `ReaperSafety` (traces 15, 8, 6, 7 states; at 9 the reaper lists the Lease at t=0, right after `claim_batch`, and deletes it at t=2, the tick the first renewal goes out). An all-features run at `MaxTime=10` passed 36M distinct states in 5 minutes without finishing, so the checks are split by feature set.

## Counterexamples (condensed; full ones in logs/, condensed ones in traces/)

### finding_23n: release() with no Lease deletes a newer batch's claims (19 states, no failures needed)

```text
t=1  h1 Claim                       Lease uid=1 rv=1 holder=h1 renew=1; claims={1}
t=1  h1 StartRelease                (renewal stopped; release stalls before its first take)
t=8  Reaper ReapList                8+2 > 1+6+2: stale, rv=1
t=10 Reaper ReapDelete              rv unchanged -> Lease deleted
t=10 GC                             claims of uid 1 deleted; claims={}
t=10 h1 TakeRead                    no Lease -> took=None -> sends deletecollection batch-id=<id>
t=10 h2 Claim (same batch id)       Lease uid=2 created, list finds no claims, claims={2}
t=10 deletecollection lands         claims={} -> h2 believes it holds the batch, its claims are gone
```

### fix_skipOnNone (18 states)
h1 (flaky) takes the Lease (rv=2), sends deletecollection, gets a transport error, counts, re-takes (rv=3), its second deletecollection lands, count is 0, it deletes the Lease at rv=3, h2 creates the same id (uid=2), and only then the first round's deletecollection lands and deletes h2's claims. Skipping the label delete in the no-Lease round is not enough; scoping the delete to the Lease UID (`uidScoped`) is.

### skew6_connected (16 states, connected and timely holder)
```text
t=1 Claim renew=1; t=3 Renew sent; t=4 lands (rv=2, renewTime=3); t=5 reply -> nextRenew=7
t=6 ReapList: 6+6 > 3+6+2, rv=2        t=7 Renew sent (in flight)
t=8 ReapDelete: rv still 2 -> deleted, the holder believes until 9
```
The gap between two renewal writes landing on the server is `I + reply leg + request leg` (4 ticks), not `I` (2 ticks).

### skew6_flaky (11 states)
```text
t=1 Claim (renewTime=1, the holder believes until 7)    t=2 holder goes flaky, no renewal lands
t=4 ReapList: 4+6 > 1+6+2, rv=1                          t=6 ReapDelete: rv unchanged -> deleted
```
The holder still believes at t=6. A holder that cannot renew is protected only by timing: the reaper deletes no earlier than `renew + D + M - skew + 1 + I`, which is after the holder's expiry `renew + D` only while `skew <= M + I`.

### neg_reaperSubtracts (10 states)
Claim at t=0 (believes until 6); the holder goes flaky and stops renewing; the reaper picks the Lease at t=3 (3+2 > 0+6-2) and deletes it at t=5, while the holder still believes.

### neg_reaperNoPrecond (23 states)
Reaper lists the stale uid=1 Lease (rv=1); h1's release takes it, deletes the claims and the Lease; h2 creates the same id (uid=2); the reaper's by-name delete removes uid=2's Lease while h2 believes. `neg_reaperNoPrecond_hand` (20 states): a holder whose renewal stalled calls `detach()` after expiry, which rewrites renewTime; h2 attaches; the reaper's by-name delete (from a list taken before the detach) removes h2's Lease.

### neg_reaperNoWait_skew (13 states)
SkewMax=5: the renewal sent at t=2 lands at t=3 (renewTime=2), the reply comes back at t=4, so the next renewal is due at t=6. At t=6 the reaper lists the Lease (6+5 > 2+6+2) and, with no wait, deletes it in the same tick, before the due renewal goes out. With the wait (skew5_connected) the delete would come at t=8, after that renewal lands.

### neg_expiryFromResp_noWait (18 states)
Renewal sent at t=3 (renewTime=3), reply received at t=5, so the buggy holder believes until 11 instead of 9. It goes flaky at t=7. With no confirmation wait the reaper lists at t=10 (10+2 > 3+6+2) and deletes at t=10 while the holder still believes. With the wait (neg_expiryFromResp) the delete moves to t=12, after 11.

### neg_holderOnlyFence (17 states), the LEASE-17 case
```text
h1 Claim; h1 (flaky) Detach -> GiveUp (detfail) but the detach write lands: holder=none, trans=0
h1 StartRelease; h2 Attach -> holder=h2, trans=1; h2 Detach -> holder=none, trans=1
h1 TakeRead: holder empty, no uid/transitions check -> take -> deletecollection
-> deletes the batch h2 parked (ReleaseFencing)
```

### neg_unconditionalRenew (13 states), the LEASE-16 case
h1 (flaky) sends a renewal and gives up on it; detach lands (holder=none); h2 attaches (holder=h2, trans=1); the late renewal lands as an unconditional write and sets holder=h1 while h2 believes it holds the Lease (BelieverIsHolder).
