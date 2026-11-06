# BatchLease.tla: TLC results (22/N..25/N shipped baseline)

All runs use TLC2 2.19 (08 Aug 2024, rev 5a47802) from `tla2tools.jar`, OpenJDK 27, 24 workers, 21 GB heap, breadth-first search, `-deadlock` (the bounded clock stops at `MaxTime`, so terminal states are expected).

## Commands

```bash
cd scratch/tla
./configs.sh      # writes one NAME.cfg per run below (mkcfg.sh validates keys and applies overrides)
./runall.sh       # runs all 31 configs, writes summary.txt and logs/NAME.log
# single run:
java -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC -workers auto -deadlock -config main_core.cfg BatchLease.tla
python3 trace.py logs/neg_retryPastExpiry.log   # condensed counterexample (only changed variables)
```

Defaults (`mkcfg.sh`): `D=6` (1 tick is roughly 10 s, so `D=6` models a 60 s Lease), `I = D div 3 = 2`, `Grace=6`, `G=1` (`get_batch` margin), `M=2` (reaper margin), `SkewMax=2`, `Lat=1` (a request lands within 1 tick, its reply arrives within 1 more), `MaxTime=10`, `MaxInc=2`, `MaxRounds=2`, `Bugs={}`, `Fixes={}`, `SYMMETRY` over `{h1, h2}`. Every run checks all seven invariants: `ReaperSafety`, `ReaperSafetyConnected`, `AtMostOneBeliever`, `BelieverIsHolder`, `BelieverKeepsClaims`, `ReleaseFencing`, and `ClaimSetupFencing`.

## Python-to-TLA+ correspondence (`Bugs = {}, Fixes = {}`)

| Python SDK (`sandbox_batch.py` / `async_sandbox_batch.py` / `k8s_helper.py` / `batch_reaper.py`) | TLA+ action / predicate | Key fenced property |
| --- | --- | --- |
| `_claim` (atomic mode when `"claimSetup"` is off) | `Claim(p)` | Records `renewed_at` before stamping `renewTime`, checks `claims = {}` by `batch-id`, creates claims owned by `nextUid`. |
| `_claim` 2-step setup (`"claimSetup" \in Features`): `create_batch_lease` -> `list_sandbox_claim_objects(..., deadline=lease_expires_at)` -> `time.monotonic() >= lease_expires_at` -> rollback `delete_batch_lease(..., resource_version=created_rv)` | `ClaimCreateLease(p)`, `ClaimFinish(p)`, `ClaimRollback(p)`, `Deliver("ldel")` | Local `now < expAt[p]` check guarantees the Lease still exists on the apiserver with `uid = myUid[p]` and `holder = p` (`ClaimSetupFencing`), and `resource_version=created_rv` prevents a slow rollback from deleting a replacement Lease (`BelieverIsHolder`). |
| `_attach` (`get_batch`) | `Attach(p)`, `NotStaleForAttach` | Adopts only when `holderIdentity` is empty and `now <= renewTime + dur - CLOCK_SKEW_MARGIN`, replacing at read `resourceVersion` with `leaseTransitions + 1`. |
| `_renew_loop` / `_renew_once` / `_write_renewal` | `Renew(p)`, `Recv(p)` | Sends PUT at last written `resourceVersion` with timeout `min(I, expAt - now)`; sets `expAt = sentAt + D` while `now < expAt`; stops on 404 or foreign holder on 409. |
| `_expire_lease_if_due` across caller checks, create/cleanup workers, and consumer/acquire waits | `Believes(p)` (`alive[p] /\ pc[p] = "hold" /\ ~err[p] /\ now < expAt[p]`) | Self-fences handle locally as soon as `time.monotonic() >= _lease_expires_at`. |
| `_delete_claim` (`release_member`, `_drop_acquired`, `_delete_released_create`) over `_batch_custom_objects_api` (`retries=False`) with `min(30s, expAt - now)` | `MemberDelete(p)`, `Deliver("mdel")` (`"memberOps" \in Features`) | Single unretried by-name `DELETE` sent only while `Believes(p)` and bounded by `expAt[p]` never deletes a replacement batch's claim while skew stays within `D + M` (`BelieverKeepsClaims`). |
| `detach` | `Detach(p)`, `Recv(p)` | Waits for active renewal and member deletes (`wKind = "none"`), writes `holder=None` with `Grace`, retries 409 once if still holder. |
| `release` / `_take_batch_lease` / `_delete_claims_in_rounds` / `release_group` | `StartRelease(p)`, `TakeRead(p)`, `Count(p)`, `Deliver("dcoll"/"ldel")` | Fences take on `(uid, leaseTransitions)` + holder, scopes `deletecollection` and live counts to `(batch-id, batch-lease-uid)` (`{myUid[p]}`), deletes Lease at taken `resourceVersion`. |
| `batch_reaper.find_stale_leases` / `reap` | `ReapList`, `ReapDelete` | Picks Leases at `now + rskew > renewTime + dur + M`, waits full `lease.dur` monotonic ticks from `listed_at`, and deletes with `preconditions.resource_version = listed_rv`. |
| Kubernetes garbage collector (`ownerReferences`) | `GC(u)` | Collects claims of UID `u` only after Lease incarnation `u` no longer exists. |

## Results (`summary.txt`)

| Run | Features, constants, switches | Result | States generated / distinct | Depth | Trace | Time |
| --- | --- | --- | --- | --- | --- | --- |
| `main_core` | `{crash,flaky,attach,detach}`, `SkewMax=5, MaxTime=10` | No error | 11,250,597 / 5,366,897 | 60 | - | 42 s |
| `main_release` | `{release,flaky,crash}`, `SkewMax=5, MaxTime=10` | No error | 52,180,660 / 24,233,858 | 72 | - | 179 s |
| `main_fence` | `{release,detach,attach,flaky}`, `MaxTime=7` | No error | 55,991,793 / 19,655,559 | 60 | - | 155 s |
| `main_claim_setup` | `{claimSetup,flaky,crash}`, `MaxTime=14` | No error | 1,221,646 / 663,265 | 71 | - | 6 s |
| `main_member_ops` | `{memberOps,flaky,crash}`, `MaxTime=14` | No error | 1,327,699 / 486,124 | 72 | - | 6 s |
| `skew6_connected` | `{}`, `SkewMax=6, MaxInc=1, MaxTime=14` | No error | 14,968 / 10,447 | 38 | - | 2 s |
| `skew12_connected` | `{}`, `SkewMax=12, MaxInc=1, MaxTime=14` | No error | 57,021 / 32,965 | 39 | - | 4 s |
| `skew8_flaky` | `{flaky}`, `SkewMax=8, MaxInc=1, MaxTime=14` | No error | 2,833,046 / 1,233,135 | 68 | - | 12 s |
| `skew9_flaky` | `{flaky}`, `SkewMax=9, MaxInc=1, MaxTime=14` | No error | 3,743,449 / 1,589,514 | 68 | - | 12 s |
| `skew10_flaky` | `{flaky}`, `SkewMax=10, MaxInc=1, MaxTime=14` | No error (saturates at `SkewMax=9`) | 3,743,449 / 1,589,514 | 68 | - | 11 s |
| `skew8_member_flaky` | `{memberOps,flaky}`, `SkewMax=8, MaxInc=2, MaxTime=10` | No error | 3,179,957 / 908,037 | 56 | - | 11 s |
| **`skew9_member_flaky`** | `{memberOps,flaky}`, `SkewMax=9, MaxInc=2, MaxTime=10` | **`BelieverKeepsClaims` violated** | 132,776 / 65,463 | 19 | 15 | 3 s |
| **`neg_unscopedReleaseDelete`** | `{release}`, `Bugs={unscopedReleaseDelete}, MaxTime=14` | **`BelieverKeepsClaims` violated** | 30,450 / 24,052 | 22 | 22 | 3 s |
| **`neg_unscopedReleaseDelete_skipNone`** | `{release,flaky,crash}`, `Bugs={unscopedReleaseDelete}, Fixes={skipOnNone}, SkewMax=5` | **`BelieverKeepsClaims` violated** | 338,537 / 183,522 | 21 | 18 | 4 s |
| **`neg_reaperWaitInterval_skew6`** | `{}`, `SkewMax=6, Bugs={reaperWaitInterval}, MaxInc=1` | **`ReaperSafety` violated** | 2,855 / 2,016 | 22 | 16 | 2 s |
| **`neg_reaperWaitInterval_flaky_skew6`** | `{flaky}`, `SkewMax=6, Bugs={reaperWaitInterval}, MaxInc=1` | **`ReaperSafety` violated** | 1,738 / 1,243 | 14 | 10 | 1 s |
| **`neg_unconditionalRollbackDelete`** | `{claimSetup,flaky}`, `Bugs={unconditionalRollbackDelete}, MaxTime=14` | **`BelieverIsHolder` violated** | 123,662 / 76,851 | 25 | 23 | 3 s |
| **`neg_claimNoExpiryCheck`** | `{claimSetup,flaky}`, `Bugs={claimNoExpiryCheck}, MaxTime=14` | **`ClaimSetupFencing` violated** | 2,662 / 1,589 | 13 | 10 | 2 s |
| **`neg_retryPastExpiry`** | `{memberOps,flaky}`, `Bugs={retryPastExpiry}, MaxTime=14` | **`BelieverKeepsClaims` violated** | 904,270 / 351,147 | 22 | 21 | 5 s |
| `neg_reaperSubtracts` | `{flaky}`, `Bugs={reaperSubtracts}, MaxInc=1, MaxTime=14` | No error (masked by full-`D` wait + `sentAt + D`) | 1,215,677 / 577,904 | 68 | - | 6 s |
| **`neg_reaperSubtracts_interval`** | `{flaky}`, `Bugs={reaperSubtracts,reaperWaitInterval}, MaxInc=1` | **`ReaperSafety` violated** | 1,645 / 1,188 | 14 | 11 | 2 s |
| **`neg_reaperNoPrecond`** | `{release}`, `Bugs={reaperNoPrecondition}, MaxTime=14` | **`ReaperSafety` violated** | 177,201 / 139,022 | 30 | 26 | 4 s |
| **`neg_reaperNoPrecond_hand`** | `{detach,attach,flaky}`, `Bugs={reaperNoPrecondition}, MaxInc=1, MaxTime=14` | **`BelieverIsHolder` violated** | 2,006,450 / 1,090,523 | 24 | 23 | 10 s |
| **`neg_reaperNoWait_skew`** | `{}`, `SkewMax=5, Bugs={reaperNoWait}, MaxInc=1` | **`ReaperSafety` violated** | 751 / 600 | 21 | 14 | 1 s |
| `neg_reaperNoWait` | `{flaky}`, `Bugs={reaperNoWait}, MaxInc=1` | No error (masked by margin `M=2`) | 33,391 / 17,781 | 49 | - | 2 s |
| `neg_expiryFromResp` | `{flaky}`, `Bugs={expiryFromResponse}, MaxInc=1, MaxTime=14` | No error (masked at `SkewMax=2` by full-`D` wait) | 152,518 / 93,072 | 66 | - | 3 s |
| **`neg_expiryFromResp_skew9`** | `{flaky}`, `SkewMax=9, Bugs={expiryFromResponse}, MaxInc=1, MaxTime=14` | **`ReaperSafety` violated** | 69,180 / 43,848 | 20 | 16 | 3 s |
| **`neg_expiryFromResp_noWait`** | `{flaky}`, `Bugs={expiryFromResponse,reaperNoWait}, MaxInc=1` | **`ReaperSafety` violated** | 11,379 / 6,726 | 20 | 17 | 2 s |
| **`neg_holderOnlyFence`** | `{release,detach,attach,flaky}`, `Bugs={holderOnlyFence}, MaxTime=7` | **`ReleaseFencing` violated** | 328,012 / 168,070 | 19 | 16 | 3 s |
| `neg_getBatchAdds` | `{detach,attach,flaky}`, `Bugs={getBatchAdds}, MaxInc=1` | No error (masked by empty-holder + `rv` precondition) | 4,436,261 / 2,106,673 | 60 | - | 18 s |
| **`neg_unconditionalRenew`** | `{detach,attach,flaky}`, `Bugs={unconditionalRenew}, MaxInc=1` | **`BelieverIsHolder` violated** | 25,445 / 14,416 | 15 | 13 | 2 s |

## Tightness boundary analysis (`D + M = 8` vs `D + M + 1 = 9`)

1. **Lease-only `ReaperSafety` (`skew8_flaky`, `skew9_flaky`, `skew10_flaky`):**
   Once `SkewMax >= D + M + 1 = 9`, `Reapable` (`now + SkewMax > renew + D + M`) holds immediately at `now = renew`, so `skew9_flaky` and `skew10_flaky` explore the exact same worst-case state space (`1,589,514` distinct states). Because the reaper cannot list a `resourceVersion` before the write that created it (`rAt >= sentAt`), and because the holder computes `expAt = sentAt + D` (`LEASE-5`) while the reaper waits `D` monotonic ticks (`rAt + D >= sentAt + D = expAt`), `ReaperSafety` holds at arbitrary wall-clock skew (`skew8_flaky`, `skew9_flaky`, and `skew10_flaky` all pass).
2. **Where `SkewMax = D + M + 1 = 9` breaks safety:**
   - **In-flight by-name member deletes (`skew8_member_flaky` vs `skew9_member_flaky`):** `_delete_claim` sends an unretried `DELETE /sandboxclaims/b1-0` while `now < expAt[p]` (`latest send = expAt[p] - 1`), which can spend `Lat = 1` tick in wire transit and land at `expAt[p]`. At `SkewMax = 8` (`D + M`), `ReapList` cannot fire until `sentAt + 1`, so `ReapDelete` fires at `expAt[p] + 1 > expAt[p]` and `skew8_member_flaky` passes (`908,037` distinct states, 0 errors). At `SkewMax = 9` (`D + M + 1`), `ReapList` fires at `sentAt`, `ReapDelete` fires at `expAt[p]`, `h2` claims `uid=2` at `expAt[p]`, and `h1`'s wire-transit `DELETE` lands at `expAt[p]`, violating `BelieverKeepsClaims` in 15 states (`skew_margin + leaseDurationSeconds - in_flight_latency`).
   - **Send-time vs response-time expiry (`neg_expiryFromResp` vs `neg_expiryFromResp_skew9`):** With `Bugs = {"expiryFromResponse"}`, a renewal sent and landed at `t=2` is listed by the reaper at `t=2` when `SkewMax = 9` (`2 + 9 > 2 + 6 + 2`), the reply arrives at `t=3` setting `expAt = 9`, and `ReapDelete` at `t=8` violates `ReaperSafety` in 16 states.

## Key new `22/N` counterexamples

- **`neg_unconditionalRollbackDelete` (23 states):** `h1` creates `Lease(uid=1, rv=1)` at `t=0` and stalls during setup; the reaper lists at `t=7` and deletes `uid=1` at `t=13`; `h1` wakes and sends an unconditional rollback `DELETE /leases/batch-b1` (`prec=0`); `h2` creates `Lease(uid=2, rv=2)` and finishes setup; `h1`'s rollback delete lands and removes `h2`'s live Lease (`BelieverIsHolder` violated). Passing `resource_version=created_rv` (`main_claim_setup`) closes this completely.
- **`neg_claimNoExpiryCheck` (10 states):** `h1` creates `Lease(uid=1, expAt=6)` at `t=0`, stalls until `t=6`, and finishes `claim_batch()` with an already-expired Lease (`ClaimSetupFencing` violated). Bounding `list_sandbox_claim_objects` with `deadline=lease_expires_at` and checking `time.monotonic() >= lease_expires_at` closes this completely.
- **`neg_retryPastExpiry` (21 states):** `h1` starts a by-name `_delete_claim` while `Believes(h1)` holds, then goes flaky while hidden `urllib3` retries keep the request alive past `expAt[h1]`; after the reaper deletes `uid=1`, GC removes `uid=1`'s claims, and `h2` claims `uid=2`, `h1`'s retried `DELETE` lands and deletes `h2`'s claim (`BelieverKeepsClaims` violated). Routing batch custom-object calls through `_batch_custom_objects_api` (`retries=False`) and capping `_request_timeout` by `expAt - now` (`main_member_ops`) closes this completely.
