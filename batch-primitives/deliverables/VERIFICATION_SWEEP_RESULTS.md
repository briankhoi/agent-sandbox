# Automated Batch Verification & Chaos Pipeline Sweep Results

- **Target Branch / Ref:** `origin/feat/rl-batch-claim-mode` (stacked on `24/N` `#2194`, `23/N` `#2189`, and `22/N` `#2188`)
- **Verified Commit SHA:** `e84e964ce2d87987ec2443a5b5edccc84a374289`
- **Verified Git Tree SHA:** `f798166c129e24f4f0ae59b842a0a32ce40fe177`
- **Verification Strategy Reference:** `batch-primitives/comprehensive_verification_strategy.md` (`e72f7347`)
- **Drop-In Verification Suite Patch:** `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/deliverables/verification_suite/verification_suite.patch` (`12,419` lines across `12` repository files; verified with `git apply --check --whitespace=error` both full and `--exclude='examples/agent-sandbox-rl/*'` against tree `f798166c129e24f4f0ae59b842a0a32ce40fe177`)

---

## 1. One-Minute Executive Summary

We built eight permanent, deterministic verification pipelines (a 22-run modular TLA+ formal suite, a 21-finding static AST and call-graph auditor, a pure SDK 12-cell fault-injection suite paired with a dedicated RL batch fault-injection suite, a stateful Hypothesis fuzzer backed by a shared structural invariant oracle, a lockstep sync/async differential fuzzer, a multi-threaded and async concurrency/CAS stress suite, a performance and complexity suite of bytecode-count, request-count, memory, and wakeup gates for the SDK and RL integration (Pipeline 8), and a 12-stage single-command master runner) and executed an exhaustive verification sweep across `origin/feat/rl-batch-claim-mode` at commit `e84e964ce2d87987ec2443a5b5edccc84a374289` (tree `f798166c129e24f4f0ae59b842a0a32ce40fe177`).

Before sweeping for new defects, we ran our pipelines against both known PR `#2188` review bugs as unfixed negative controls:
1. **Janet's PR `#2188` review bug** (a fatal watch error stops Lease renewal while sandboxes run) failed deterministically in `BatchHandle.tla` (`ActiveWorkloadProtected` across `74` distinct states and `CleanMemberReleaseAllowed` across `11` distinct states), in `verify_batch_static.py` (`[01]`, `[02]` `INV-AUTH-1`), and live in `test_batch_fault_injection.py` (sync and async).
2. **Aditya's PR `#2188` Darwin bug** (connecting to a bound, non-listening TCP socket raises `ConnectTimeoutError` on BSD/macOS rather than `NewConnectionError`) failed live in `test_k8s_helper.py` via BSD `SYN`-drop socket simulation.

All `1,100` pre-existing batch SDK (`399`) and RL (`701`) unit tests passed on `e84e964ce2d87987ec2443a5b5edccc84a374289` without detecting any of the defects below. Across the full verification sweep and iterative multi-agent adversarial audit rounds, the correctness pipelines uncovered **29 distinct verified findings** (`4 CRITICAL`, `9 HIGH`, `14 MEDIUM`, `2 LOW`) backed by **48 executable failing test reproductions**, **21 static AST/call-graph detections**, and **14 deterministic TLA+ negative-control counterexamples** (`8` baseline bug reproductions + `6` protocol mutation controls), alongside **7 audited intended contracts** (`Section 3.13`, where we recommend zero production changes):

Pipeline 8 then added **10 performance findings** (`F-30`..`F-39`: `1 HIGH`, `3 MEDIUM`, `6 LOW`; [Section 3.14](#perf-findings)), each red in a deterministic gate on `e84e964c`, green under its candidate fix, and re-confirmed by a standalone wall-clock and request-count repro on real SDK and RL objects. The largest: `F-30` (every RL step's liveness check sorts the whole batch under its lock, `41x` slower at `2,000` members), `F-33` (`SandboxFleet.release` is `O(H)` under the fleet lock), `F-39` (session command output costs quadratic time), and `F-34` (`25/N` only, fixed on `26/N`).

| ID | Severity | Invariant / Rule | Verified Location (`file:line`) | Owning Commit & Stack Target | Active-Voice Summary |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **F-01** | `CRITICAL` | `INV-AUTH-1`, `INV-AUTH-2`, `INV-AUTH-3` | `sandbox_batch.py:491,506,1640,1654,1738,1745` & `async_sandbox_batch.py:508,524,1625,1636,1720` | `540465e8` (`22/N` `#2188`) + `3a1607bc` | `_watch_loop` writes fatal watch errors (`403`/`404`/cert) to shared `_state.note_error(e)`, halting `_renew_loop`, discarding renewed Lease RV, suppressing `lease_degraded()`, and blocking `release_member()` / `release_group()`. |
| **F-02** | `MEDIUM` | Platform Socket Portability | `test_k8s_helper.py:1196-1268` (`:1243`) | `cd965322` (`22/N` `#2188`) | `test_k8s_helper.py:1243` connects to a bound, non-listening socket and asserts `NewConnectionError`, whereas BSD/macOS (`xnu`) drops `SYN` packets and raises sibling `ConnectTimeoutError`. |
| **F-03** | `CRITICAL` | `INV-OWN-4`, `INV-LEAK-1` | `batch_state.py:310-314,357`, `sandbox_batch.py:1166-1179,1605-1610`, `async_sandbox_batch.py:1200-1211,1594-1599` | `113fae28` / `5e379f8a` (standalone `feature/batch-claim` PR) | Late watch `ADDED` (or `410` relist) after `release_not_ready()` or `release_group()` calls `release_claim(claim_name, member.warmpool)` with default `needs_delete=False`, tombstoning the live claim in `_released` with `0` K8s `DELETE` calls. |
| **F-04** | `CRITICAL` | `INV-OWN-3`, `INV-AUTH-2` | `sandbox_batch.py:1212-1218,2209-2259`, `async_sandbox_batch.py:1245-1251,2208-2258`, `k8s_helper.py:731-761`, `async_k8s_helper.py:709-737` | `113fae28` / `522dad4c` + `540465e8` (`22/N` `#2188`) | `release_group()` invokes `_delete_claims_in_rounds` with unclamped `90.0s` `_request_timeout` and paginated `count_live_sandbox_claims` (which forwards `deadline=None` to `_claim_list_pages`), mutating and listing claims past `_lease_expires_at`. |
| **F-05** | `HIGH` | `INV-LEAK-2` | `async_fleet.py:179-182` | `a09950e1` (`25/N` or standalone `main` PR) | Cancelling `await AsyncSandboxFleet.acquire(...)` abandons `_to_thread` (`self._thread_pool().submit`) while the worker thread finishes acquiring, permanently leaking `SandboxHandle`, `BatchClaim`, and `_claims_reserved`. |
| **F-06** | `HIGH` | `INV-LEAK-2` | `fleet.py:401-403,1473,1567,1737,1852,1869` | `dab949fb` (`25/N`) + `e87bc38c` / `c70b415b` | Five rollback blocks in `SandboxFleet` (`acquire`, `_acquire_reserved`, `release`, `resume`, `restore`) catch `except Exception:` instead of `BaseException`, skipping slot, handle, and `BatchClaim` rollback on `KeyboardInterrupt` / `SystemExit`. |
| **F-07** | `HIGH` | `INV-LEAK-1` | `batch_claims.py:135-155,184-192` | `06d29e24` (`25/N` `feat/rl-batch-claim-mode`) | `BatchClaims._batch_for` omits a raced `SandboxBatch` from `_opened` and leaks raw `ApiException(503)` when `_closed` wins a race, and `release_all()` clears `_opened = []` upfront before `batch.release()`. |
| **F-08** | `HIGH` | `INV-LEAK-1` | `async_sandbox_client.py:241-245,263,273-298` | `e272eee8` + `522dad4c` (standalone `feature/batch-claim` PR) | `AsyncSandboxClient.__aexit__` calls `close() -> _stop_batches() -> _unregister_batch(batch)` unconditionally even when `batch._release_started() and not batch._lease_released`, defeating `delete_all()` cleanup retry. |
| **F-09** | `HIGH` | `INV-WATCH-HTTP-1` | `async_k8s_helper.py:739-770` | `3a1607bc` (standalone `feature/batch-claim` PR) | `AsyncK8sHelper.watch_sandbox_claims` (`_preload_content=False`) skips HTTP status validation, swallowing non-JSON `401`/`403`/`502`/`503` responses as `0`-event watches and dropping `Retry-After` headers on JSON `429` responses. |
| **F-10** | `HIGH` | `INV-CONN-1`, `INV-LEAK-1` | `handles.py:475-519,606-620` & `batch_claims.py:78-88` | `dab949fb` / `06d29e24` (`25/N`) + `a09950e1` | In `claim_mode="batch"`, `SandboxHandle.refresh()` leaves watch-evicted `handle.sandbox` closed (`is_active=False`), `SandboxHandle.release()` skips `delete_manual_triggers()`, and stopped-batch fallback `BatchClaim.release()` leaves `sandbox` open. |
| **F-11** | `HIGH` | `INV-LEAK-1` | `fleet.py:1462-1524,1875-1878,1983-2015` | `2b68984b` / `e87bc38c` (`25/N` or standalone `main` PR) | `SandboxFleet._teardown()` sets `_torndown=True` before `release_all()`, so handle `0` raising skips handles `1..N-1` and pool/template sweeps permanently, while post-teardown `acquire()` creates templates/warmpools before failing. |
| **F-12** | `LOW` | `INV-ACCT-1` | `batch_state.py:708-714` | `8ad5a407` (standalone `feature/batch-claim` PR) | `BatchState._time_out_undecided_groups()` clears `_waiting[pool]` without calling `self._resolved.update(self._waiting[pool])`, breaking `_resolved` partition accounting in `GROUP` mode. |
| **F-13** | `MEDIUM` | `INV-TYPES-2` (PEP 561) | `k8s_agent_sandbox/__init__.py:15-71` & `batch_claims.py:37,45,155` | `8ad5a407` / `3a1607bc` + `06d29e24` (`25/N`) | `k8s_agent_sandbox` ships `py.typed` without `__all__`, causing `mypy --strict` (`no_implicit_reexport`) to reject public batch imports, and `batch_claims.py:155` returns untyped `Any` under `--strict`. |
| **F-14** | `MEDIUM` | `INV-OWN-TIMEOUT-3` | `batch_claims.py:87` & `fleet.py:1587` (`resources.py:112,819,978`) | `06d29e24` (`25/N`) + `c70b415b` | `BatchClaim.release()` fallback and `_acquire_reserved` call `cluster.resources.delete_claim(...)` on `Resources` with `request_timeout=None` on retried `custom_api`, allowing stalled apiservers to pin cleanup indefinitely. |
| **F-15** | `MEDIUM` | `INV-LOCK-ASYNC-1`, `INV-LEAK-2` | `async_sandbox_client.py:255-264,273-298` | `e272eee8` + `522dad4c` (standalone `feature/batch-claim` PR) | `AsyncSandboxClient.close()` holds `self._lock` across `await sandbox.close_connection()` (`0.250s` starvation) and closes `k8s_helper` without serializing on `batch._lifecycle_lock`, crashing in-flight `await batch.release()`. |
| **F-16** | `MEDIUM` | `INV-LEAK-2` | `sandbox_batch.py:1352-1363,1586-1590`, `k8s_helper.py:763-792`, `async_sandbox_batch.py:478-499,1577-1601` | `522dad4c` / `3a1607bc` (standalone `feature/batch-claim` PR) | Sync `_stop_background_threads()` joins `_creators` and `_renew_thread` without calling `watch.Watch.stop()` or joining `_watch_thread` (leaving `_watch_thread` blocked up to `65s`), and async `_watch_loop` omits `contextlib.aclosing`. |
| **F-17** | `MEDIUM` | `INV-CONN-1`, `INV-OWN-1` | `sandbox_batch.py:580-596`, `async_sandbox_batch.py:590-607`, `sandbox.py:275-301`, `async_sandbox.py:285-310` | `255d15fc` / `3a1607bc` (standalone `feature/batch-claim` PR) | `batch.connect(member)` returns a closed cached `Sandbox` (`is_active=False`) after `sbx.close_connection()`, and `sbx.terminate()` calls `delete_sandbox_claim` directly (`sandbox.py:291` / `async_sandbox.py:300`), bypassing batch Lease fencing after `detach()`. |
| **F-18** | `MEDIUM` | `INV-OWN-3` (Reaper Resilience) | `batch_reaper.py:60-69,89-128,154-190` | `a97eeae7` + `d9df3a42` (`22/N` `#2188` or standalone PR) | `batch_reaper.find_stale_leases` crashes `batch_reaper.reap` on `410 Gone` pagination `continue` token expiry and raises `TypeError` across the entire namespace when a single Lease carries a naive `spec.renew_time`. |
| **F-19** | `MEDIUM` | Claim TTL Monotonicity | `utils.py:109,115`, `batch_claims.py:59-60,176-177`, `testing.py:443-447` | `87754fa1` / `06d29e24` (`25/N`) + `c121a21c` / `841cefbb` | `construct_sandbox_claim_lifecycle_spec` truncates `shutdownTime` sub-seconds via `.strftime("%Y-%m-%dT%H:%M:%SZ")` so `BatchClaim.expires_at` exceeds actual K8s `shutdownTime` by up to `0.999999s`, and `FakeFleet(claim_mode="batch")` leaves `claim_expires_at=None`. |
| **F-20** | `MEDIUM` | `INV-LEAK-1` (RL Reaper Labels) | `fleet.py:224-228,1926-1976`, `resources.py:122`, `reaper.py:143-285,373-378` | `06d29e24` (`25/N`) + `dd38b094` / `c70b415b` | Custom `FleetConfig(labels=...)` omits `constants.DEFAULT_LABELS` (`{"app": "agent-sandbox-rl"}`) on `BatchClaims` (`fleet.py:224-228`), and `_run_leftovers` / `verify_teardown` / `reap` ignore run-labeled `coordination.k8s.io` batch `Lease` objects. |
| **F-21** | `MEDIUM` | RL Fleet API Contracts | `fleet.py:787-815,1447-1450,1542-1547,1696-1697`, `async_fleet.py:184-185`, `preflight.py:118-290` | `dab949fb` / `06d29e24` (`25/N`) + `a1a58a01` | `claim.batch.connect` readiness race raises raw `SandboxNotReadyError` instead of `BatchError`, `preflight()` skips `coordination.k8s.io` `leases` verification in `claim_mode="batch"`, and `acquire_batch` leaks handles `0..k-1` when item `k` fails. |
| **F-22** | `MEDIUM` | `INV-TYPES-1` | `examples/agent-sandbox-rl/agent_sandbox_rl/*.py` (`10` files, `41` errors) | Pre-existing on `main` (`bcf5d4a0` -> standalone `main` PR) | `mypy --ignore-missing-imports` reports `41` static type errors across `10` modules (`checked 29 source files`) in `agent_sandbox_rl`. |
| **F-23** | `LOW` | `INV-PARITY-LOCK-1`, `INV-LEAK-2` | `sandbox_client.py:593,603` & `k8s_helper.py:206-210` | `74cd8fa5` / `cd965322` (`22/N` `#2188` + standalone PR) | `SandboxClient` reads `batch._connected`/`_stale_connections` and calls `batch._detach_unfinished()` without `batch._lock`, and `K8sHelper(api_client=None)` opens `3` `ApiClient` thread/socket pools without exposing `close()`. |
| **F-24** | `CRITICAL` | `INV-LEAK-1`, `INV-OWN-3` | `sandbox_batch.py:864,887-918,1110-1117,1305-1363`, `async_sandbox_batch.py:873,896-927,1138-1152,1331-1369` | `dc9ea439` / `522dad4c` (standalone `feature/batch-claim` PR) | Dynamic `acquire()` dispatches `create_sandbox_claim` outside `self._creators` and `_active_deletes`, so `release()` finishes `deletecollection` and sets `_detached=True` while `acquire()` is mid-`POST`, suppressing `_drop_acquired -> _delete_claim` and leaking an orphaned `SandboxClaim`. |
| **F-25** | `HIGH` | `INV-ACCT-2`, `INV-ACCT-4` | `sandbox_batch.py:1220-1238,1787-1801` & `async_sandbox_batch.py:1253-1270,1769-1783` | `113fae28` / `5e379f8a` (standalone `feature/batch-claim` PR) | Up to `max_in_flight=20` `_create_worker` threads/tasks pop `(claim_name, group)` from `_create_plan` *before* sleeping on staggered `create_rps` slots; `release_group()` / `release_not_ready()` misses those popped items in `_drop_planned_creates`, stalling `events()`, `group_outcomes()`, and `wait_for_quorum()`. |
| **F-26** | `MEDIUM` | `INV-OWN-3`, `INV-LEAK-2` | `sandbox_batch.py:869,1926-1927,2030,2039` & `async_sandbox_batch.py:878,1919-1924,2030,2039` | `dc9ea439` / `5e379f8a` (standalone `feature/batch-claim` PR) | Dynamic `acquire()` pacing and `_create_one` `429`/transient retry sleeps wait only on `self._stop_creating` instead of the acquire's `waiter` (stranding `acquire()` across multi-second `Retry-After` sleeps after `release_group`), and `_send_create` waits on `_in_flight` without clamping to `_lease_expires_at`. |
| **F-27** | `MEDIUM` | `INV-OWN-1`, `INV-AUTH-3`, `INV-LEAK-2` | `sandbox_batch.py:414,442-466` (`sandbox_client.py:509-515,520-558`) & `async_sandbox_batch.py:411,439-463` (`async_sandbox_client.py:672-678,685-724`) | `3a1607bc` / `6feedc5c` (`22/N` `#2188` or standalone PR) | `get_batch()` (`_attach`) sends `replace_batch_lease(..., _request_timeout=30)` unclamped by `lease_duration`, returning an already-expired handle when the `PUT` takes longer than `lease_duration`, and omits `batch_utils.check_pool_size` (accepting `pool_size < 3` and leaving `max_in_flight=20`). |
| **F-28** | `MEDIUM` | `INV-LEAK-1` | `recycle.py:336-355,389-394,498-519,558-575` | `2b68984b` (`25/N` or standalone `main` PR) | When a recyclable image rotates (`pos > 0`) under `scale_on_hold=True` and `reset.prime(handle)` fails before `held[img] += 1` / `unwarm_image`, async `reuse_git_restore_sandbox_async` decrements `held[img]` to `-1` (scaling the pool to `1` dangling warm replica) and sync `reuse_git_restore_sandbox` leaks its JIT `replicas_override=1` warm replica. |
| **F-29** | `HIGH` | `INV-OWN-3`, `INV-LEAK-2` | `sandbox_batch.py:441-450,1518-1544,2137-2147` & `async_sandbox_batch.py:438-447,1520-1544,2137-2147` | `522dad4c` / `3a1607bc` (standalone `feature/batch-claim` PR) | When `batch_reaper` or a racing client deletes a Lease between `read_batch_lease` (`GET`) and `replace_batch_lease` (`PUT`), `_take_batch_lease` (`release`), `_attach` (`get_batch`), and `detach` only catch `409`, letting raw `ApiException(status=404)` abort `release()` before `deletecollection`, escape `except BatchError:` in `get_batch()`, and leave `_lease_released=False` in `detach()`. |
| **F-30** | `HIGH` | `INV-PERF-HOTPATH-1` | `batch_claims.py:72` (`25/N`), `:92` and `failfast.py:404-411,447` (`26/N`) | `25/N` and `26/N` | `BatchClaim.still_reaches()` (every `is_alive()` step check) and `26/N`'s `BatchWatchdog._poll_once` copy and sort every member under the batch lock to read one: `33x` to `39x` more work at `2,000` members. |
| **F-31** | `LOW` | `INV-PERF-HOTPATH-3` | `batch_state.py:426-448,574-582` | SDK (both) | `has_pool`, `members(pool)`, and `release_group(pool)` scan every member and group per pool release (`39x` to `43x`). |
| **F-32** | `LOW` | `INV-PERF-HOTPATH-2` | `sandbox_batch.py:1393-1400`, `async_sandbox_batch.py:1400-1407` | SDK (both) | `_close_connections` scans the `_stale_connections` list per connection, `O(S^2)` behind a backlog of failed closes. |
| **F-33** | `MEDIUM` | `INV-PERF-HOTPATH-2` | `fleet.py:1717,1719` (`25/N`), `:1857,1859` (`26/N`) | `25/N` and `26/N` | `SandboxFleet.release()` runs two dataclass-equality scans of every live handle under the fleet lock (`19.5x` at `1,000` handles). |
| **F-34** | `MEDIUM` | Minimum requests | `fleet.py:1503-1510` | `25/N` only (fixed by `3f95b264`) | Repeat on-demand acquires re-send `ensure_template` and `create_warmpool`: `2` extra requests per acquire. |
| **F-35** | `LOW` | Bounded memory | `batch_state.py:256` | SDK (both) | `BatchState._released` keeps one tombstone per released claim forever (`156 B` per episode). |
| **F-36** | `LOW` | `INV-PERF-HOTPATH-2` | `sandbox_batch.py:1220-1232`, `async_sandbox_batch.py:1253-1265` | SDK (both) | The handles' `release_group()` walks the whole fill plan to drop one pool's planned creates (`12x` at `1,000` planned pools). |
| **F-37** | `LOW` | No needless wakeups | `sandbox_batch.py:1210`, `async_sandbox_batch.py:1243` | SDK (both) | `release_group()` wakes every waiting acquire, including other pools' (`100` of `100`). |
| **F-38** | `LOW` | Minimum requests | `fleet.py:1562,1564` (`25/N`), `:1610,1612` (`26/N`) | `25/N` and `26/N` | A per-claim acquire `GET`s the Sandbox twice (`get_pod_name`, `get_pod_ip`) where one read returns both. |
| **F-39** | `MEDIUM` | `INV-PERF-HOTPATH-5` | `handles.py:186-222` (`:217`) | `25/N` and `26/N` | `SandboxSession.run` re-joins and re-scans its whole output buffer per chunk: `10x` the output costs `107x` the time. |

### Master 12-Stage Sweep Summary (`dev/tools/run_batch_verification.sh`)

| Stage | Pipeline | Wall Time | Result on `e84e964c` | Measured Output |
| :--- | :--- | :--- | :--- | :--- |
| `0` | Harness Self-Check (`Import & Mypy`) | `39s` (`~6s` warm) | `PASS` | Verifies `k8s_agent_sandbox` resolves inside `REPO_ROOT`; `0` `mypy` errors across `6` SDK harness files, `1` RL test module, and `2` BFS model scripts. |
| `1` | Static AST & Call-Graph Auditor (`verify_batch_static.py`) | `1s` | `FAIL` (expected on unfixed code) | `Summary: 21 finding(s) (CRITICAL=4, HIGH=9, MEDIUM=5, LOW=3)`, `0` false positives. |
| `1b` | Static Hot-Path Complexity (Check 8 alone) | `1s` | `FAIL` (expected on unfixed code) | `16` findings on `25/N` (`F-30`..`F-33`, `F-36`, `F-39`), `18` on `26/N`, `0` with the candidate fixes. |
| `2` | Modular TLA+ Suite (`runall_subsystems.sh`, `22` TLC runs) | `56s` | `PASS` (`22/22` expected outcomes) | `8` positive specs pass (`3,272,848` distinct / `17,866,937` generated states); `14` deterministic `-workers 1` negative controls violate target invariants (`2,499` distinct / `6,296` generated states; `3,275,347` total distinct states, `0` subsystem deadlocks). |
| `3` | BFS Lock & Cancel State Models (`verify_delete_lifecycle_model.py`, `verify_cancel5_exercise.py`) | `4s` | `PASS` | `62,579` total reachable states across `15` sync/async lock/signal models (`7` positive safety models = `26,053` states with `0` violations + `8` expected negative-control models = `36,526` states) + `19` explored states across `3` `CANCEL-5` assertions (`4 + 9 + 6`). |
| `4` | BSD/macOS Socket Portability (`test_k8s_helper.py`) | `2s` | `FAIL` (reproduces **F-02**) | `1 failed, 80 deselected in 1.35s` (`ConnectTimeoutError` at `test_k8s_helper.py:1243`). |
| `5a` | Pure SDK Fault-Injection Suite (`test_batch_fault_injection.py`) | `7s` | `FAIL` (reproduces **F-01, F-03, F-04, F-08, F-09, F-15, F-16, F-17, F-18, F-24, F-25, F-26, F-27, F-29**) | `23 failed, 13 passed in 6.57s` (`36` tests total; `0` `agent_sandbox_rl` imports). |
| `5b` | RL Batch Fault-Injection Suite (`test_batch_fault_injection_rl.py`) | `3s` | `FAIL` (reproduces **F-05, F-06, F-07, F-10, F-11, F-14, F-19, F-20, F-21, F-28**) | `20 failed in 0.92s` (`20` independent RL batch failure reproductions). |
| `6` | Stateful Hypothesis Fuzzer (`test_batch_state_fuzz.py`) | `15s` | `FAIL` (shrinks **F-12** & **F-03**) | `2 failed, 2 passed in 15.01s` (`10,000`-step `TestHealthyCoreBatchStateFuzz` + `6,000`-step `TestReattachedBatchStateFuzz` pass; shrinks `F-12` and `F-03` on cold cache). |
| `7` | Sync/Async Lockstep Differential Fuzzer (`test_batch_differential.py`) | `3s` | `FAIL` (reproduces **F-12**) | `1 failed, 51 passed in 2.55s` (`50` seeded lockstep traces + `CANCEL-5` interruption parity pass; `F-12` fails identically on sync and async). |
| `8` | Concurrency & CAS Stress Suite (`test_batch_concurrency_stress.py`) | `4s` | `FAIL` (reproduces **F-15**) | `1 failed, 4 passed in 3.63s` (`50`-worker sync/async churn, `15`-round `9`-thread Lease CAS, and creator flood pass; `0.250s` lock starvation fails). |
| `9` | Production Mypy & PEP 561 (`SDK + RL`) | `32s` (`~8s` warm) | `FAIL` (reproduces **F-13** & **F-22**) | SDK internal: `0` errors (`44` files); PEP 561 `--strict -c`: `Found 3 errors in 1 file`; RL `--ignore-missing-imports`: `Found 41 errors in 10 files (checked 29 source files)`. |
| `10a` | SDK Performance & Complexity Suite (`test_batch_performance_invariants.py`) | `~3 min` (`-n 8`) | `FAIL` (reproduces **F-31, F-32, F-36, F-37**) | `9` red gates on `25/N`; `66 passed, 1 xfailed` (`F-35` target) with the candidate fixes. |
| `10b` | RL Performance & Complexity Suite (`test_batch_performance_rl.py`) | `~15s` | `FAIL` (reproduces **F-30, F-33, F-34, F-39**) | `12` red gates on `25/N`, `8` on `26/N` (including the watchdog); `20 passed, 2 skipped, 1 xfailed` (`F-38` target) with the candidate fixes. |

---

## 2. Step 1 Negative-Control Proof Logs

A verification harness that starts green proves nothing about its defect-detection power. Before running the broader sweep, we proved that our formal models, static analyzers, and runtime tests fail deterministically on both known PR `#2188` review bugs.

### Negative Control 1A: Janet's Bug in TLA+ (`BatchHandle.tla`)

- **Command:**
  ```bash
  cd /usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/work/tla
  ./run.sh bh_neg_janet BatchHandle
  ./run.sh bh_neg_janet_release BatchHandle
  ./run.sh bh_fixed BatchHandle
  ```
- **Measured Deterministic TLC Output (`logs/bh_neg_janet.log`, `-workers 1`, `Bugs = {"sharedWatchLeaseError"}`):**
  TLC explores `74` distinct states (`148` generated states) in `<1s` and prints a minimal 6-state counterexample violating `ActiveWorkloadProtected`:
  ```text
  Error: Invariant ActiveWorkloadProtected is violated.
  State 1: <Initial predicate>
    activeSandboxes = 1, leaseExpiresAt = 6, renewState = "ACTIVE", watchState = "RUNNING", stateError = "NONE", myRv = 1, serverRv = 1
  State 2: <WatchFatalError>
    watchState = "STOPPED", watchErr = "WATCH_FAILED", stateError = "WATCH_FAILED", createState = "STOPPED"
  State 3..4: <Tick> -> now = 2 (nextRenewAt = 2)
  State 5: <RenewStart>
    renewState = "IN_FLIGHT", serverRv = 2, sentAt = 2 (Lease PATCH succeeds on apiserver!)
  State 6: <RenewFinishSuccess>
    renewState = "STOPPED", myRv = 1, leaseExpiresAt = 6, renewDegraded = FALSE
  ```
  State 5 -> State 6 exposes an additional subtlety beyond simple loop exit: even when an in-flight Lease `PATCH` commits on the apiserver (`serverRv = 2`), `_renew_once()` sees `self._state.error() is not None` (`sandbox_batch.py:1738`), discards `written` (`myRv` stays `1`), leaves `renewDegraded = FALSE`, and terminates `_renew_loop` (`renewState = "STOPPED"`) while `activeSandboxes = 1` continues running.
- **Measured Deterministic TLC Output (`logs/bh_neg_janet_release.log`, `-workers 1`, `Bugs = {"sharedWatchLeaseError"}`):**
  TLC violates `CleanMemberReleaseAllowed` in a 3-state counterexample (`Init -> WatchFatalError -> ReleaseActiveSandbox` sets `blockedRelease = TRUE` across `11` distinct states / `15` generated states).
- **Measured Fixed Control (`logs/bh_fixed.log`, `Bugs = {}`):**
  Separating `watchErr` from `leaseErr` passes all `5` invariants (`ActiveWorkloadProtected`, `AuthoritySeparation`, `CleanMemberReleaseAllowed`, `CachedLeaseRvConsistent`, `ExpiredImpliesDegraded`) across all `3,280` distinct states (`9,266` generated states, `0` deadlocks).

### Negative Control 1B: Janet's Bug Live in Python (`test_batch_fault_injection.py`)

- **Command:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "watch_fatal_error_does_not_halt_lease_renewal"
  ```
- **Measured Failure Output (Sync + Async):**
  - `TestCell01WatchVsLeaseAuthoritySeparation::test_watch_fatal_error_does_not_halt_lease_renewal`:
    Injects a `403 Forbidden` watch `ApiException` while a caller holds a connected sandbox and `now < _lease_expires_at`. `_watch_loop` calls `self._state.note_error(e)` (`sandbox_batch.py:1640`). Calling `batch._renew_once()` sends `replace_namespaced_lease` to the mock apiserver, hits `if self._detached or self._state.error() is not None: return False` (`sandbox_batch.py:1738`), and fails `AssertionError: False is not true : INV-AUTH-1 violated: _renew_once() returned False after watch 403 even though Lease is valid and connected sandbox is active`.
  - `TestCell01AsyncWatchAndHttpStatusHandling::test_async_watch_fatal_error_does_not_halt_lease_renewal`:
    Fails identically at `async_sandbox_batch.py:1625` and `async_sandbox_batch.py:1720` (`AssertionError: False is not true : INV-AUTH-1 violated: AsyncSandboxBatch._renew_once() returned False after watch 403`).

### Negative Control 2: Aditya's Darwin Bug Live in Python (`test_k8s_helper.py`)

- **Command:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_k8s_helper.py -k "bsd_non_listening_socket_simulation"
  ```
- **Measured Failure Output:**
  On Linux, connecting to a TCP socket after `bind()` without `listen()` immediately receives `RST` (`ECONNREFUSED`), which `urllib3` wraps as `NewConnectionError`. On BSD/macOS (`xnu`), connecting to a bound, non-listening socket silently drops `SYN` packets until the `0.05s` connect timeout expires, raising `socket.timeout`, which `urllib3` wraps as `ConnectTimeoutError` (a sibling subclass of `SocketTimeoutError` and `HTTPConnectionError`, **not** a subclass of `NewConnectionError`). Simulating BSD `SYN` timeout on the non-listening loopback port causes `test_k8s_helper.py:1243` to fail live:
  ```text
  urllib3.exceptions.ConnectTimeoutError: (<urllib3.connection.HTTPConnection object>, 'Connection to 127.0.0.1 timed out. (connect timeout=0.05)')
  ```

---

## 3. Complete Findings Catalog (`F-01` .. `F-39` at `e84e964ce2d87987ec2443a5b5edccc84a374289`)

Every finding below cites exact `file:line` coordinates and `git blame` SHAs verified against `e84e964ce2d87987ec2443a5b5edccc84a374289`, paired with a deterministic single-command reproduction and surgical fix recommendation.

---

### `F-01` [`CRITICAL`]: Fatal Watch Errors Corrupt Shared Lease Authority State, Halting Lease Renewal While Workloads Run

- **Violated Invariants:** `INV-AUTH-1` (Watch-Failure Isolation), `INV-AUTH-2` (Monotonic Fence), `INV-AUTH-3` (`lease_degraded()` Accuracy)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py:491, 506, 573, 1640, 1654, 1738-1748`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_batch.py:508, 524, 591, 1625, 1636, 1720-1731`
- **Owning Commits & PR Routing:** `540465e8` in open PR `22/N` (`#2188`, `origin/feat/batch-10-deletecollection-retry`) coupled `_renew_once()` (`:1738`) and `_expire_lease_if_due()` (`:491`) to `self._state.error()`, interacting with `3a1607bc` / `113fae28` where `_watch_loop` wrote fatal watch errors to `self._state.note_error(e)` (`:1640, :1654`). Fold directly into `22/N` (`#2188`).
- **Discovery Pipelines:** Pipeline 1 (`BatchHandle.tla` `bh_neg_janet` & `bh_neg_janet_release`), Pipeline 2 (`verify_batch_static.py` `[01]`, `[02]`), Stage 5a (`test_batch_fault_injection.py` Cell 01 sync + async).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "watch_fatal_error_does_not_halt_lease_renewal"
  ```
- **Root-Cause Trace:**
  1. `_watch_loop` catches a non-`410` `ApiException` (such as RBAC `403 Forbidden`) or non-transport exception and calls `self._state.note_error(e)` (`sandbox_batch.py:1640, 1654`; `async_sandbox_batch.py:1625, 1636`).
  2. `_renew_once()` sends `replace_namespaced_lease` to the apiserver (`sandbox_batch.py:1710`). Even after the apiserver commits the new Lease `renewTime` and increments `resourceVersion`, `_renew_once()` acquires `self._lock`, evaluates `if self._detached or self._state.error() is not None: return False` (`sandbox_batch.py:1738-1739`), discards `written`, and exits `_renew_loop`.
  3. Because `_renew_loop` exits without calling `_mark_renewal_degraded(...)` and `_expire_lease_if_due()` checks `self._state.error() is None` (`sandbox_batch.py:491`), `batch.lease_degraded()` remains `False` forever even after `self._lease_expires_at` passes (`INV-AUTH-3`), at which point Kubernetes GC (`ownerReferences`) or `batch_reaper.reap` deletes the underlying `SandboxClaim` objects underneath active connected sandboxes (`INV-AUTH-1`).
  4. Simultaneously, `_check_healthy()` (`sandbox_batch.py:506`) raises `BatchError` on `release_member()`, `release_not_ready()`, and `release_group()`, blocking clean member deletion while the handle still holds the Lease. Conversely, when Lease renewal genuinely fails in `_stop_renewal` (`sandbox_batch.py:1745`), `_stop_renewal` never sets `self._watch_stop`, leaving `_watch_loop` streaming unnecessarily.
- **Surgical Fix Recommendation:**
  - **Recommended:** Keep `BatchState._error` scoped to watch/fill state and add `self._lease_error: Exception | None = None` on `SandboxBatch` and `AsyncSandboxBatch` (`err()` returns `self._lease_error or self._state.error()`):
    - `_renew_once()` and `_expire_lease_if_due()` check only `self._detached` and `self._lease_error`, keeping Lease renewal and expiry fencing active regardless of `self._state.error()`.
    - `_stop_renewal` sets `self._lease_error = error` and signals `self._watch_stop.set()` (and cancels `_watch_task` in async) so Lease loss stops watch promptly.
    - Watch-dependent consumers (`wait_for_quorum`, `group_outcomes`, `events`, and `acquire` waiting on readiness) continue waking and raising on `self._lease_error or self._state.error()`.
    - Lease-fenced cleanup calls (`release_member`, `release_not_ready`, `release_group`, `release`, `detach`) check only `self._detached` and `self._lease_error` (plus `_expire_lease_if_due`); update `test_release_methods_delete_nothing_once_err_is_set` (`test_sandbox_batch.py:3660`), release docstrings, and run `make fix-api-docs`.
  - **Rejected Alternative:** Treating `403` on watch as retryable without separating `self._lease_error` from `self._state.error()`. Any other non-transport watch failure (`401`, `404`, malformed event, cert rotation error) would still kill `_renew_loop`.

---

### `F-02` [`MEDIUM`]: BSD/macOS Non-Listening Socket Connects Raise `ConnectTimeoutError`, Breaking `test_k8s_helper.py:1243`

- **Violated Rule:** Cross-platform test determinism (Linux vs BSD/macOS `xnu` TCP stack)
- **Verified Location:** `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_k8s_helper.py:1196-1268` (`sock.bind(("127.0.0.1", 0))` at `:1197-1198`, assertion at `:1243`)
- **Owning Commit & PR Routing:** `cd965322` in open PR `22/N` (`#2188`, `origin/feat/batch-10-deletecollection-retry`). Fold into commit `cd965322` in `22/N`.
- **Discovery Pipeline:** Stage 4 (`test_k8s_helper.py::TestLeaseApiClient::test_batch_custom_object_requests_bsd_non_listening_socket_simulation`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_k8s_helper.py -k "bsd_non_listening_socket_simulation"
  ```
- **Root-Cause Trace:**
  `test_batch_custom_object_requests_do_not_retry_and_keep_injected_settings` binds a `SOCK_STREAM` socket to `127.0.0.1:0` without calling `listen()` (`:1197-1198`) and asserts `with self.assertRaises(urllib3.exceptions.NewConnectionError)` (`:1243`). On macOS (`xnu`), the kernel silently drops `SYN` packets to a bound, non-listening socket rather than replying with `RST`, so the `0.05s` connect timeout fires and `urllib3` raises `urllib3.exceptions.ConnectTimeoutError` (which inherits from `SocketTimeoutError` and `HTTPConnectionError`, not `NewConnectionError`).
- **Surgical Fix Recommendation:**
  - **Recommended:** Change `test_k8s_helper.py:1243` to `with self.assertRaises((urllib3.exceptions.NewConnectionError, urllib3.exceptions.ConnectTimeoutError)) as conn_err:` while retaining `self.assertNotIsInstance(conn_err.exception, urllib3.exceptions.MaxRetryError)` on `:1245`.
  - **Rejected Alternative:** Closing the socket before `batch_call()` to force `ECONNREFUSED`; another CI process can bind the freed ephemeral port during the window.

---

### `F-03` [`CRITICAL`]: Late Watch `ADDED` After `release_not_ready()` or `release_group()` Tombstones Live Claims Without Deleting Them in Kubernetes

- **Violated Invariants:** `INV-OWN-4` (Zero Post-Release Stragglers), `INV-LEAK-1` (Zero Leaked Cluster Claims)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/batch_state.py:310-314, 357, 362-375, 415, 431`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py:1166-1179, 1605-1610, 1878-1903`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_batch.py:1200-1211, 1594-1599, 1844-1871`
- **Owning Commits & PR Routing:** `113fae28` (`15/N` `#1990`) and `5e379f8a` (`19/N` `#2166`), merged on `upstream/feature/batch-claim`. Open a standalone fix PR off `upstream/feature/batch-claim`.
- **Discovery Pipelines:** Pipeline 1 (`BatchWatch.tla` `bw_neg_late_added`, `20` distinct states), Stage 5a (`test_batch_fault_injection.py::TestCell02Watch410GoneAndLateAddedLeak::test_late_watch_added_after_release_not_ready_deletes_claim_in_k8s`), Stage 6 (`test_batch_state_fuzz.py::test_late_watch_added_after_release_not_ready_queues_pending_delete`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "test_late_watch_added_after_release_not_ready_deletes_claim_in_k8s"
  ```
- **Root-Cause Trace:**
  1. When a fill creator finishes creating `b1-0` and `b1-1` (or a prior holder before `get_batch()` re-attachment sent `b1-1`, or a timed-out `POST` landed after `release_group` finished `deletecollection`) and the caller invokes `release_not_ready()` (populating `self._fill_stopped_pools` at `batch_state.py:415`) or `release_group(pool)` (`self._fill_stopped_pools.add(warmpool)` at `batch_state.py:431`) before the watch delivers `ADDED(b1-1)`, `b1-1` is not yet in `self._members`.
  2. When `_watch_loop` subsequently receives `ADDED(b1-1)` (or `resync_from_list` runs at `batch_state.py:357` after a `410` watch reset), `BatchState.upsert_claim` (`batch_state.py:310-314`) executes:
     ```python
     if previous is None and self.fill_creates_released(member.warmpool):
         self.release_claim(claim_name, member.warmpool)
         return None
     ```
  3. Because `release_claim` (`batch_state.py:362`) defaults `needs_delete: bool = False`, calling `self.release_claim(claim_name, member.warmpool)` inserts `'b1-1'` into `self._released` **without** calling `self._need_delete(claim_name)` (`self._pending_deletes` stays `{}`), and `_watch_loop` (`sandbox_batch.py:1605-1610`) never calls `self._delete_claim(claim_name)`. Because `'b1-1'` now sits in `_released`, every subsequent `MODIFIED` event, `410` relist, `release_not_ready()`, and `release_member()` ignores `'b1-1'`, stranding the live `SandboxClaim` on the cluster until batch teardown or `shutdownTime`.
- **Surgical Fix Recommendation:**
  - **Recommended:** Pass `needs_delete=True` at `batch_state.py:313` (`self.release_claim(claim_name, member.warmpool, needs_delete=True)` so `self._pending_deletes` records the late-arriving claim until a successful `_delete_claim` calls `self._state.note_deleted(claim_name, seq)` at `sandbox_batch.py:1271`), and have `_watch_loop` (on both `ADDED`/`MODIFIED` and `410` `resync_from_list` paths in `sandbox_batch.py` and `async_sandbox_batch.py`) snapshot any newly queued `_pending_deletes` and dispatch `self._delete_claim` outside `self._lock` while Lease health holds (`delete_sandbox_claim` treats `404` as idempotent success if a creator also deleted it).
  - **Rejected Alternative:** Leaving `needs_delete=False` and relying exclusively on `_delete_released_create` on creator threads; normal watch lag after `_create_worker` already returned from `create_sandbox_claim` (before `release_not_ready()` ran!) has no active creator left to call `_delete_released_create`, and re-attached `get_batch()` handles have no creator threads at all.

---

### `F-04` [`CRITICAL`]: `release_group()` Issues `90s` `deletecollection` and Unbounded `count_live_sandbox_claims` Unclamped by `_lease_expires_at`

- **Violated Invariants:** `INV-OWN-3` (Lease-Window Bounded Mutations), `INV-AUTH-2` (Monotonic Fence)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py:1212-1218, 2209-2259`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_batch.py:1245-1251, 2208-2258`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/k8s_helper.py:731-761` & `async_k8s_helper.py:709-737`
- **Owning Commits & PR Routing:** `113fae28` / `522dad4c` + `540465e8` in open PR `22/N` (`#2188`, `origin/feat/batch-10-deletecollection-retry`). Fold directly into `540465e8` in `22/N` (`#2188`).
- **Discovery Pipelines:** Pipeline 2 (`verify_batch_static.py` `[07]`, `[08]`), Stage 5a (`test_batch_fault_injection.py::TestCell12SdkTeardownFencingAndReaper::test_release_group_clamps_delete_timeout_to_remaining_lease_window`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "test_release_group_clamps_delete_timeout_to_remaining_lease_window"
  ```
- **Root-Cause Trace:**
  `release_group()` checks `_check_healthy()` once on entry (`sandbox_batch.py:1205`), then calls `_delete_claims_in_rounds` (`:1212-1218`). Inside `_delete_claims_in_rounds` (`sandbox_batch.py:2231-2259`, `async_sandbox_batch.py:2230-2258`), every round issues `helper.delete_sandbox_claims_by_label(..., _request_timeout=90.0)` (`BATCH_COLLECTION_REQUEST_TIMEOUT_SECONDS`) and `helper.count_live_sandbox_claims(...)` (which in `k8s_helper.py:731-761` and `async_k8s_helper.py:709-737` lacks a `deadline` parameter altogether and forwards `deadline=None` to `_claim_list_pages`). If `_renew_loop` degrades (`503` / network partition) with `2.5s` left on the Lease, `release_group()` sends `deletecollection` retries and paginated list calls up to `90s`+ past `_lease_expires_at`, violating the monotonic Lease fence (`INV-OWN-3`).
- **Surgical Fix Recommendation:**
  - **Recommended:** Add `deadline: float | None = None` to `K8sHelper.count_live_sandbox_claims` and `AsyncK8sHelper.count_live_sandbox_claims` (forwarding `deadline` to `_claim_list_pages`), and pass an optional `fence_deadline_fn: Callable[[], float] | None` into `_delete_claims_in_rounds` (returning `self._lease_expires_at` under `self._lock` for `release_group`, and `None` where Lease-write preconditions per round already govern); before each `delete_sandbox_claims_by_label` and `count_live_sandbox_claims` call, compute `remaining = fence_deadline_fn() - time.monotonic()`, raise `BatchLeaseExpiredError` if `remaining <= 0`, and clamp `_request_timeout = min(collection_timeout, remaining)` and `deadline = fence_deadline_fn()`.
  - **Rejected Alternative:** Shrinking `BATCH_COLLECTION_REQUEST_TIMEOUT_SECONDS` globally; large batches with healthy Leases legitimately need multi-second `deletecollection` budgets, whereas clamping to `remaining` enforces exact Lease fencing.

---

### `F-05` [`HIGH`]: Cancelling `AsyncSandboxFleet.acquire` Orphans Worker-Thread `SandboxHandle`, `BatchClaim`, and `_claims_reserved` Slot

- **Violated Invariant:** `INV-LEAK-2` (Zero Resource / Capacity Leaks Under Cancellation)
- **Verified Location:** `examples/agent-sandbox-rl/agent_sandbox_rl/async_fleet.py:179-182`
- **Owning Commit & PR Routing:** `a09950e1` (merged on `main`, exercised with `BatchClaim` in `25/N` `origin/feat/rl-batch-claim-mode`). Fold into `25/N` (or land on `upstream/main` and restack `25/N`).
- **Discovery Pipelines:** Pipeline 2 (`verify_batch_static.py` `[17]`), Stage 5b (`test_batch_fault_injection_rl.py::test_rl_async_fleet_cancelled_acquire_reclaims_real_handle_and_slot`).
- **1-Command Reproduction:**
  ```bash
  pytest -o asyncio_mode=auto examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py -k "test_rl_async_fleet_cancelled_acquire_reclaims_real_handle_and_slot"
  ```
- **Root-Cause Trace:**
  `AsyncSandboxFleet.acquire` (`async_fleet.py:179-182`) delegates synchronous `self._fleet.acquire(...)` to its worker pool (`self._thread_pool()`) via `await self._to_thread(...)` (`loop.run_in_executor`). Cancelling `await fleet.acquire(task)` cancels only the `asyncio.Future` wrapper while the underlying worker thread runs `self._fleet.acquire` to completion, appends `SandboxHandle` to `self._fleet._handles`, holds a live `BatchClaim`, and keeps `self._fleet._claims_reserved == 1` with no caller reference to release it.
- **Surgical Fix Recommendation:**
  - **Recommended (Critical `concurrent.futures.Future` Detail):** Submit the worker call directly via `cf_fut = self._thread_pool().submit(self._fleet.acquire, task, snapshot_uid=snapshot_uid, pod_annotations=pod_annotations)` and await `asyncio.wrap_future(cf_fut)` inside `try ... except BaseException:`. On cancellation, set an atomic `abandoned = True` flag under a `threading.Lock` and attach `cf_fut.add_done_callback(_reclaim)` **on the `concurrent.futures.Future` (`cf_fut`), NOT on the `asyncio.Future`!** (Attaching `add_done_callback` to `loop.run_in_executor`'s `asyncio.Future` fires immediately with the cancelled `asyncio.Future` and never receives the worker thread's eventual `SandboxHandle`.) When `cf_fut` finishes without exception (`not cf_fut.cancelled() and cf_fut.exception() is None`), `_reclaim` calls `self._fleet.release(cf_fut.result())` on a background thread (falling back to a short-lived daemon `threading.Thread` if `self._pool` already shut down).
  - **Rejected Alternative:** Attaching `add_done_callback` to the `asyncio.Future` returned by `loop.run_in_executor`; `asyncio.Future.cancel()` marks the wrapper future cancelled immediately, so `fut.result()` raises `CancelledError` inside the callback while the worker thread leaks the handle seconds later.

---

### `F-06` [`HIGH`]: Five `SandboxFleet` Rollback Sites Catch `except Exception:` Instead of `BaseException`, Leaking Slots, Handles, and `BatchClaim`s on `SIGINT` / `SIGTERM`

- **Violated Invariant:** `INV-LEAK-2` (`BaseException` Rollback Atomicity)
- **Verified Locations:** `examples/agent-sandbox-rl/agent_sandbox_rl/fleet.py:401-403, 1473, 1567-1633, 1719-1737, 1852, 1869`
- **Owning Commits & PR Routing:** `dab949fb` in `25/N` (`origin/feat/rl-batch-claim-mode`) added `if claim is not None: claim.release()` inside `_acquire_reserved`'s `except Exception:` block (`:1569`), inheriting `except Exception:` across `acquire` (`:1473`), `_acquire_reserved` (`:1567`), `release` (`:1737`), `resume` (`:1852`), and `restore` (`:1869`) from `e87bc38c` / `c70b415b`. Fix in `25/N`.
- **Discovery Pipelines:** Pipeline 2 (`verify_batch_static.py` `[12]`..`[16]`), Stage 5b (`test_batch_fault_injection_rl.py::test_rl_sync_fleet_keyboard_interrupt_rolls_back_reserved_slot` and `test_rl_sync_fleet_release_keyboard_interrupt_restores_handle_to_fleet`).
- **1-Command Reproduction:**
  ```bash
  pytest examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py -k "test_rl_sync_fleet_"
  ```
- **Root-Cause Trace:**
  `SandboxFleet._on_signal` (`fleet.py:401-403`) raises `KeyboardInterrupt` on `SIGINT` and `SystemExit(128 + signum)` on `SIGTERM`, both subclasses of `BaseException` outside `Exception`. All five transactional rollback handlers in `SandboxFleet` catch `except Exception:`:
  1. `acquire` (`:1473`) and `_acquire_reserved` (`:1567`): a signal arriving after `batch_claims.acquire` (e.g., during `claim.batch.connect`) skips `claim.release()`, `delete_claim`, `sandbox.terminate()`, `cluster.release_replicas(1)`, and `_claims_reserved -= 1`.
  2. `release` (`:1719, :1737`): removes `handle` from `self._handles` upfront and only re-appends `handle` under `except Exception:`, permanently dropping `handle` from `fleet.handles()` while `_claims_reserved` and the remote `SandboxClaim` remain live.
  3. `resume` (`:1852`) and `restore` (`:1869`): reserve a live slot before resuming/restoring and only call `self._free_slot_for_suspend()` under `except Exception:`.
- **Surgical Fix Recommendation:**
  - **Recommended:** Change `fleet.py:1473, 1567, 1737, 1852, 1869` from `except Exception:` to `except BaseException:` (and `except BaseException as exc:` at `:1567`), gating watchdog error wrapping at `fleet.py:1625` with `if batch_claims is None and verdict is not None and isinstance(exc, Exception):` so `KeyboardInterrupt` and `SystemExit` propagate unaltered after rollback completes.
  - **Rejected Alternative:** Fixing only `acquire` and `_acquire_reserved` while leaving `release`, `resume`, and `restore` on `except Exception:`; `_on_signal` can interrupt any synchronous K8s RPC across the handle lifecycle.

---

### `F-07` [`HIGH`]: `BatchClaims._batch_for` and `BatchClaims.release_all` Orphan Live Renewing `SandboxBatch` Handles on Teardown Race or Transient `503`

- **Violated Invariant:** `INV-LEAK-1` (Zero Orphaned Renewing Batches / Background Threads)
- **Verified Locations:** `examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py:135-155, 184-192`
- **Owning Commit & PR Routing:** `06d29e24` in open branch `25/N` (`origin/feat/rl-batch-claim-mode`). Fold directly into `06d29e24` in `25/N`.
- **Discovery Pipelines:** Pipeline 1 (`RLBatchLifecycle.tla` `rl_neg_teardown_race` & `rl_neg_clear_upfront`), Pipeline 2 (`verify_batch_static.py` `[18]`, `[19]`), Stage 5b (`test_batch_fault_injection_rl.py::test_rl_batch_claims_teardown_race_tracks_batch_and_raises_fleet_error` and `test_rl_batch_claims_release_all_retains_failed_batches_for_retry`).
- **1-Command Reproduction:**
  ```bash
  pytest examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py -k "test_rl_batch_claims_"
  ```
- **Root-Cause Trace:**
  1. `_batch_for` (`batch_claims.py:135-155`) calls `cluster.sandbox_client.claim_batch(...)` outside `self._lock`. If `release_all()` sets `self._closed = True` while `claim_batch()` runs, lines `147-149` skip `self._opened.append(batch)` and line `152` calls `batch.release()` unprotected. When `batch.release()` hits a transient `503`, raw `ApiException` escapes (instead of `FleetError`) and `batch` remains excluded from `self._opened` with its `_renew_loop` renewing the Lease forever (`rl_neg_teardown_race` violates `NoOrphanedRenewingBatch` in `16` distinct states).
  2. `release_all()` (`batch_claims.py:186`) runs `opened, self._opened = self._opened, []` upfront before calling `batch.release()`. When `batch.release()` fails transiently (`:191`) or `KeyboardInterrupt` interrupts the loop, `self._opened` stays empty, turning subsequent `release_all()` retries into no-ops (`rl_neg_clear_upfront` violates `NoOrphanedRenewingBatch` in `14` distinct states).
- **Surgical Fix Recommendation:**
  - **Recommended:** Unconditionally append `batch` to `self._opened` under `self._lock` immediately after `claim_batch()` (with `except BaseException:` rollback if interrupted before acquiring `self._lock`); in `release_all()`, snapshot `opened = list(self._opened)` under `self._lock` and remove each `batch` from `self._opened` under `self._lock` **only after** `batch.release()` succeeds; when `_batch_for` observes `closed`, delegate to `self.release_all()` and raise `FleetError`.
  - **Rejected Alternative:** Holding `self._lock` across `claim_batch(...)` network I/O, which serializes multi-cluster creation and blocks teardown during apiserver stalls.

---

### `F-08` [`HIGH`]: `AsyncSandboxClient.__aexit__` Unconditionally Unregisters Failed Batches in `_stop_batches()`, Defeating Cleanup Retry

- **Violated Invariant:** `INV-LEAK-1` (Retryable Batch Cleanup Parity With Sync `SandboxClient`)
- **Verified Locations:** `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_client.py:241-245, 263, 273-298` (contrast sync `sandbox_client.py:582-600`)
- **Owning Commits & PR Routing:** `e272eee8` + `522dad4c` (`12/N` `#1824`), merged on `upstream/feature/batch-claim`. Open a standalone fix PR off `upstream/feature/batch-claim`.
- **Discovery Pipelines:** Pipeline 2 (`verify_batch_static.py` `[20]`), Stage 5a (`test_batch_fault_injection.py::TestCell12AsyncClientTeardownAndCloseRace::test_async_client_aexit_retains_failed_batch_for_cleanup_retry`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "test_async_client_aexit_retains_failed_batch_for_cleanup_retry"
  ```
- **Root-Cause Trace:**
  In `AsyncSandboxClient.__aexit__` (`async_sandbox_client.py:241-245`), `try: await self._delete_automatic_sandboxes()` runs `await batch.release()`. When `batch.release()` fails transiently (for example `403` before token refresh or `503` on `deletecollection`), `_release_batches` deliberately keeps `batch` registered in `_active_batches` so the caller can retry `await client.delete_all()`. Immediately afterward, `__aexit__`'s `finally: await self.close()` (`:244`) calls `await self._stop_batches()` (`:263`), which stops renewal and unconditionally calls `self._unregister_batch(batch)` (`:297`), erasing `batch` from `_active_batches` so a subsequent `await client.delete_all()` finds `0` batches (`1 != 2` in our reproduction).
- **Surgical Fix Recommendation:**
  - **Recommended (Preserving `cleanup=False` Contract at `test_async_sandbox_batch.py:1544`):** Mirror sync `SandboxClient._track_closing_batch` (`sandbox_client.py:582-600`): in `_stop_batches()` (`async_sandbox_client.py:297`), check `if (batch._release_started() and not batch._lease_released) or batch._connected or batch._stale_connections:` to retain batches whose `release()` started and failed (or whose connections failed to close) so `delete_all()` can retry cleanup, while still unregistering untouched `cleanup=False` batches (`not batch._release_started()`) when `close()` stops their background tasks (preserving `test_async_sandbox_batch.py:1544`!).
  - **Rejected Alternative:** Checking `if not batch._lease_released:` alone without `batch._release_started()`; on `AsyncSandboxClient(cleanup=False)`, `close()` stops background tasks without deleting the Lease (`_lease_released` stays `False`), so omitting `batch._release_started()` would retain `cleanup=False` batches and break `test_async_sandbox_batch.py:1544`.

---

### `F-09` [`HIGH`]: `AsyncK8sHelper.watch_sandbox_claims` Skips HTTP Status Validation, Swallowing Non-JSON `401`/`403`/`502`/`503` Watch Errors and Dropping `429` `Retry-After` Headers

- **Violated Invariant:** `INV-WATCH-HTTP-1` (Explicit HTTP Error & `Retry-After` Propagation on Watch Streams)
- **Verified Location:** `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_k8s_helper.py:739-770` (contrast sync `k8s_helper.py:763-792`)
- **Owning Commit & PR Routing:** `3a1607bc` (`1/N` `#1742`), merged on `upstream/feature/batch-claim`. Open a standalone fix PR off `upstream/feature/batch-claim`.
- **Discovery Pipelines:** Pipeline 2 (`verify_batch_static.py` `[09]`), Stage 5a (`test_batch_fault_injection.py::TestCell01AsyncWatchAndHttpStatusHandling::test_async_watch_sandbox_claims_raises_api_exception_with_headers_on_http_error`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "test_async_watch_sandbox_claims_raises_api_exception_with_headers_on_http_error"
  ```
- **Root-Cause Trace:**
  In `kubernetes_asyncio`, passing `_preload_content=False` (used internally by `watch.Watch().stream(...)` at `async_k8s_helper.py:752`) bypasses `RESTClientObject.request` status checking and returns the raw `aiohttp.ClientResponse` even when `resp.status` is `401`, `403`, `429`, `502`, or `503`. `kubernetes_asyncio.watch.Watch.stream` splits lines and ignores non-JSON lines (`json.JSONDecodeError`), returning cleanly as an empty `0`-event stream when an authenticating proxy or overloaded apiserver returns plain-text/HTML `403 Forbidden` or `503 Service Unavailable`. Even when K8s returns a single-line JSON `Status` object on `429 TooManyRequests`, `Watch` synthesizes `ApiException(status=429)` without `http_resp.headers`, dropping `Retry-After` so `_watch_loop` ignores server-requested backoff.
- **Surgical Fix Recommendation:**
  - **Recommended:** Wrap the coroutine callable `func` passed into `w.stream(checked_list, ...)` (note: `kubernetes_asyncio.watch.Watch.close()` sets `self.resp = None` in its `finally:` block, so inspecting `w.resp` *after* `w.stream` finishes sees `None`!). Inside `checked_list(*args, **kwargs)`, await `resp = await self.custom_objects_api.list_namespaced_custom_object(*args, **kwargs)`; if `not (200 <= resp.status <= 299)`, read `body = await resp.read()`, close/release `resp`, construct `err = ApiException(status=resp.status, reason=resp.reason)`, attach `err.body = body` and `err.headers = dict(resp.headers)`, and raise `err` before `Watch.stream` consumes lines.
  - **Rejected Alternative:** Inspecting `w.resp` after `async for event in w.stream(...)` exits; `Watch.stream`'s `finally: await self.close()` clears `w.resp = None` before control returns to `watch_sandbox_claims`.

---

### `F-10` [`HIGH`]: Batch-Mode `SandboxHandle` Fails to Reconnect Watch-Evicted Sandboxes on `refresh()`, Leaks Snapshot Triggers on `release()`, and Leaves Fallback Connections Open

- **Violated Invariants:** `INV-CONN-1` (Active Handle Validity), `INV-LEAK-1` (Zero Leaked Triggers / Client Connections)
- **Verified Locations:**
  - `examples/agent-sandbox-rl/agent_sandbox_rl/handles.py:475-519, 606-620` (and `clients/python/agentic-sandbox-client/k8s_agent_sandbox/gke_extensions/snapshots/sandbox_with_snapshot_support.py:569-578`)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py:78-88`
- **Owning Commits & PR Routing:** `dab949fb` and `06d29e24` in open branch `25/N` (`origin/feat/rl-batch-claim-mode`), interacting with `a09950e1` (`handles.py` snapshot/refresh support). Fold directly into `25/N`.
- **Discovery Pipeline:** Stage 5b (`test_batch_fault_injection_rl.py::test_rl_batch_handle_refresh_reconnects_closed_sandbox_after_watch_eviction`, `test_rl_batch_handle_release_cleans_snapshot_triggers`, `test_rl_stopped_batch_fallback_release_closes_sandbox_connection`).
- **1-Command Reproduction:**
  ```bash
  pytest examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py -k "test_rl_batch_handle_ or test_rl_stopped_batch_fallback"
  ```
- **Root-Cause Trace:**
  Three lifecycle gaps hit `SandboxHandle` when `self._batch_claim is not None`:
  1. **`F-10a` (`handles.py:475-519`):** During `suspend()` -> `resume()` / `restore()`, the pod loses readiness and restarts with a new Pod IP. `SandboxBatch._watch_loop` sees readiness loss / IP change, evicts `self._connected[claim_name]`, and calls `old_sb.close_connection()` (`old_sb.is_active == False`). When the caller invokes `handle.refresh()`, `refresh()` calls `sb.get_pod_name()` / `sb.get_pod_ip()` on the stale `old_sb` without calling `self.sandbox = self._batch_claim.batch.connect(...)`, leaving `handle.sandbox` closed (`is_active=False`).
  2. **`F-10b` (`handles.py:606-620`):** In `per_claim` mode, `handle.release()` calls `self.sandbox.terminate()` (`:620`), which runs `SandboxWithSnapshotSupport.terminate()` (`gke_extensions/snapshots/sandbox_with_snapshot_support.py:574-575`) to delete `PodSnapshotManualTrigger` CRs (`self._snapshots.delete_manual_triggers()`). In `batch` mode, `handle.release()` returns early at `:618` after `self._batch_claim.release()`, leaking every `PodSnapshotManualTrigger` created during the episode.
  3. **`F-10c` (`batch_claims.py:78-88` & `handles.py:614-618`):** When `batch.release_member(member)` raises `BatchError` (for example after Lease expiry) and `BatchClaim.release()` falls back to `cluster.resources.delete_claim(member.claim_name)` (`:87`), neither `BatchClaim.release()` nor `SandboxHandle.release()` calls `self.sandbox.close_connection()`, leaking open port-forward/HTTP/connector resources on `handle.sandbox`.
- **Surgical Fix Recommendation:**
  - **Recommended:**
    1. In `SandboxHandle.refresh()` (`handles.py:490`), when `self._batch_claim is not None` and `(sb is None or not getattr(sb, "is_active", True))`, re-establish `self.sandbox = new_sb = self._batch_claim.batch.connect(self._batch_claim.member)` (retrying inside the existing `_REFRESH_ATTEMPTS` loop while the resumed claim converges to `READY` in the batch watch), and if `old_sb` carried an initialized `_snapshots` engine with `created_manual_triggers`, carry those trigger names onto `new_sb.snapshots.created_manual_triggers` so `release()` still deletes manual triggers created before `suspend()`.
    2. In `SandboxHandle.release()` (`handles.py:614-618`), clean up manual snapshot triggers without lazily instantiating a `SnapshotEngine` when snapshots were unused: check `snap_eng = getattr(self.sandbox, "_snapshots", None)` (falling back to `'snapshots' in vars(self.sandbox)` for test mocks) and call `snap_eng.delete_manual_triggers()`, and ensure `if self.sandbox is not None: self.sandbox.close_connection()` runs in `finally:`.
  - **Rejected Alternative:** Accessing the lazy `@property` `self.sandbox.snapshots` unconditionally on every batch `release()`, which allocates a `SnapshotEngine` on handles that never took snapshots, or calling `self.sandbox.terminate()`, which bypasses `SandboxBatch` Lease fencing.

---

### `F-11` [`HIGH`]: `SandboxFleet._teardown` Aborts Remaining Handles and Pool/Template Sweeps When Handle `0` Raises, and Post-Teardown `acquire()` Creates Orphaned Templates/Pools

- **Violated Invariant:** `INV-LEAK-1` (Complete Fleet Teardown Sweep & Post-Teardown Admission Fence)
- **Verified Locations:** `examples/agent-sandbox-rl/agent_sandbox_rl/fleet.py:1462-1524, 1875-1878, 1983-2015`
- **Owning Commits & PR Routing:** `2b68984b` and `e87bc38c` (merged on `main`, interacting with `BatchClaims` teardown expectations in `25/N` `test_batch_claims.py:213-241`). Fix in `25/N` (or standalone `main` PR restacked into `25/N`).
- **Discovery Pipeline:** Stage 5b (`test_batch_fault_injection_rl.py::test_rl_fleet_teardown_sweeps_remaining_handles_and_pools_when_handle_0_raises` and `test_rl_post_teardown_acquire_fails_fast_without_creating_remote_pools`).
- **1-Command Reproduction:**
  ```bash
  pytest examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py -k "test_rl_fleet_teardown_ or test_rl_post_teardown_"
  ```
- **Root-Cause Trace:**
  1. **`F-11a` (`fleet.py:1875-1878, 1983-2015`):** `_teardown()` flips `self._torndown = True` (`:1991`), enters `try: self.release_all()` (`:1994`), and runs `self._batch_claims.release_all()` in `finally:` (`:2000`). However, `release_all()` (`:1875-1877`) runs an unguarded `for h in list(self._handles): self.release(h)`, and lines `2001-2093` (unwarming pools, deleting run-scoped `SandboxWarmPool`s, `SandboxTemplate`s, and namespaces) sit **outside** `try/finally`. When handle `0`'s `release()` raises, `release_all()` skips handles `1..N-1`, lines `2001-2093` never execute, and a retry call to `fleet.teardown()` returns immediately at `:1985` (`if self._torndown: return`) without ever sweeping pools or templates.
  2. **`F-11b` (`fleet.py:1462-1524`):** `SandboxFleet.acquire()` acquires `self._lock` at `:1462` to reserve capacity without checking `if self._torndown:`. For unplanned images after `teardown()`, `acquire()` proceeds through `self._ensure_pool(cluster, task.image, 1)` (`:1503`), calling `cluster.resources.ensure_template(...)` and `create_warmpool(...)` on the cluster before failing much later at `:1657` or `BatchClaims._batch_for`.
- **Surgical Fix Recommendation:**
  - **Recommended (Preserving `test_batch_claims.py:238`):**
    1. In `_teardown()` / `release_all()`, catch and record `first_exc: BaseException | None = None` across all handles in `list(self._handles)` so every handle `0..N-1` attempts `self.release(h)`, execute `self._batch_claims.release_all()` AND the entire pool/template/namespace sweep (`lines 2001-2093`) before exiting `_sweep_lock`, and then `if first_exc is not None: raise first_exc` at the end of `_teardown()` (preserving `test_batch_claims.py:238`, which asserts `teardown()` propagates handle release errors after sweeping).
    2. Check `if self._torndown: raise FleetError("fleet is torn down; call setup() before acquiring")` inside `with self._lock:` at `fleet.py:1462` before reserving `_claims_reserved` or creating on-demand templates/warmpools.
  - **Rejected Alternative:** Swallowing handle exceptions in `_teardown()` without re-raising `first_exc`; that breaks existing contract test `test_batch_claims.py:238`.

---

### `F-12` [`LOW`]: `BatchState._time_out_undecided_groups` Clears `_waiting` Without Recording Members in `_resolved`

- **Violated Invariant:** `INV-ACCT-1` (Conservation of Claims / `_resolved` Partition Accounting)
- **Verified Location:** `clients/python/agentic-sandbox-client/k8s_agent_sandbox/batch_state.py:708-714`
- **Owning Commit & PR Routing:** `8ad5a407` (`13/N` `#1988`), merged on `upstream/feature/batch-claim`. Open a standalone fix PR off `upstream/feature/batch-claim`.
- **Discovery Pipelines:** Pipeline 1 (`BatchConsumer.tla` `bc_neg_waiting_leak`, `77` distinct states), Stage 6 (`test_batch_state_fuzz.py::TestFullBatchStateFuzz::test_full_batch_state_machine` automatic Hypothesis shrink), Stage 7 (`test_batch_differential.py::test_differential_group_timeout_resolves_dropped_ready_members`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_differential.py -k "test_differential_group_timeout_resolves_dropped_ready_members"
  ```
- **Root-Cause Trace:**
  When `expire_fill()` calls `_time_out_undecided_groups()` (`batch_state.py:708-714`), line `712` executes `self._waiting[pool].clear()` before emitting `GroupOutcome(warmpool=pool, error=BatchTimeoutError("Group quorum timed out"))`. Unlike `_decide_group_outcome` (`:683`, `self._resolved.update(waiting)`) and `fail_quorum` (`:752`, `self._resolved.update(waiting)`), `_time_out_undecided_groups()` omits `self._resolved.update(self._waiting[pool])`, leaving Ready fill members of timed-out groups absent from `_resolved`. Because `fill_settled()` (`:880`) checks `self._fill_expired or len(self._resolved) + self._never_created >= self.size` and `_fill_expired` is already `True`, this remains `LOW` severity (internal accounting drift).
- **Surgical Fix Recommendation:**
  - **Recommended:** Insert `self._resolved.update(self._waiting[pool])` before `self._waiting[pool].clear()` at `batch_state.py:712`, aligning all three `_waiting` drain paths.
  - **Rejected Alternative:** Relaxing the `_resolved` partition invariant in `batch_invariant_oracle.py`; keeping `_resolved` exact protects future `BatchState` metrics and settlement callers.

---

### `F-13` [`MEDIUM`]: `k8s_agent_sandbox/__init__.py` Ships `py.typed` Without `__all__`, Breaking PEP 561 `mypy --strict` Consumers (`batch_claims.py:37, 45, 155`)

- **Violated Invariant:** `INV-TYPES-2` (PEP 561 Public Package Export Contract)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/__init__.py:15-71` (and `pyproject.toml:43, 92`)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py:37, 45, 155`
- **Owning Commits & PR Routing:** `8ad5a407` / `3a1607bc` / `dc9ea439` in `k8s_agent_sandbox/__init__.py` (standalone SDK PR off `upstream/feature/batch-claim`), plus `batch_claims.py:155` in `06d29e24` (`25/N` `origin/feat/rl-batch-claim-mode`).
- **Discovery Pipelines:** Pipeline 2 (`verify_batch_static.py` `[21]`), Stage 9 (`mypy --strict -c ...`).
- **1-Command Reproduction:**
  ```bash
  MYPYPATH=clients/python/agentic-sandbox-client \
    mypy --strict -c "from k8s_agent_sandbox import SandboxBatch, AsyncSandboxBatch, BatchGroup"
  ```
- **Root-Cause Trace:**
  `clients/python/agentic-sandbox-client/pyproject.toml` ships `k8s_agent_sandbox/py.typed` (`:43`) and enables `strict = true` (`:92`, implying PEP 561 `no_implicit_reexport = true`), yet `k8s_agent_sandbox/__init__.py` imports `32` public symbols without defining `__all__`. Strict PEP 561 callers reject `from k8s_agent_sandbox import SandboxBatch, AsyncSandboxBatch, BatchGroup, ...` with `[attr-defined]`, and `batch_claims.py:155` triggers `[no-any-return]` because `cluster.sandbox_client` lacks a return annotation.
- **Surgical Fix Recommendation:**
  - **Recommended:** Define `__all__` listing all `32` public symbols in `k8s_agent_sandbox/__init__.py`, and annotate `batch: SandboxBatch = cluster.sandbox_client.claim_batch(...)` at `batch_claims.py:135` in `25/N`.
  - **Rejected Alternative:** Repeating `import X as X` across all `32` imports instead of declaring `__all__`.

---

### `F-14` [`MEDIUM`]: `BatchClaim.release()` Fallback and `SandboxFleet._acquire_reserved` Call `Resources.delete_claim` With Unbounded `request_timeout=None`

- **Violated Invariant:** `INV-OWN-TIMEOUT-3` (Bounded Timeout on Every Outbound Claim / Lease API Call)
- **Verified Locations:** `examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py:87` and `examples/agent-sandbox-rl/agent_sandbox_rl/fleet.py:1587` (`resources.py:112, 819-822, 974-982`)
- **Owning Commits & PR Routing:** `06d29e24` in `25/N` (`batch_claims.py:87`) and `c70b415b` (`fleet.py:1587`). Fold `batch_claims.py:87` (and `fleet.py:1587`) into `25/N`.
- **Discovery Pipelines:** Pipeline 2 (`verify_batch_static.py` `[10]`, `[11]`), Stage 5b (`test_batch_fault_injection_rl.py::test_rl_batch_claim_release_fallback_passes_bounded_request_timeout`).
- **1-Command Reproduction:**
  ```bash
  pytest examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py -k "test_rl_batch_claim_release_fallback_passes_bounded_request_timeout"
  ```
- **Root-Cause Trace:**
  Both `BatchClaim.release()` fallback (`batch_claims.py:87`) and `SandboxFleet._acquire_reserved` rollback (`fleet.py:1587`) call `cluster.resources.delete_claim(claim_name)` without passing `request_timeout`. `Resources.delete_claim` (`resources.py:112, 819, 978`) defaults `request_timeout` to `None` on retried `self.custom_api`, allowing a blackholed connection or hung apiserver to stall RL claim rotation (`recycle.py`) and rollback threads indefinitely.
- **Surgical Fix Recommendation:**
  - **Recommended:** Promote `_REQUEST_TIMEOUT = (3, 10)` from `failfast.py:57` to `constants.REQUEST_TIMEOUT` and pass `request_timeout=constants.REQUEST_TIMEOUT` at `batch_claims.py:87` and `fleet.py:1587` (or default `request_timeout` in `Resources.delete_claim` to `constants.REQUEST_TIMEOUT`).
  - **Rejected Alternative:** Mutating global `socket.setdefaulttimeout()`, which breaks long-lived watch streams in the same process.

---

### `F-15` [`MEDIUM`]: `AsyncSandboxClient.close` Holds `self._lock` Across `await sandbox.close_connection()` and Closes `k8s_helper` Concurrent With In-Flight `batch.release()`

- **Violated Invariants:** `INV-LOCK-ASYNC-1` (Zero `await` I/O Under `asyncio.Lock`), `INV-LEAK-2` (Lifecycle Serialization During Close)
- **Verified Locations:** `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_client.py:255-264, 273-298`
- **Owning Commits & PR Routing:** `e272eee8` + `522dad4c` (`12/N` `#1824`), merged on `upstream/feature/batch-claim`. Open a standalone fix PR off `upstream/feature/batch-claim`.
- **Discovery Pipelines:** Pipeline 2 (`verify_batch_static.py` `[03]`), Stage 5a (`test_batch_fault_injection.py::TestCell12AsyncClientTeardownAndCloseRace::test_async_client_close_serializes_with_in_flight_batch_release`), Stage 8 (`test_batch_concurrency_stress.py::test_async_client_close_holds_lock_across_io_starving_batch_ops`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "test_async_client_close_serializes_with_in_flight_batch_release"
  ```
- **Root-Cause Trace:**
  1. `AsyncSandboxClient.close()` (`async_sandbox_client.py:255-262`) holds `async with self._lock:` while awaiting `await sandbox.close_connection()` (`:258`), starving concurrent batch registration and teardown operations waiting on `self._lock` (`0.250s` blocked in Stage 8).
  2. Immediately after `await self._stop_batches()` (`:263`, which stops renewal tasks without acquiring `batch._lifecycle_lock`), line `264` runs `await self.k8s_helper.close()`, closing the underlying `aiohttp` session while an in-flight `await batch.release()` still holds `batch._lifecycle_lock` and awaits `delete_sandbox_claims_by_label` (`RuntimeError: Session is closed while batch.release() is still running!`).
- **Surgical Fix Recommendation:**
  - **Recommended:** Snapshot `list(self._active_connection_sandboxes.items())` under `self._lock` and await `sandbox.close_connection()` outside `self._lock` (matching `AsyncSandboxBatch._close_cached_connections`). In `_stop_batches()`, **after** `if await self._finish_detach(batch): continue` (`async_sandbox_client.py:280`, which acquires `batch._lifecycle_lock` internally inside `batch.detach()`), acquire `async with batch._lifecycle_lock:` (checking `if batch._released: continue` once acquired) before stopping renewal/watch tasks and before `close()` calls `await self.k8s_helper.close()`, so any in-flight `release()` / `detach()` finishes before the HTTP transport closes.
  - **Rejected Alternative:** Ignoring `ClientError` / `RuntimeError` inside `batch.release()`; swallowing transport closure leaves claims and Leases undeleted on the apiserver while reporting a spurious failure or silent partial teardown.

---

### `F-16` [`MEDIUM`]: Sync `SandboxBatch._stop_background_threads` Leaves `_watch_thread` Blocked up to `65s`, and Async `_watch_loop` Omits `contextlib.aclosing`

- **Violated Invariant:** `INV-LEAK-2` (Deterministic Background Thread & Async Generator Shutdown)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py:1352-1363, 1586-1590` and `k8s_helper.py:763-792`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_batch.py:478-499, 1577-1601`
- **Owning Commits & PR Routing:** `522dad4c` (`12/N` `#1824`) and `3a1607bc` (`1/N` `#1742`), merged on `upstream/feature/batch-claim`. Open a standalone fix PR off `upstream/feature/batch-claim`.
- **Discovery Pipeline:** Stage 5a (`test_batch_fault_injection.py::TestCell12SdkTeardownFencingAndReaper::test_sync_stop_background_threads_interrupts_and_joins_watch_thread`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "test_sync_stop_background_threads_interrupts_and_joins_watch_thread"
  ```
- **Root-Cause Trace:**
  1. Unlike `AsyncSandboxBatch._stop_background_tasks` (`async_sandbox_batch.py:478-499`), which cancels and awaits `self._watch_task`, sync `SandboxBatch._stop_background_threads` (`sandbox_batch.py:1352-1363`) joins `self._creators` and `self._renew_thread` (`:1357-1362`) while merely setting `self._watch_stop.set()` without calling `watch.Watch.stop()` on the active `K8sHelper.watch_sandbox_claims` stream (`k8s_helper.py:763-792`) or joining `self._watch_thread`. Consequently, `batch.release()` and `batch.detach()` return while `_watch_thread` stays alive and blocked on its HTTP socket read for up to `_WATCH_TIMEOUT_SECONDS + 5 = 65s`, accumulating daemon threads and HTTP sockets under RL batch churn.
  2. In `AsyncSandboxBatch._watch_loop` (`async_sandbox_batch.py:1577`), `async for event in self._client.k8s_helper.watch_sandbox_claims(...)` iterates an async generator without `async with contextlib.aclosing(...)`, deferring `await w.close()` (`async_k8s_helper.py:770`) to GC finalization whenever `break` exits early (`:1580, :1591`).
- **Surgical Fix Recommendation:**
  - **Recommended:** Pass an optional `on_watch: Callable[[watch.Watch], None] | None` hook (or `stop_event`) into `K8sHelper.watch_sandbox_claims` so `_stop_background_threads()` calls `w.stop()` (which closes the underlying `urllib3` response socket) and joins `self._watch_thread` with a bounded timeout when `threading.current_thread() is not self._watch_thread`; wrap `AsyncSandboxBatch._watch_loop`'s `watch_sandbox_claims` generator in `async with contextlib.aclosing(...)`.
  - **Rejected Alternative:** Lowering `_WATCH_TIMEOUT_SECONDS` to `1s`, which multiplies idle apiserver watch reconnect QPS 60x.

---

### `F-17` [`MEDIUM`]: `batch.connect(member)` Returns Closed Cached `Sandbox` Handles, and `sbx.terminate()` Bypasses Batch Lease Fencing

- **Violated Invariants:** `INV-CONN-1` (Cached Connection Liveness), `INV-OWN-1` (Lease-Fenced Claim Deletion)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py:580-596` & `async_sandbox_batch.py:590-607`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox.py:275-301` (`:291`) & `async_sandbox.py:285-310` (`:300`)
- **Owning Commits & PR Routing:** `255d15fc` (`6/N` `#1984`), `3a1607bc` (`1/N` `#1742`), and `3da0fc4a`. Open a standalone fix PR off `upstream/feature/batch-claim`.
- **Discovery Pipeline:** Stage 5a (`test_batch_fault_injection.py::TestCell12SdkTeardownFencingAndReaper::test_connect_recreates_closed_cached_sandbox` and `test_batch_connected_sandbox_terminate_respects_lease_fence`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "test_connect_recreates_closed_cached_sandbox or test_batch_connected_sandbox_terminate_respects_lease_fence"
  ```
- **Root-Cause Trace:**
  1. **`F-17a` (`sandbox_batch.py:580-596`, `async_sandbox_batch.py:590-607`):** When a caller closes a connected `Sandbox` directly (`sbx.close_connection()`, setting `sbx.is_active == False`) without releasing the member, `self._connected[member.claim_name]` retains the dead `Sandbox` instance, and subsequent `batch.connect(member)` calls return that dead `Sandbox` (`is_active == False`) instead of constructing a fresh `self._client.sandbox_class(...)` instance.
  2. **`F-17b` (`sandbox.py:275-301`, `async_sandbox.py:285-310`):** `SandboxBatch.connect` (`sandbox_batch.py:583`) and `AsyncSandboxBatch.connect` (`async_sandbox_batch.py:593`) construct `self._client.sandbox_class(claim_name=member.claim_name, ...)` inline. Calling `sbx.terminate()` (or exiting `with batch.connect(member) as sbx:`) calls `self.k8s_helper.delete_sandbox_claim(self.claim_name, self.namespace)` directly (`sandbox.py:291`, `async_sandbox.py:300`), bypassing `release_member` and batch Lease fencing even after `batch.detach()` hands Lease ownership to another process (`INV-OWN-1` violated).
- **Surgical Fix Recommendation:**
  - **Recommended:**
    1. In `SandboxBatch.connect` and `AsyncSandboxBatch.connect`, evict a cached entry when `not cached[0].is_active` and construct a fresh `sandbox_class` connection.
    2. Pass an optional `_claim_deleter` callback (or route `Sandbox.terminate()` / `AsyncSandbox.terminate()` on batch-owned handles through `batch.release_member(member)`) so `sbx.terminate()` enforces the batch Lease fence and updates batch `_released` accounting instead of issuing an unfenced `delete_sandbox_claim`.
  - **Rejected Alternative:** Documenting "never call `terminate()` or `with sbx:` on batch sandboxes" without enforcing it in code; `Sandbox` implements `__enter__`/`__exit__ -> terminate()`, making accidental unfenced deletion a one-line footgun.

---

### `F-18` [`MEDIUM`]: `batch_reaper.find_stale_leases` Crashes on `410 Gone` Pagination Token Expiry and Aborts Entire Namespaces on a Single Naive `renew_time`

- **Violated Invariant:** `INV-OWN-3` (Fault-Isolated Batch Reaper Progress)
- **Verified Locations:** `clients/python/agentic-sandbox-client/k8s_agent_sandbox/batch_reaper.py:60-69, 89-128, 154-190`
- **Owning Commits & PR Routing:** `a97eeae7` (`18/N` `#2154`) + `d9df3a42` (`22/N` `#2188`, `fix(python-sdk): wait a full Lease duration before reaping stale batch Leases`). Fold into `22/N` (`#2188`) or open a standalone PR off `upstream/feature/batch-claim`.
- **Discovery Pipeline:** Stage 5a (`test_batch_fault_injection.py::TestCell12SdkTeardownFencingAndReaper::test_batch_reaper_restarts_on_410_pagination_expiry` and `test_batch_reaper_isolates_malformed_naive_renew_time`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "test_batch_reaper_"
  ```
- **Root-Cause Trace:**
  1. **`F-18a` (`batch_reaper.py:89-128`):** `find_stale_leases` paginates `list_namespaced_lease` with `_continue=continue_token` (`:99-105`), yet unlike `K8sHelper._claim_list_pages` (which catches `ApiException(status=410)` when etcd compacts a stale `continue` token and restarts from `_continue=None`), `find_stale_leases` lets `410 Gone` propagate uncaught and abort `batch_reaper.reap`.
  2. **`F-18b` (`batch_reaper.py:60-69, 119, 154-190`):** `is_lease_reapable` computes `now > renew_time + timedelta(...)` (`:69`) where `now` is timezone-aware UTC (`datetime.now(UTC)`). If a single manually edited or third-party Lease bearing the batch label has an offset-naive `spec.renew_time`, Python raises `TypeError: can't compare offset-naive and offset-aware datetimes`, aborting `find_stale_leases` and skipping every legitimate stale Lease in that namespace and all remaining namespaces (`:183-190`).
- **Surgical Fix Recommendation:**
  - **Recommended:** Normalize naive `renew_time` (`if renew_time.tzinfo is None: renew_time = renew_time.replace(tzinfo=UTC)`) and isolate per-Lease evaluation errors with a warning log (`continue`), and restart `find_stale_leases` pagination from `continue_token = None` (bounded to e.g. `3` restarts, deduplicating by `(name, uid)`) when `list_namespaced_lease` returns `ApiException(status=410)` on a non-`None` `continue_token`.
  - **Rejected Alternative:** Dropping pagination (`limit`) in `find_stale_leases`; large namespaces can hold thousands of Leases and require bounded page sizes.

---

### `F-19` [`MEDIUM`]: Sub-Second `.strftime` Truncation Makes `BatchClaim.expires_at` Exceed Actual K8s `shutdownTime`, and `FakeFleet(claim_mode="batch")` Leaves `claim_expires_at=None`

- **Violated Rule:** Conservative Claim Expiry Lower Bound (`BatchClaim.expires_at <= controller shutdownTime`) & `FakeFleet` Test Parity
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/utils.py:109, 115`
  - `examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py:59-60, 176-177`
  - `examples/agent-sandbox-rl/agent_sandbox_rl/testing.py:443-447`
- **Owning Commits & PR Routing:** `87754fa1` and `06d29e24` in open branch `25/N` (`origin/feat/rl-batch-claim-mode`), interacting with `c121a21c` (`utils.py:115`) and `841cefbb` (`testing.py:443`). Fold the `batch_claims.py` and `testing.py` fixes directly into `25/N` (`87754fa1`).
- **Discovery Pipeline:** Stage 5b (`test_batch_fault_injection_rl.py::test_rl_batch_claim_expires_at_never_exceeds_truncated_k8s_shutdown_time` and `test_rl_fake_handle_exposes_claim_expires_at_for_recycle_parity`).
- **1-Command Reproduction:**
  ```bash
  pytest examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py -k "test_rl_batch_claim_expires_at_ or test_rl_fake_handle_exposes_claim_expires_at"
  ```
- **Root-Cause Trace:**
  1. **`F-19a` (`utils.py:109, 115` & `batch_claims.py:59-60, 176-177`):** `BatchClaim`'s comment (`batch_claims.py:59-60`) states that `expires_at` is *"measured from before the create, so it is never later than the real one."* However, `construct_sandbox_claim_lifecycle_spec` (`utils.py:109, 115`) formats `shutdownTime` with `shutdown_time.strftime("%Y-%m-%dT%H:%M:%SZ")`, which **floors** microseconds to `.000000Z`! When `claim_batch` or `acquire` runs at wall-clock `T.950s` with a `640s` budget, K8s receives `(T + 640).000Z` (`639.05s` away), while `BatchClaim.expires_at` records `started + 640.0`, overestimating the real K8s `shutdownTime` by up to `0.999999s`.
  2. **`F-19b` (`testing.py:443-447`):** `FakeFleet(FleetConfig(claim_mode="batch")).acquire()` constructs `FakeHandle` without setting `_batch_claim` or `claim_expires_at`, so `fh.claim_expires_at` returns `None` and `recycle._claim_expiring` (`recycle.py:72-86`) never runs in `FakeFleet` framework tests.
- **Surgical Fix Recommendation:**
  - **Recommended:** Subtract `1.0` second from `expires_at` in `BatchClaims.acquire` (`batch_claims.py:176-177`, accounting for whole-second `strftime` floor truncation without touching merged `utils.py` in `25/N`; optionally round up or format microseconds in a separate `main` PR for `utils.py`), and populate `claim_expires_at` (via a lightweight `_FakeBatchClaim(expires_at=...)` or `claim_expires_at` field on `FakeHandle`) in `FakeFleet.acquire()` when `self.config.claim_mode == "batch"`.
  - **Rejected Alternative:** Ignoring sub-second truncation because `BATCH_SHUTDOWN_MARGIN_SECONDS` exists; `recycle._claim_expiring` compares directly against `handle.claim_expires_at`, so violating the stated `<= shutdownTime` invariant creates an exact 1s cliff at the expiry boundary.

---

### `F-20` [`MEDIUM`]: Custom `FleetConfig(labels=...)` Drops `app=agent-sandbox-rl` on `BatchClaims`, and RL Teardown/Reaper Sweeps Ignore Run-Labeled Batch `Lease`s

- **Violated Invariant:** `INV-LEAK-1` (Complete Run-Scoped Label & Reaper Accounting)
- **Verified Locations:**
  - `examples/agent-sandbox-rl/agent_sandbox_rl/fleet.py:224-228, 1926-1976` (contrast `resources.py:122`)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/reaper.py:143-285, 373-378`
- **Owning Commits & PR Routing:** `06d29e24` in open branch `25/N` (`origin/feat/rl-batch-claim-mode`), interacting with `dd38b094` and `c70b415b`. Fold directly into `25/N`.
- **Discovery Pipeline:** Stage 5b (`test_batch_fault_injection_rl.py::test_rl_custom_fleet_labels_preserve_managed_app_label_on_batch_claims` and `test_rl_run_leftovers_and_verify_teardown_account_for_batch_leases`).
- **1-Command Reproduction:**
  ```bash
  pytest examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py -k "test_rl_custom_fleet_labels_ or test_rl_run_leftovers_"
  ```
- **Root-Cause Trace:**
  1. **`F-20a` (`fleet.py:224-228` vs `resources.py:122`):** `Resources.__init__` (`resources.py:122`) merges `{**(labels or {}), **constants.DEFAULT_LABELS}` where `DEFAULT_LABELS = {"app": "agent-sandbox-rl"}`. By contrast, `SandboxFleet.__init__` (`fleet.py:224-228`) constructs `BatchClaims` with `{**config.labels, constants.RUN_ID_LABEL: self.run_id}` without merging `constants.DEFAULT_LABELS` when custom `FleetConfig(labels={"team": "rl"})` is supplied, omitting `app=agent-sandbox-rl` on every batch `Lease` and `SandboxClaim` so `reap(all_managed=True)` (`reaper.py:373`) never discovers them.
  2. **`F-20b` (`fleet.py:1926-1976`, `reaper.py:143-285`):** `SandboxFleet._run_leftovers()`, `verify_teardown()`, and `reaper.reap()` count and delete `claims`, `warmpools`, `templates`, and `pods`, yet never query or delete `coordination.k8s.io/v1` `Lease` objects matching `RUN_ID_LABEL` (`agents.x-k8s.io/asrl-run-id=<run_id>`), reporting `0` leftovers even when leaked batch `Lease`s survive a crashed teardown.
- **Surgical Fix Recommendation:**
  - **Recommended:** Merge `constants.DEFAULT_LABELS` into `BatchClaims` labels at `fleet.py:224-228` (`{**self.config.labels, **constants.DEFAULT_LABELS, constants.RUN_ID_LABEL: self.run_id}`), and include `coordination_v1_api.list_namespaced_lease(..., label_selector=...)` (and `delete_namespaced_lease` in `reap()`) in `_run_leftovers()` and `reaper.reap()`.
  - **Rejected Alternative:** Waiting `leaseDurationSeconds` for external `k8s_agent_sandbox.batch_reaper.reap` cronjobs; `fleet.teardown(verify=True)` promises synchronous run-scoped cleanup verification.

---

### `F-21` [`MEDIUM`]: `SandboxFleet` Leaks Raw `SandboxNotReadyError` on `batch.connect` Race, Skips `leases` RBAC in `preflight()`, and Leaks Partial Handles in `acquire_batch()`

- **Violated Rule:** RL Fleet Error Contract (`BatchError`), Preflight Completeness, and Batch Rollback Atomicity
- **Verified Locations:**
  - `examples/agent-sandbox-rl/agent_sandbox_rl/fleet.py:787-815, 1447-1450, 1542-1547, 1696-1697`
  - `examples/agent-sandbox-rl/agent_sandbox_rl/async_fleet.py:184-185`
  - `examples/agent-sandbox-rl/agent_sandbox_rl/preflight.py:118-290`
- **Owning Commits & PR Routing:** `dab949fb` and `06d29e24` in open branch `25/N` (`origin/feat/rl-batch-claim-mode`), plus `a1a58a01` (`acquire_batch`). Fold `F-21a` and `F-21b` (and `F-21c`) into `25/N`.
- **Discovery Pipeline:** Stage 5b (`test_batch_fault_injection_rl.py::test_rl_batch_connect_not_ready_raises_batch_error`, `test_rl_preflight_verifies_leases_rbac_when_claim_mode_is_batch`, `test_rl_acquire_batch_rolls_back_partial_handles_on_error`).
- **1-Command Reproduction:**
  ```bash
  pytest examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py -k "test_rl_batch_connect_not_ready_ or test_rl_preflight_verifies_leases_ or test_rl_acquire_batch_rolls_back_"
  ```
- **Root-Cause Trace:**
  1. **`F-21a` (`fleet.py:1447-1450, 1542-1547`):** `SandboxFleet.acquire()` documents and tests (`fleet.py:1447-1450`, `test_batch_claims.py:282, 357`) that batch-claim failures raise `BatchError`. However, if the batch watch observes readiness loss in the narrow window between `claim = batch_claims.acquire(...)` and `sandbox = claim.batch.connect(claim.member)` (`fleet.py:1547`), `connect()` raises `SandboxNotReadyError` (which does **not** inherit from `BatchError`), leaking an unexpected exception type to batch-mode callers.
  2. **`F-21b` (`fleet.py:787-815`, `preflight.py:118-290`):** When `claim_mode == "batch"`, `fleet.preflight()` verifies CRDs, controller deployment, namespace, and manifest dry-runs, yet never checks `coordination.k8s.io/v1` `leases` permissions (`create`/`get`/`list`/`replace`/`delete`), letting `preflight()` pass green and `setup()` warm all pools on RBAC-restricted clusters that immediately fail with `403 Forbidden` on the first `claim_batch()`.
  3. **`F-21c` (`fleet.py:1696-1697`, `async_fleet.py:184-185`):** `SandboxFleet.acquire_batch` (`return [self.acquire(t) for t in tasks]`) and `AsyncSandboxFleet.acquire_batch` (`await asyncio.gather(...)`) abandon already-acquired handles `0..k-1` inside `self._handles` when task `k` raises, leaking active sandboxes and `_claims_reserved` slots without returning those handles to the caller.
- **Surgical Fix Recommendation:**
  - **Recommended:** Catch `SandboxNotReadyError` around `claim.batch.connect(claim.member)` at `fleet.py:1547` and raise `BatchError(f"batch member '{claim.member.claim_name}' became not ready before connect") from exc`; pass `check_leases=(self.config.claim_mode == "batch")` into `preflight_cluster` to dry-run or check `coordination.k8s.io` `leases` access; and in sync/async `acquire_batch`, release any already-acquired handles in `except BaseException:` before re-raising.
  - **Rejected Alternative:** Translating `SandboxNotReadyError` into `FleetError` instead of `BatchError`, which contradicts `fleet.py:1447-1450` and existing `test_batch_claims.py:282, 357` contracts.

---

### `F-22` [`MEDIUM`]: 41 `mypy` Static Type Errors Across 10 Modules in `agent_sandbox_rl`

- **Violated Invariant:** `INV-TYPES-1` (Zero Static Type Errors Across SDK and RL Consumers)
- **Verified Locations (Measured via `mypy --ignore-missing-imports examples/agent-sandbox-rl/agent_sandbox_rl`):**
  - `examples/agent-sandbox-rl/agent_sandbox_rl/fleet.py:504, 573, 1401, 1489, 1490, 1560, 1561, 1752` (`11` errors across `8` lines)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/observability.py:280, 307, 311, 313, 320, 340, 346, 354` (`8` errors)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/reaper.py:174, 327, 340, 346` (`5` errors across `4` lines)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/testing.py:137, 141, 143, 144, 447` (`5` errors)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/handles.py:307, 496, 510, 620` (`4` errors)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/preflight.py:228, 301` (`2` errors)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/failfast.py:271, 286` (`2` errors)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/resources.py:748, 757` (`2` errors)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/placement.py:126` (`1` error)
  - `examples/agent-sandbox-rl/agent_sandbox_rl/adapters/openhands.py:192` (`1` error)
- **Owning Commits & PR Routing:** Pre-existing on `upstream/main` (`bcf5d4a0`) because `dev/tools/test-unit` runs `mypy` only on `k8s_agent_sandbox`. Open a dedicated `chore(agent-sandbox-rl): fix mypy type errors and wire CI check` PR off `upstream/main`.
- **Discovery Pipeline:** Stage 9 (`run_batch_verification.sh` & `verify_batch_static.py --with-mypy`).
- **1-Command Reproduction:**
  ```bash
  mypy --ignore-missing-imports examples/agent-sandbox-rl/agent_sandbox_rl
  ```
- **Surgical Fix Recommendation:**
  - **Recommended:** Fix all `41` static type annotations in a standalone `upstream/main` PR and add `mypy --ignore-missing-imports examples/agent-sandbox-rl/agent_sandbox_rl` to `dev/tools/test-unit`.
  - **Rejected Alternative:** Bundling 10-file type cleanup into `25/N` (`feat/rl-batch-claim-mode`), which violates single-concern PR scoping.

---

### `F-23` [`LOW`]: Unlocked `SandboxClient` Reads on `batch._connected`/`_stale_connections`/`_detach_unfinished()`, and `K8sHelper` Unclosed `ApiClient` Pools

- **Violated Invariants:** `INV-PARITY-LOCK-1` (Lock Discipline), `INV-LEAK-2` (Client Pool Cleanup)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_client.py:593, 603`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/k8s_helper.py:206-210`
- **Owning Commits & PR Routing:** `74cd8fa5` (`sandbox_client.py:593, 603`, merged on `upstream/feature/batch-claim` -> standalone PR) and `cd965322` (`k8s_helper.py:207-210` in `22/N` `#2188`).
- **Discovery Pipeline:** Pipeline 2 (`verify_batch_static.py` `[04]`, `[05]`, `[06]`).
- **1-Command Reproduction:**
  ```bash
  python dev/tools/verify_batch_static.py --fail-on LOW
  ```
- **Root-Cause Trace:**
  1. **`F-23a` (`sandbox_client.py:593, 603`):** `SandboxClient._track_closing_batch` (`:593`) reads `batch._connected or batch._stale_connections` and `_finish_detach` (`:603`) calls `batch._detach_unfinished()` without holding `batch._lock`, while concurrent threads mutate those fields under `batch._lock`.
  2. **`F-23b` (`k8s_helper.py:206-210`):** When `K8sHelper(api_client=None)` initializes its own `ApiClient` instances (`api_client`, `batch_api_client`, `unretried_api_client`), `K8sHelper` exposes no `close()` method (unlike `AsyncK8sHelper.close()`), leaving `3` `multiprocessing.pool.ThreadPool` / `urllib3.PoolManager` instances open per `SandboxClient` until GC.
- **Surgical Fix Recommendation:**
  - **Recommended:** Guard `sandbox_client.py:593` and `:603` with `with batch._lock:`, and add `K8sHelper.close()` closing owned `ApiClient` instances from `SandboxClient.close()` / `__exit__`.
  - **Rejected Alternative:** Acquiring `self._lock` inside `SandboxBatch._detach_unfinished()`, which breaks the caller-locks convention shared with `_exit_detaches()` (`sandbox_client.py:719`).

---

### `F-24` [`CRITICAL`]: `release()` and `detach()` Skip Draining In-Flight Dynamic `acquire()` HTTP `POST`s, Permanently Orphaning Newly Created `SandboxClaim`s

- **Violated Invariants:** `INV-LEAK-1` (Zero Orphaned Cluster Claims), `INV-OWN-3` (Lease-Fenced Mutation Drain)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py:864, 887-918, 1110-1117, 1305-1363`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_batch.py:873, 896-927, 1138-1152, 1331-1369`
- **Owning Commits & PR Routing:** `dc9ea439` and `522dad4c`, merged on `upstream/feature/batch-claim`. Open a standalone fix PR off `upstream/feature/batch-claim`.
- **Discovery Pipelines:** Pipeline 1 (`BatchAcquireRelease.tla` `bar_neg_inflight_release`, `88` distinct states violating `NoOrphanedClaimAfterBatchRelease`), Stage 5a (`test_batch_fault_injection.py::TestCell12SdkTeardownFencingAndReaper::test_release_drains_in_flight_acquire_create_without_leaking_claim` and `TestCell12AsyncClientTeardownAndCloseRace::test_async_release_drains_in_flight_acquire_create_without_leaking_claim`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "release_drains_in_flight_acquire_create_without_leaking_claim"
  ```
- **Root-Cause Trace:**
  1. Initial fill creators run inside `self._creators`, so `_stop_background_threads()` (`sandbox_batch.py:1357-1358`) and `_stop_background_tasks()` (`async_sandbox_batch.py:481-490`) join/await all in-flight fill `create_sandbox_claim` `POST`s *before* `release()` runs `_delete_batch_objects` (`deletecollection`).
  2. By contrast, dynamic `acquire()` executes `_check_pool` (`sandbox_batch.py:863`, `async_sandbox_batch.py:872`), `_refresh_warm_credit`, and `_create_one` (`create_sandbox_claim` at `sandbox_batch.py:887-898`, `async_sandbox_batch.py:896-907`) directly on the caller's thread/task **outside** both `self._creators` and `self._member_delete()` (`_active_deletes`).
  3. When another thread/task calls `batch.release()` (or `batch.detach()`) while `acquire()` is mid-`POST` inside `create_sandbox_claim`, `release()` sets `self._detached = True` (`:1309`), sees `_active_deletes == 0` (`:1318`), runs `_delete_batch_objects` (`deletecollection` + `delete_batch_lease`), and returns! When the already-dispatched `create_sandbox_claim` `POST` commits on the apiserver moments later, `acquire()` wakes in `_wait_acquired`, raises `BatchError("... detached or released")`, enters `except BaseException: self._drop_acquired(...)` (`:918`), and `_drop_acquired` (`sandbox_batch.py:1111-1115`, `async_sandbox_batch.py:1143-1149`) calls `_delete_claim`, catches `BatchError` (because `self._detached` is `True`), and executes `pass`, stranding an orphaned live `SandboxClaim` on the cluster!
- **Surgical Fix Recommendation:**
  - **Recommended:** Guard each outbound HTTP attempt in `_send_create` (or dynamic `acquire`'s `_create_one` / `_check_pool` / `_refresh_warm_credit` flight) with an `_active_creates` / `_member_delete()` counter drained by `_wait_active_deletes()` (or `_wait_active_creates()`) after setting `self._detached = True` and `self._stop_creating.set()` in `release()` and `detach()` *before* `_delete_batch_objects` / Lease handoff runs. Every dynamic `POST` then completes before `release()`'s `deletecollection` sweeps the batch label!
  - **Rejected Alternative:** Allowing `_drop_acquired` to call `delete_sandbox_claim` even when `self._detached` is `True`; after `detach()`, another holder may already own the Lease (`INV-OWN-1`), whereas draining in-flight creates *before* `release()`/`detach()` releases the Lease preserves strict Lease fencing.

---

### `F-25` [`HIGH`]: `_create_worker` Pops `_create_plan` Items Before Sleeping on `create_rps` Slots, Making `release_group()` and `release_not_ready()` Stall Consumers

- **Violated Invariants:** `INV-ACCT-2` (Prompt Released-Pool Settlement), `INV-ACCT-4` (Non-Blocking Consumer Termination)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py:1220-1238, 1787-1801`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_batch.py:1253-1270, 1769-1783`
- **Owning Commits & PR Routing:** `113fae28` (`15/N` `#1990`) and `5e379f8a` (`19/N` `#2166`), merged on `upstream/feature/batch-claim`. Open a standalone fix PR off `upstream/feature/batch-claim`.
- **Discovery Pipelines:** Pipeline 1 (`BatchConsumer.tla` `bc_neg_popped_slot`, `33` distinct states violating `ReleasedPoolImmediatelySettled`), Stage 5a (`test_batch_fault_injection.py::TestCell08Create429CooldownAndWarmCreditPause::test_release_group_cancels_popped_pacing_slot_workers_without_stalling_events` and `TestCell08AsyncCreatePacingSlotCancellation::test_async_release_group_cancels_popped_pacing_slot_workers_without_stalling_events`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "release_group_cancels_popped_pacing_slot_workers_without_stalling_events"
  ```
- **Root-Cause Trace:**
  1. `claim_batch` spawns up to `max_in_flight = 20` `_create_worker` threads/tasks immediately. Under `self._lock`, every worker pops `(claim_name, group) = self._create_plan.popleft()` (`sandbox_batch.py:1787`, `async_sandbox_batch.py:1769`), reserves a staggered `slot = self._reserve_create_slot(...)`, and then sleeps **outside** `self._lock` on `self._stop_creating.wait(max(0.0, slot - time.monotonic()))` (`:1800` / `:1782`).
  2. When a caller invokes `release_group(pool)` or `release_not_ready()`, `_drop_planned_creates` (`sandbox_batch.py:1220-1238`, `async_sandbox_batch.py:1253-1270`) iterates only `self._create_plan`. All `<= 20` items already popped by sleeping workers are invisible to `_drop_planned_creates` and absent from `self._state._members`, so `BatchState.release_group` / `release_not_ready` does **not** add them to `_released` or `_resolved`!
  3. Consequently, `self._state.fill_settled()` stays `False`, stalling `batch.events()`, `batch.group_outcomes()`, and `batch.wait_for_quorum()` across the entire multi-second `create_rps` / `429 Retry-After` pacing schedule until every sleeping worker finally wakes up and hits `can_send() -> False`.
- **Surgical Fix Recommendation:**
  - **Recommended:** Track popped, not-yet-sent fill creates in `self._in_flight_plan: dict[str, str]` (`claim_name -> warmpool`) under `self._lock` (or peek/reserve slots before popping, or wake a dedicated `_plan_changed` event when `release_group` / `release_not_ready` runs) so `_drop_planned_creates` immediately calls `self._state.release_claim(claim_name, warmpool)` for popped unstarted claims (settling `fill_settled()` at once!) and wakes sleeping pacing workers so freed slots are not wasted.
  - **Rejected Alternative:** Setting `self._stop_creating` inside `release_group(pool)`, which would erroneously abort initial fill creates for *other* healthy pools in the same batch.

---

### `F-26` [`MEDIUM`]: Dynamic `acquire()` Pacing and `429` Retry Sleeps Ignore Pool Release Waiters, and `_send_create` Waits Unboundedly on `_in_flight`

- **Violated Invariants:** `INV-OWN-3` (Lease-Bounded Semaphore Wait), `INV-LEAK-2` (Prompt Cancellation on Group Release)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py:869, 1926-1927, 2030, 2039`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_batch.py:878, 1919-1924, 2030, 2039`
- **Owning Commits & PR Routing:** `dc9ea439` and `5e379f8a`, merged on `upstream/feature/batch-claim`. Open a standalone fix PR off `upstream/feature/batch-claim`.
- **Discovery Pipeline:** Stage 5a (`test_batch_fault_injection.py::TestCell11SdkAcquireCancellation::test_sdk_async_acquire_retry_backoff_wakes_immediately_on_release_group`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "test_sdk_async_acquire_retry_backoff_wakes_immediately_on_release_group"
  ```
- **Root-Cause Trace:**
  1. Dynamic `acquire()` allocates a per-claim `waiter` (`threading.Event` / `asyncio.Event`) in `self._acquire_waiters[claim_name]` that `release_group(warmpool)` and `release_member()` signal via `_notify_all()`. However, `acquire()`'s initial pacing sleep (`sandbox_batch.py:869` `self._stop_creating.wait(...)`; `async_sandbox_batch.py:878` `await self._wait_for_stop(...)`) and `_create_one`'s transient/`429 Retry-After` sleeps (`:2030, :2039` in both files) wait **only** on `self._stop_creating`! Calling `release_group(pool)` sets `waiter` without setting `_stop_creating`, stranding `acquire()` across multi-second `Retry-After` sleeps (`15s` in our reproduction) before it notices `release_group(pool)` released the pool.
  2. In `_send_create`, `self._in_flight.acquire(...)` (`sandbox_batch.py:1926-1927`; `async_sandbox_batch.py:1919-1924`) bounds its wait only by caller `deadline` (`None` when a caller invokes `acquire(pool)` with default `timeout=None`), rather than clamping to `self._lease_expires_at` or waking on Lease loss.
- **Surgical Fix Recommendation:**
  - **Recommended:** Pass `stop_event` (`waiter` for `acquire()`, `self._stop_creating` for fill workers, with `_notify_all()` also setting all `_acquire_waiters` on `self._stop_creating` / Lease loss) into pacing and `_create_one` retry sleeps, and clamp `_send_create`'s `_in_flight` wait to `self._lease_expires_at`.
  - **Rejected Alternative:** Requiring callers to pass a short `timeout=` on every `acquire()` call; `release_group(pool)` explicitly documents that an `acquire()` waiting on `pool` raises `BatchError` promptly.

---

### `F-27` [`MEDIUM`]: `get_batch()` (`_attach`) Sends Unclamped `replace_batch_lease` That Can Return an Already-Expired Handle, and Skips `check_pool_size`

- **Violated Invariants:** `INV-OWN-1` (Valid Lease Ownership on Return), `INV-AUTH-3` (`lease_degraded()` / Expiry Fencing), `INV-LEAK-2` (Renewal Pool Headroom)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py:414, 442-466` (contrast `sandbox_client.py:509-515` and `get_batch` at `:520-558`)
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_batch.py:411, 439-463` (contrast `async_sandbox_client.py:672-678` and `get_batch` at `:685-724`)
- **Owning Commits & PR Routing:** `3a1607bc` (`1/N` `#1742`, `get_batch` / `_attach`, plus `31a47728` and `77c05c26`) and `6feedc5c` (`batch_utils.check_pool_size`). Fold into `22/N` (`#2188`) or open a standalone PR off `upstream/feature/batch-claim`.
- **Discovery Pipeline:** Stage 5a (`test_batch_fault_injection.py::TestCell12SdkTeardownFencingAndReaper::test_get_batch_fences_slow_attach_lease_write`, `test_get_batch_validates_and_clamps_pool_size`, and async twins `test_async_get_batch_fences_slow_attach_lease_write`, `test_async_get_batch_validates_and_clamps_pool_size`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "get_batch_"
  ```
- **Root-Cause Trace:**
  1. **`F-27a` (`sandbox_batch.py:414, 442-462`, `async_sandbox_batch.py:411, 439-459`):** `_attach` stamps `renewed_at = time.monotonic()` at `:414` / `:411` (after `list_sandbox_claim_objects` at `:397` / `:394`, before `reconstruct_groups`/`seed_from_claims`) and calls `replace_batch_lease(..., _request_timeout=batch_utils.BATCH_REQUEST_TIMEOUT_SECONDS)` (`30s`). When attaching to a short-duration Lease (e.g., `lease_duration=6s`) over a slow apiserver where `replace_batch_lease` takes `7s`, `_attach` succeeds and returns a handle with `self._lease_expires_at = renewed_at + 6` **already in the past** (`now >= _lease_expires_at`, `lease_degraded() == False`, `err() is None`) instead of clamping `_request_timeout` and raising `BatchLeaseExpiredError`!
  2. **`F-27b` (`sandbox_client.py:509-515` vs `get_batch` at `:520-558`; `async_sandbox_client.py:672-678` vs `:685-724`; `batch_utils.py:75, 81, 304-335`):** `claim_batch` calls `batch_utils.check_pool_size(pool_size, max_in_flight, explicit=...)` to reserve `BATCH_POOL_HEADROOM = 2` sockets for Lease renewal and watch (rejecting `pool_size < 3` and clamping implicit `BATCH_DEFAULT_MAX_IN_FLIGHT = 20`). `get_batch()` skips `check_pool_size` completely, accepting `pool_size < 3` and leaving `BoundedSemaphore(20)` (`BATCH_DEFAULT_MAX_IN_FLIGHT`) unclamped even when `api_pool_size() == 2`, allowing concurrent `acquire()` / `release_not_ready()` calls on an attached batch to exhaust the connection pool and starve `_renew_loop`!
- **Surgical Fix Recommendation:**
  - **Recommended:**
    1. In `_attach` (before listing/writing the Lease, matching `claim_batch`), compute `max_in_flight = batch_utils.check_pool_size(pool_size, batch_utils.BATCH_DEFAULT_MAX_IN_FLIGHT, explicit=False)` (`await client.k8s_helper.api_pool_size()` in async) and pass `max_in_flight=max_in_flight` into `cls(..., max_in_flight=max_in_flight)`.
    2. Immediately before `replace_batch_lease` (clamped to `_request_timeout=min(batch_utils.BATCH_REQUEST_TIMEOUT_SECONDS, duration)`), refresh `renewed_at = time.monotonic()` **and** `now = batch_utils.lease_now()` (`spec.renew_time = spec.acquire_time = now`) in that exact order so `LEASE-5` monotonic-before-wall-clock ordering holds. After `replace_batch_lease` returns, verify `time.monotonic() < renewed_at + duration` (matching `_claim` at `sandbox_batch.py:304, 315` and `SandboxBatch.__init__` at `:190`); if expired, **never** call `delete_batch_lease` (existing `SandboxClaim`s carry `ownerReferences` to that Lease, so deleting it would trigger Kubernetes GC on live sandboxes!). Instead, conditionally clear `spec.holder_identity = None` at `written.metadata.resource_version` without advancing `spec.renew_time` (or leave the already-expired Lease in place for `batch_reaper`) and raise `BatchLeaseExpiredError` before `_start()`.
  - **Rejected Alternative:** Calling `delete_batch_lease` on attach timeout (destroys existing batch sandboxes via K8s GC) or stamping `renewed_at = time.monotonic()` *after* `replace_batch_lease` returns (violates `LEASE-5` / `INV-AUTH-2` conservative local deadline).

---

### `F-28` [`MEDIUM`]: `recycle.py` Post-Rotation `reset.prime` Failure Underflows Async `held[img]` to `-1` and Leaks Sync JIT Warm Replicas

- **Violated Invariant:** `INV-LEAK-1` (Exact Warm-Pool Replica Scaling Across Rotation Failures)
- **Verified Locations:** `examples/agent-sandbox-rl/agent_sandbox_rl/recycle.py:336-355, 389-394, 498-519, 558-575`
- **Owning Commit & PR Routing:** `2b68984b` (merged on `upstream/main`, exercised on every batch `max_reuses` or `_claim_expiring` claim rotation in `25/N` `origin/feat/rl-batch-claim-mode`). Fix in `25/N` (or standalone `upstream/main` PR restacked into `25/N`).
- **Discovery Pipeline:** Stage 5b (`test_batch_fault_injection_rl.py::test_rl_async_recycle_reclaim_prime_failure_does_not_underflow_held_replicas` and `test_rl_sync_recycle_reclaim_prime_failure_unwarms_jit_pool`).
- **1-Command Reproduction:**
  ```bash
  pytest -o asyncio_mode=auto examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py -k "test_rl_async_recycle_reclaim_ or test_rl_sync_recycle_reclaim_"
  ```
- **Root-Cause Trace:**
  1. **`F-28a` (`recycle.py:500-519, 530-536, 558-575` in `reuse_git_restore_sandbox_async`):** On task `0`, `reset.prime` sets `recyclable = True` and increments `held[img] += 1` (`:517`). When the claim rotates on `_claim_expiring` or `uses >= max_reuses` (`:527-536`), lines `534-535` decrement `held[img] -= 1` back to `0` and set `handle = None`. On the next task (`pos > 0`), `handle = await afleet.acquire(t)` (`:500`) sets `handle` non-`None`, and line `514` runs `baseline = await afleet._to_thread(reset.prime, handle)` *before* `held[img] += 1` (`:517`). When `reset.prime` raises on the re-claimed sandbox, `finally:` (`:558-575`) sees `holding = handle is not None` (`True`!) and `recyclable` (`True`!), so line `574` decrements `held[img] -= 1` from `0` to **`-1`**, and `_rescale(img)` computes `desired = max(0, active[img] - held[img]) = max(0, 0 - (-1)) = 1`, scaling the warm pool **up** to `1` dangling warm replica after all workers exit!
  2. **`F-28b` (`recycle.py:336-355, 389-394` in `reuse_git_restore_sandbox`):** On post-rotation re-claim (`scale_on_hold and recyclable`), line `337` calls `fleet.warm_image(t.image, replicas_override=1, wait=True)`. If `fleet.acquire(t)` (`:338`) or `reset.prime(handle)` (`:352`) raises before line `354` (`fleet.unwarm_image(t.image)`), `finally:` (`:389-394`) releases `handle` without calling `fleet.unwarm_image(t.image)`, stranding the JIT warm replica.
- **Surgical Fix Recommendation:**
  - **Recommended:** Track an explicit `counted_hold: bool = False` flag in `reuse_git_restore_sandbox_async` (set `True` at `:517` when `held[img] += 1` runs, set `handle = None` right after `await afleet.release(handle)` at `:531` and `:544`, reset `counted_hold = False` at `:534` and `:547` when `held[img] -= 1` runs, and gate line `573` on `if counted_hold:` instead of `if holding:`); in sync `reuse_git_restore_sandbox`, track `jit_warmed: bool = False` (set `True` at `:337`, cleared at `:354`, and call `fleet.unwarm_image(...)` in `finally:` when `jit_warmed` remains `True`).
  - **Rejected Alternative:** Clamping `held[img] = max(0, held[img] - 1)` in `finally:`; when another concurrent shard for the same `img` legitimately holds `held[img] == 1`, a spurious `-1` from the failed shard corrupts the sibling shard's hold count from `1` to `0` (`desired` still off by `+1`!) unless each shard tracks `counted_hold` locally.

---

### `F-29` [`HIGH`]: Uncaught `ApiException(status=404)` TOCTOU Race in `release()` (`_take_batch_lease`), `get_batch()` (`_attach`), and `detach()`

- **Violated Invariants:** `INV-OWN-3` (Preconditioned Lease Mutations & Reaper Coexistence), `INV-LEAK-2` (`release()` Aborting Before `deletecollection`)
- **Verified Locations:**
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py:441-450, 1518-1544, 2137-2147`
  - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_batch.py:438-447, 1520-1544, 2137-2147`
- **Owning Commits & PR Routing:** `522dad4c` (`12/N` `#1824`, `detach` & `_take_batch_lease`) and `3a1607bc` (`1/N` `#1742`, `_attach`), merged on `upstream/feature/batch-claim`. Open a standalone fix PR off `upstream/feature/batch-claim` (or fold into `22/N` `#2188`).
- **Discovery Pipeline:** Stage 5a (`test_batch_fault_injection.py::TestCell12SdkTeardownFencingAndReaper::test_lease_replace_404_toctou_in_release_get_batch_and_detach` and `TestCell12AsyncClientTeardownAndCloseRace::test_async_lease_replace_404_toctou_in_release_get_batch_and_detach`).
- **1-Command Reproduction:**
  ```bash
  pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py -k "lease_replace_404_toctou"
  ```
- **Root-Cause Trace:**
  Unlike `_renew_once` (`sandbox_batch.py:1713-1714`, `async_sandbox_batch.py:1693-1694`), which explicitly handles `ApiException(status=404)` on `replace_batch_lease`, all three other read-modify-write Lease paths only handle `e.status == 409`:
  1. **`release()` -> `_take_batch_lease` (`sandbox_batch.py:2119-2147`, `async_sandbox_batch.py:2119-2147`):** `_take_batch_lease` documents (`:2101-2102`) that it returns `None` if the Lease is missing so `release()` still runs UID-scoped `deletecollection`. If `read_batch_lease` (`GET`, `:2119`) returns a stale Lease and `batch_reaper` deletes that Lease before `replace_batch_lease` (`PUT`, `:2138`), Kubernetes raises `ApiException(status=404)`. Line `2140` (`if e.status != 409: raise`) re-raises raw `ApiException(404)` (which `_is_transient_error` classifies as fatal), aborting `_delete_claims_in_rounds` before `deletecollection` runs!
  2. **`get_batch()` -> `_attach` (`sandbox_batch.py:392-450`, `async_sandbox_batch.py:389-447`):** `get_batch` documents (`sandbox_client.py:533-544`) that `except BatchError:` catches any unattachable batch. If `read_batch_lease` (`:392`) returns an unheld Lease and `batch_reaper` (or another client that attached and released) deletes the Lease during the paginated `list_sandbox_claim_objects` call (`:397`), `replace_batch_lease` (`:442`) raises `ApiException(status=404)`. Line `446` only checks `if e.status == 409:`, letting raw `ApiException(404)` escape `except BatchError:`.
  3. **`detach()` (`sandbox_batch.py:1492-1554`, `async_sandbox_batch.py:1498-1548`):** When `read_batch_lease` returns `None` (`:1495` or `:1539` in sync; `:1501` or `:1540` in async), `detach()` treats the Lease as already released. If the Lease disappears between `read_batch_lease` and `replace_batch_lease` (`:1519` / `:1521`), `ApiException(status=404)` hits `if e.status != 409 or attempt: raise` (`:1527` / `:1529`), propagating raw `ApiException(404)` and leaving `self._lease_released = False`.
- **Surgical Fix Recommendation:**
  - **Recommended:**
    1. In `_take_batch_lease` (sync & async): if `e.status == 404: return None`.
    2. In `_attach` (sync & async): if `e.status == 404:`, raise `BatchNotFoundError(f"batch '{batch_id}' not found in namespace '{namespace}'") from e` when `not claim_items` else `BatchLeaseExpiredError(f"batch '{batch_id}' Lease is missing or stale in namespace '{namespace}'") from e` (matching `:401-406`).
    3. In `detach` (sync & async): if `e.status == 404: break` (which exits to `with self._lock: if self._detach_done(): return`, preserving `BatchError` when `release()` ran re-entrantly on the same thread per `test_sandbox_batch.py:2685-2703` while marking `_lease_released = True` on external Lease deletion).
  - **Rejected Alternative:** Retrying `read_batch_lease` on `404` inside `_take_batch_lease` or `detach`; `404` on `replaceNamespacedLease` already proves the Lease object no longer exists.

---

### 3.13 Seven Audited & Verified Intended Contracts (Recommend NO Production Change)

Per `working_rules.md` line 73, we audited seven candidate behaviors flagged during adversarial review and verified against tests, formal models, and design docs that the existing implementation is **intentional and sound** (zero production changes recommended):

1. **Re-Attached `BatchState.fill_settled()` Reopening on Late-Landing Missing Claims (`batch_state.py:304-309, 851-865`):**
   `count_missing_fill_as_unable()` counts unseen fill ordinals into `_missing`, `_lost`, and `_never_created` so re-attached `get_batch()` can settle without waiting forever on creates that never left a crashed holder. If a delayed create lands later as `PENDING`, `upsert_claim` takes back its `_missing`/`_lost`/`_never_created` count so `events()` delivers its eventual terminal/ready event, while already-decided group and quorum outcomes remain monotonic (`test_batch_state.py:695-710`).
2. **`SandboxHandle.is_alive()` Consulting `read_namespaced_pod_status` Once `record_pod_identity()` Sets `pod_uid` (`handles.py:399-415`):**
   In `claim_mode="batch"`, `is_alive()` short-circuits via `self._batch_claim.still_reaches()` (`0` K8s `GET`s) while `self.pod_uid is None`, and switches to `read_namespaced_pod_status` once the caller opts into `record_pod_identity()` (or `refresh()`) because Kubernetes `SandboxClaim` status does not carry container restart counts needed to detect in-place OOM restarts (`handles.py:361-365`, `test_batch_claims.py:431-442`).
3. **`recycle._claim_expiring` `2 * longest_task_s + reset_timeout` Heuristic & `BATCH_SHUTDOWN_MARGIN_SECONDS` Inclusion (`recycle.py:72-86`, `batch_claims.py:176-177`):**
   Including `BATCH_SHUTDOWN_MARGIN_SECONDS` in `BatchClaim.expires_at` matches the real K8s `shutdownTime` written into `spec.lifecycle.shutdownTime`, while `_claim_expiring` reserves `2 * longest_task_s + reset_timeout` of headroom before retiring a recycled claim (`test_batch_claims.py:478-522`). (Only the sub-second `.strftime` truncation in `F-19a` requires adjustment.)
4. **`SandboxFleet.teardown()` Calling Per-Handle `release_member` Before `BatchClaims.release_all()` and Re-Raising Handle Errors (`fleet.py:1993-2000`, `test_batch_claims.py:213-241`):**
   Per-handle `release()` closes exec sessions and updates fleet metrics/accounting before `BatchClaims.release_all()` deletes each batch's claims and Lease, and propagating the handle release error is an explicit tested contract (`test_batch_claims.py:238`). (Only `F-11a`'s failure to finish releasing handles `1..N-1` and sweeping pools/templates before re-raising is a bug.)
5. **Static Per-Plan Cluster Binding in `SandboxFleet.plan()` (`fleet.py:828-862`):**
   Binding planned images to clusters during `plan()` matches warm-pool placement (`SandboxWarmPool` replicas live on the planned cluster); cross-cluster failover belongs to explicit re-planning rather than `BatchClaims`.
6. **Intentional Single-Flight Async I/O Locks (`async_k8s_helper.py:141` `_init_lock` and `async_sandbox_batch.py:1081` `refresh_lock`):**
   `AsyncK8sHelper._ensure_initialized` holds `self._init_lock` to serialize one-time kubeconfig initialization, and `AsyncSandboxBatch._refresh_warm_credit` (`async_sandbox_batch.py:1066-1091`) holds per-warmpool `refresh_lock` (never `self._lock` or `self._changed`) to coalesce concurrent `list_sandbox_claims` credit probes for the same pool (`verify_batch_static.py` explicitly models and exempts both single-flight I/O locks with `0` false positives).
7. **`AsyncSandboxBatch` `CANCEL-5` Rollback Under `async with self._changed:` Protected by `INV-LOCK-ASYNC-1` (`async_sandbox_batch.py:702, 1138`):**
   In `AsyncSandboxBatch.wait_for_quorum` (`:702`) and `_drop_acquired` (`:1138`), `except BaseException:` acquires `async with self._changed:` before mutating `_state`. Because `verify_batch_static.py` enforces zero `await` points inside every `self._lock` / `self._changed` block across `AsyncSandboxBatch` (`INV-LOCK-ASYNC-1`), `self._changed` is never held across an event-loop yield, so `asyncio.Lock.acquire()` executes synchronously without suspending (`test_sdk_async_acquire_repeated_cancellation_with_concurrent_watch` and `test_differential_acquire_interruption_cleanup_parity` pass `100%` under concurrent watch notifications and repeated `task.cancel()`, while `verify_cancel5_exercise.py` explores `19` states across `3` assertions to guard against any future `await` inside `_changed`).

---

<a id="perf-findings"></a>

### 3.14 Performance & Complexity Findings (`F-30` .. `F-39`, Pipeline 8)

Pipeline 8 (Stage `1b` Check 8 `HOT_PATH_COMPLEXITY`, Stages `10a`/`10b`) measures cost in machine-independent units: executed Python bytecode instructions of the test thread at a small and a large size, whole-table walks inside C builtins, `__eq__` comparisons, outbound request counts, and `tracemalloc` bytes. Line numbers are at `e84e964c` (`25/N`) unless marked `26/N` (`bb443e71`, `origin/feat/rl-batch-hardening`). The SDK is byte-identical on both branches, so every SDK finding applies to both. Every finding was re-confirmed outside the suite by a standalone wall-clock and request-count repro on real `SandboxClient` / `SandboxBatch` / `SandboxFleet` objects (last column).

| ID | Severity | `25/N` | `26/N` | Gate (RED today) | Gate measurement | Standalone repro (wall clock) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **F-30** | `HIGH` | Present | Present, plus `BatchWatchdog` | `test_batch_performance_rl.py::test_batch_claim_still_reaches_is_o1_in_pool_size`, `::test_handle_is_alive_on_lost_claim_is_o1_in_pool_size` (each on a state stand-in and on a real `SandboxBatch`), `::test_batch_watchdog_idle_poll_is_o1_in_batch_size` (`26/N`); Check 8 `INV-PERF-HOTPATH-1` | `still_reaches`: `4,421` vs `146,783` instructions at `N=50` vs `2,000` (`33.2x`); lost-claim `is_alive`: `4,049` vs `146,405` (`36.2x`); watchdog idle poll: `4,184` vs `162,134` (`38.8x`) | `still_reaches` on a real batch: `41.4` vs `1,692.7 us` (`40.9x`); watchdog idle poll: `37.7` vs `1,884.7 us` (`50.0x`) |
| **F-31** | `LOW` | Present | Present | `test_batch_performance_invariants.py::test_has_pool_is_o1_in_batch_and_group_count`, `::test_per_pool_members_and_release_group_scale_with_pool_size`; Check 8 `INV-PERF-HOTPATH-3` | `has_pool`: `1,116` vs `48,156` (`43.2x`); `members(pool)` `43.8x`; `release_group(pool)` `39.0x` | `has_pool`: `11.3` vs `554.5 us` at `G=100` vs `5,000` (`48.9x`) |
| **F-32** | `LOW` | Present | Present | `::test_sync_close_connections_is_linear_in_stale_count`, `::test_async_close_connections_is_linear_in_stale_count`; Check 8 `INV-PERF-HOTPATH-2` | `5,000` vs `1,280,000` comparisons closing `S=50` vs `800` connections behind an equal backlog | `45` vs `8,012 us` (`177.5x` where linear is `16x`) |
| **F-33** | `MEDIUM` | Present | Present | `test_batch_performance_rl.py::test_fleet_release_is_o1_in_active_handles`; Check 8 `INV-PERF-HOTPATH-2` | `11,106` vs `216,306` instructions for one `release()` at `H=50` vs `1,000` (`19.5x`) | `147.4` vs `2,927.9 us` (`19.9x`) |
| **F-34** | `MEDIUM` | Present | **Fixed** (`3f95b264`) | `test_batch_performance_rl.py::test_repeat_on_demand_acquires_skip_pool_and_template_requests`, `::test_fleet_batch_mode_acquire_is_o1_in_pool_size`, `::test_fleet_episodes_retain_no_memory[fleet,per_claim]` | `18` extra requests (`ensure_template` + `create_warmpool`) across `9` repeat acquires; `6,000` across `3,000` churn episodes | `18` extra requests across `9` acquires on `25/N`, `0` on `26/N` |
| **F-35** | `LOW` | Present | Present | Pinned: `::test_acquire_release_churn_retains_only_released_tombstones`; strict expected failure: `::test_released_tombstones_stay_bounded` | `156 B` retained per acquire/release episode, all `BatchState._released` tombstones | `5,000` tombstones after `5,000` episodes, `172 B`/episode |
| **F-36** | `LOW` | Present | Present | `::test_sync_release_group_is_o1_in_planned_pools`, `::test_async_release_group_is_o1_in_planned_pools`; Check 8 `INV-PERF-HOTPATH-2` | First `release_group` during the fill: `7,469` vs `90,769` instructions at `20x5` vs `1,000x5` planned creates (`12.2x`; async `12.6x`) | `600` vs `1,588 us` (`2.6x`) |
| **F-37** | `LOW` | Present | Present | `::test_sync_release_group_wakes_only_its_pool`, `::test_async_release_group_wakes_only_its_pool` | `release_group('pool-a')` wakes `100` of `100` acquires waiting on another pool | `100` of `100` woken |
| **F-38** | `LOW` | Present | Present | Strict expected failure: `test_batch_performance_rl.py::test_per_claim_acquire_reads_the_sandbox_once` | `2` Sandbox `GET`s per per-claim acquire where `1` returns both values | `18` Sandbox `GET`s across `9` acquires |
| **F-39** | `MEDIUM` | Present | Present | `test_batch_performance_rl.py::test_session_run_cost_is_linear_in_output_size`; Check 8 `INV-PERF-HOTPATH-5` | `0.62` vs `1,075.55 ms` for `50` vs `2,000` 1 KiB output chunks (`1,737x` for `40x` the output) | `10.9` vs `1,165.5 ms` for `400 KiB` vs `4 MB` (`106.6x` for `10x`) |

#### `F-30` [`HIGH`]: `BatchClaim.still_reaches()` (and `26/N` `BatchWatchdog._poll_once`) Look Up One Member Through a Full `batch.members(warmpool)` Snapshot

- **Verified Locations:** `examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py:72` (`25/N`), `:92` (`26/N`), called from `SandboxHandle.is_alive()` at every RL step boundary. `26/N` only: `failfast.py:404-411` (every poll copies and sorts every member and rebuilds `O(N)` sets) and `failfast.py:447` (per-verdict re-check). Neither handle exposes `get_member(claim_name)`, although `BatchState.get_member` (`batch_state.py:584`) is `O(1)`.
- **Root Cause:** `SandboxBatch.members(warmpool)` takes the batch lock, walks all `N` members, filters to the pool, and sorts. `still_reaches()` keeps one member. `N` workers checking once per step do `O(N^2 log N)` work per step, serialized on the lock the watch thread needs for every event. The watchdog polls at up to `50 Hz` during acquire bursts.
- **Recommended Fix:** Add `get_member(claim_name)` to both handles (sync under the lock, async without awaiting) and use it in `still_reaches()` and the watchdog re-check; track pending members so an idle poll is `O(1)`.
- **Two-Sided Proof:** With the fix, both RL gates pass at `1.0x`, the handle `get_member` gates pass, Check 8 reports no `INV-PERF-HOTPATH-1` finding, and the only existing test that changes is `test_batch_claims.py`'s `FakeBatch`, which gains `get_member`.

#### `F-31` [`LOW`]: `BatchState.has_pool`, `members(warmpool)`, and `release_group` Scan Every Member and Group

- **Verified Locations:** `batch_state.py:426-448`, `:574-582`; callers are the handles' `release_group()`.
- **Why `LOW`:** These run once per pool release, not per event; releasing all `G` pools of a multi-image batch costs `O(G * N)` under the batch lock.
- **Recommended Fix:** Per-pool indexes `_members_by_pool` and `_acquiring_by_pool`, maintained by one insert and one pop helper each.

#### `F-32` [`LOW`]: `_close_connections` Scans `_stale_connections` (a `list`) per Connection

- **Verified Locations:** `sandbox_batch.py:1393-1400`, `async_sandbox_batch.py:1400-1407`.
- **Why `LOW`:** Quadratic only behind a backlog of failed closes, which stay at the front of the list.
- **Recommended Fix:** An insertion-ordered `dict[Sandbox, None]`.

#### `F-33` [`MEDIUM`]: `SandboxFleet.release(handle)` Runs Two Dataclass-Equality Scans of `self._handles` Under `fleet._lock`

- **Verified Locations:** `fleet.py:1717,1719` (`25/N`), `fleet.py:1857,1859` (`26/N`).
- **Recommended Fix:** Key `self._handles` by `id(handle)` in an insertion-ordered dict.

#### `F-34` [`MEDIUM`, `25/N` only]: Repeat On-Demand `acquire()` Re-Sends `ensure_template` and `create_warmpool`

- **Verified Location:** `fleet.py:1503-1510` (`25/N`). Applies in per-claim and batch mode. **Fixed on `26/N`** by `3f95b264`.

#### `F-35` [`LOW`]: `BatchState._released` Keeps One Tombstone per Released Claim for the Life of a Dynamic Batch

- **Verified Location:** `batch_state.py:256`, never pruned (about `1.5 GB` per `10M` episodes).
- **Recommended Fix:** Prune once the claim's `DELETED` event is seen and no create for it can still land, or keep an ordinal low-watermark. The strict expected-failure gate then passes, so remove its marker.

#### `F-36` [`LOW`]: The Handles' `release_group()` Walks the Whole Fill Plan to Drop One Pool's Planned Creates

- **Verified Locations:** `sandbox_batch.py:1220-1232` (`_drop_planned_creates`), `async_sandbox_batch.py:1253-1265`.
- **Why `LOW`:** Once per pool release during the fill; releasing all `G` pools costs `O(G^2)` under the batch lock (about `1 s` at `G=1,000`).
- **Recommended Fix:** Index planned creates per pool (`_plan_by_pool`) with a live counter; the create worker skips dropped entries.

#### `F-37` [`LOW`]: `release_group()` Wakes Every Waiting Acquire, Not Only That Pool's

- **Verified Locations:** `sandbox_batch.py:1210`, `async_sandbox_batch.py:1243` (`self._notify_all()`).
- **Recommended Fix:** `notify_all()` on the condition plus waking only the released pool's acquiring names.

#### `F-38` [`LOW`]: A Per-Claim Acquire Reads the Sandbox Twice

- **Verified Locations:** `fleet.py:1562,1564` (`25/N`), `:1610,1612` (`26/N`) call `sandbox.get_pod_name()` and `sandbox.get_pod_ip()`; each `GET`s the Sandbox (`sandbox.py:103-113`, `:131-142`).
- **Recommended Fix:** One read that returns both, or seed the pod IP from the read `get_pod_name()` already makes. The strict expected-failure gate then passes, so remove its marker.

#### `F-39` [`MEDIUM`]: `SandboxSession.run` Re-Joins and Re-Scans Its Whole Output Buffer on Every Chunk

- **Verified Location:** `handles.py:186-222` (`:217`), both branches. This is the per-command path of a recycled sandbox with `use_session=True`.
- **Root Cause:** Each stdout chunk runs `"".join(buf)` and `marker in text` over everything received so far, so `K` chunks copy and scan `O(K^2)` bytes. The copying happens in C: instruction counts and peak memory stay linear, so only time (and Check 8's `INV-PERF-HOTPATH-5`) shows it.
- **Recommended Fix:** Keep the last `len(marker) - 1` characters, test `marker in tail + chunk`, and join once when the marker arrives.
- **Two-Sided Proof:** With that fix, the repro scales linearly (`0.6`, `2.6`, `9.3 ms` for `1x`, `4x`, `10x` the output) and all `21` session unit tests pass.

## 4. Coverage Matrices: Master Invariants, Chaos Suites, & 22-Run TLA+ Suite

### 4.1 Master Invariant Coverage (`comprehensive_verification_strategy.md` Section 2)

| Invariant ID | Specification Contract | Enforcing Pipelines | Status on `e84e964c` |
| :--- | :--- | :--- | :--- |
| `INV-OWN-1` | Lease-Before-Claim & Lease-Fenced Claim Mutations | `BatchLease.tla`, `batch_invariant_oracle.py`, `test_batch_fault_injection.py` | **VIOLATED -> `F-17b`, `F-27a`** |
| `INV-OWN-2` | UID-Scoped Selectors (`batch-uid=<lease.uid>` on list/watch/deletecollection) | `BatchLease.tla`, `test_batch_fault_injection.py` Cell 02/05, `test_batch_concurrency_stress.py` | **PASS** |
| `INV-OWN-3` | Preconditioned Lease Mutations, Lease-Bounded Collection/Semaphore Waits, & Reaper | `BatchLease.tla`, `BatchAcquireRelease.tla`, `verify_batch_static.py`, `test_batch_fault_injection.py` | **VIOLATED -> `F-04`, `F-18`, `F-24`, `F-26`, `F-29`** |
| `INV-OWN-4` | Zero Post-Release Stragglers (`_pending_deletes` / `_released` capture every create/acquire) | `BatchWatch.tla`, `BatchAcquireRelease.tla`, `verify_delete_lifecycle_model.py`, `test_batch_fault_injection.py`, `test_batch_state_fuzz.py` | **VIOLATED -> `F-03`** |
| `INV-AUTH-1` | Watch-Failure Isolation (`_watch_loop` error never halts `_renew_loop`) | `BatchHandle.tla`, `verify_batch_static.py`, `test_batch_fault_injection.py` Cell 01 | **VIOLATED -> `F-01`** |
| `INV-AUTH-2` | Monotonic Fence (`now >= _lease_expires_at` fences outbound K8s mutations) | `BatchHandle.tla`, `test_batch_fault_injection.py` Cell 05/06/07/12 | **VIOLATED -> `F-01`, `F-04`** |
| `INV-AUTH-3` | `lease_degraded()` Accuracy & Valid Expiry on Attach | `BatchHandle.tla`, `test_batch_fault_injection.py` Cell 01/06/07/12 | **VIOLATED -> `F-01`, `F-27a`** |
| `INV-WATCH-HTTP-1` | Async Watch HTTP Error & `Retry-After` Header Fidelity | `verify_batch_static.py`, `test_batch_fault_injection.py` Cell 01 | **VIOLATED -> `F-09`** |
| `INV-ACCT-1` | Conservation of Claims (`_resolved`, `_waiting`, `_released`, `fill_settled()`) | `BatchConsumer.tla`, `batch_invariant_oracle.py`, `test_batch_state_fuzz.py`, `test_batch_differential.py` | **VIOLATED -> `F-12`** |
| `INV-ACCT-2` | Monotonic Ordinal Allocation & Prompt Released-Pool Settlement | `BatchConsumer.tla`, `batch_invariant_oracle.py`, `test_batch_fault_injection.py`, `test_batch_concurrency_stress.py` | **VIOLATED -> `F-25`** |
| `INV-ACCT-3` | Consumer Mode Exclusivity & Re-Attach Ordinal Uniqueness | `BatchConsumer.tla` (`bc_main` vs `bc_neg_stale_ordinal`), `batch_invariant_oracle.py`, `test_batch_differential.py` | **PASS** |
| `INV-ACCT-4` | Event Delivery Completeness & Non-Blocking Consumer Termination | `BatchConsumer.tla`, `BatchWatch.tla`, `test_batch_fault_injection.py`, `test_batch_state_fuzz.py`, `test_batch_differential.py` | **VIOLATED -> `F-25`** |
| `INV-CONN-1` | Active Connection Validity & Single-Flight Sharing (`is_active == True`, `refresh()`) | `BatchWatch.tla`, `test_batch_fault_injection.py`, `test_batch_fault_injection_rl.py`, `test_batch_concurrency_stress.py` | **VIOLATED -> `F-10a`, `F-10c`, `F-17a`** |
| `INV-CONN-2` | Stale Endpoint Eviction (readiness loss / Pod IP change evicts `_connected`) | `BatchWatch.tla` (`bw_main` vs `bw_neg_stale_conn`), `batch_invariant_oracle.py` | **PASS** |
| `INV-CONN-3` | Retryable Connection Teardown (`close_connection()` failure retains entry for retry) | `test_batch_fault_injection.py` Cell 12 | **PASS** |
| `INV-LEAK-1` | Zero Orphaned Batches, Claims, Triggers, Pools, Replicas, or Leases Across Teardown | `BatchWatch.tla`, `BatchAcquireRelease.tla`, `RLBatchLifecycle.tla`, `verify_batch_static.py`, `test_batch_fault_injection.py`, `test_batch_fault_injection_rl.py` | **VIOLATED -> `F-03`, `F-07`, `F-08`, `F-10b`, `F-11`, `F-20`, `F-21c`, `F-24`, `F-28`** |
| `INV-LEAK-2` | Zero Leaked Threads, Generators, Waiters, Client Pools, or Slots on `BaseException` | `verify_batch_static.py`, `verify_cancel5_exercise.py`, `test_batch_fault_injection.py`, `test_batch_fault_injection_rl.py` | **VIOLATED -> `F-05`, `F-06`, `F-15`, `F-16`, `F-23b`, `F-26`, `F-27b`, `F-29`** |
| `INV-LOCK-ASYNC-1` | Zero `await` I/O Under Shared `asyncio.Lock` / `Condition` | `verify_batch_static.py`, `test_batch_concurrency_stress.py` | **VIOLATED -> `F-15`** |
| `INV-OWN-TIMEOUT-3` | Bounded `_request_timeout` on Outbound Claim/Lease API Calls | `verify_batch_static.py`, `test_batch_fault_injection_rl.py` | **VIOLATED -> `F-14`** |
| `INV-PARITY-LOCK-1` | Lock Hierarchy (`_lifecycle_lock -> _batches_lock -> _lock`) & Field Guard Discipline | `verify_batch_static.py`, `verify_delete_lifecycle_model.py`, `test_batch_differential.py` | **VIOLATED -> `F-23a`** |
| `INV-TYPES-1..2` | Static Type Cleanliness (`mypy`) & PEP 561 `__all__` Public Exports | `verify_batch_static.py`, `run_batch_verification.sh` Stage 9 | **VIOLATED -> `F-13`, `F-22`** |

### 4.2 Fault-Injection Suite Results (Stage 5a Pure SDK + Stage 5b RL Batch)

#### Stage 5a: Pure SDK 12-Cell Chaos Matrix (`test_batch_fault_injection.py` - `36` tests: `13 passed`, `23 failed`)

| Cell | Subsystem | Injected Faults | Outcome on `e84e964c` | Verified Behavior / Findings Reproduced |
| :--- | :--- | :--- | :--- | :--- |
| **Cell 01** | `_watch_loop` vs `_renew_loop` & `AsyncK8sHelper` | Sync/async `403` fatal watch error; non-JSON HTTP `403`/`503` & JSON `429` `Retry-After` on async watch | **`3 FAILED` -> `F-01` (x2), `F-09`** | Reproduces sync/async renewal halt (`F-01`) and swallowed HTTP error / dropped `Retry-After` (`F-09`). |
| **Cell 02** | `_watch_loop` & `BatchState` | `410 Gone` multi-page relist; late watch `ADDED` after `release_not_ready()` | **`1 PASSED, 1 FAILED` -> `F-03`** | `410` relist preserves tombstones; late `ADDED` tombstones claim without K8s `DELETE` (`F-03`). |
| **Cell 03** | `_watch_loop` | `ssl.SSLEOFError` vs `ssl.SSLCertVerificationError` | **`1 PASSED`** | `SSLEOFError` retries with backoff; cert verification error stops watch cleanly. |
| **Cell 04** | `_watch_loop` | Short `<10s` clean EOF watch stream vs `>=10s` healthy stream | **`1 PASSED`** | Short stream increments backoff; `>=10s` stream resets failure counter to `0`. |
| **Cell 05** | `_renew_loop` | `409 Conflict` (same `holderIdentity` RV resync vs foreign takeover) | **`1 PASSED`** | Resyncs Lease RV when still held; fences immediately with `BatchInUseError` on takeover. |
| **Cell 06** | `_renew_loop` | `503` transient error -> `lease_degraded()` -> `200` recovery -> monotonic expiry | **`1 PASSED`** | Transitions `lease_degraded()` `False -> True -> False`, and fences once `now >= _lease_expires_at`. |
| **Cell 07** | `_renew_loop` | `404 NotFound` & `SSLCertVerificationError` on Lease renewal | **`2 PASSED`** | `404` stops immediately with `BatchLeaseExpiredError`; cert error marks `lease_degraded() == True`. |
| **Cell 08** | `_create_worker` | `429 TooManyRequests` (`Retry-After: 12`) cooldown; sync & async `release_group` vs popped pacing-slot workers | **`1 PASSED, 2 FAILED` -> `F-25` (x2)** | Sets `_warm_credit._throttled_until` and marks `CreateFailed`; sync & async popped pacing slots stall `events()` on `release_group` (`F-25`). |
| **Cell 09** | `acquire()` | `404 NotFound` on warmpool precheck followed by pool creation | **`1 PASSED`** | Evicts failed precheck from `_prechecks` so subsequent `acquire()` succeeds. |
| **Cell 10** | `acquire()` | Missing `SandboxTemplate` / terminal claim status condition | **`1 PASSED`** | Raises `SandboxTemplateNotFoundError` / `TerminalMemberError` cleanly and deletes failed claim. |
| **Cell 11** | `AsyncSandboxBatch.acquire()` | Single `CancelledError`, repeated `CancelledError` under concurrent watch, & `429 Retry-After` wake on `release_group` | **`2 PASSED, 1 FAILED` -> `F-26`** | Single and repeated cancellation under concurrent watch roll back cleanly; `429` backoff ignores `release_group` (`F-26`). |
| **Cell 12** | SDK Teardown, `get_batch`, `connect`, `terminate`, `batch_reaper`, `AsyncSandboxClient` | Failed `close_connection`, unclamped `release_group`, unjoined `_watch_thread`, sync/async in-flight `acquire` vs `release`, sync/async slow `get_batch` Lease `PUT` & `pool_size=2`, sync/async Lease `PUT` `404` TOCTOU, closed cached `Sandbox`, unfenced `terminate()`, reaper `410` & naive `renew_time`, `__aexit__` batch drop, `close()` vs `release()` race | **`1 PASSED, 16 FAILED` -> `F-04, F-08, F-15, F-16, F-17a, F-17b, F-18a, F-18b, F-24 (x2), F-27a (x2), F-27b (x2), F-29 (x2)`** | Sync `close_connection` retry passes; reproduces all `16` sync/async SDK teardown, attach, fencing, TOCTOU, reaper, and client failures live. |

#### Stage 5b: RL Batch-Claiming Fault-Injection Suite (`examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py` - `20` tests: `0 passed`, `20 failed`)

| Test Function | Target Module | Finding Reproduced |
| :--- | :--- | :--- |
| `test_rl_async_fleet_cancelled_acquire_reclaims_real_handle_and_slot` | `async_fleet.py:179-182` | **`F-05`** (`_claims_reserved` + handle/claim leak on `CancelledError`) |
| `test_rl_sync_fleet_keyboard_interrupt_rolls_back_reserved_slot` | `fleet.py:1473, 1567` | **`F-06`** (`_claims_reserved` + `BatchClaim` leak on `KeyboardInterrupt` in `acquire`) |
| `test_rl_sync_fleet_release_keyboard_interrupt_restores_handle_to_fleet` | `fleet.py:1719, 1737` | **`F-06`** (handle dropped from `_handles` on `KeyboardInterrupt` in `release`) |
| `test_rl_batch_claims_teardown_race_tracks_batch_and_raises_fleet_error` | `batch_claims.py:135-155` | **`F-07a`** (untracked raced batch + raw `ApiException(503)`) |
| `test_rl_batch_claims_release_all_retains_failed_batches_for_retry` | `batch_claims.py:184-192` | **`F-07b`** (`_opened` cleared upfront, losing failed batches on retry) |
| `test_rl_batch_claim_release_fallback_passes_bounded_request_timeout` | `batch_claims.py:87` | **`F-14`** (`request_timeout=None` on fallback `Resources.delete_claim`) |
| `test_rl_batch_handle_refresh_reconnects_closed_sandbox_after_watch_eviction` | `handles.py:475-519` | **`F-10a`** (`refresh()` leaves watch-evicted `handle.sandbox` closed) |
| `test_rl_batch_handle_release_cleans_snapshot_triggers` | `handles.py:606-620` | **`F-10b`** (`release()` skips `delete_manual_triggers()`) |
| `test_rl_stopped_batch_fallback_release_closes_sandbox_connection` | `batch_claims.py:78-88` | **`F-10c`** (fallback `release()` leaves `sandbox` connection open) |
| `test_rl_fleet_teardown_sweeps_remaining_handles_and_pools_when_handle_0_raises` | `fleet.py:1875, 1983-2015` | **`F-11a`** (handle `0` failure skips `h1` and pool/template sweeps) |
| `test_rl_post_teardown_acquire_fails_fast_without_creating_remote_pools` | `fleet.py:1462-1524` | **`F-11b`** (post-teardown `acquire()` creates template/warmpool) |
| `test_rl_batch_claim_expires_at_never_exceeds_truncated_k8s_shutdown_time` | `utils.py:115`, `batch_claims.py:176` | **`F-19a`** (`.strftime` floor makes `expires_at` up to `1s` too late) |
| `test_rl_fake_handle_exposes_claim_expires_at_for_recycle_parity` | `testing.py:443-447` | **`F-19b`** (`FakeFleet(claim_mode="batch")` leaves `claim_expires_at=None`) |
| `test_rl_custom_fleet_labels_preserve_managed_app_label_on_batch_claims` | `fleet.py:224-228` | **`F-20a`** (custom labels drop `app=agent-sandbox-rl` on `BatchClaims`) |
| `test_rl_run_leftovers_and_verify_teardown_account_for_batch_leases` | `fleet.py:1926`, `reaper.py:143` | **`F-20b`** (`_run_leftovers` / `reap` ignore run-labeled batch `Lease`s) |
| `test_rl_acquire_batch_rolls_back_partial_handles_on_error` | `fleet.py:1696`, `async_fleet.py:184` | **`F-21c`** (`acquire_batch` leaks `h0` when item `1` raises) |
| `test_rl_batch_connect_not_ready_raises_batch_error` | `fleet.py:1547` | **`F-21a`** (raw `SandboxNotReadyError` escapes `fleet.acquire` instead of `BatchError`) |
| `test_rl_preflight_verifies_leases_rbac_when_claim_mode_is_batch` | `fleet.py:787`, `preflight.py:118` | **`F-21b`** (`preflight()` skips `coordination.k8s.io` `leases` check) |
| `test_rl_async_recycle_reclaim_prime_failure_does_not_underflow_held_replicas` | `recycle.py:514, 572-575` | **`F-28a`** (post-rotation `prime` failure underflows `held[img]` to `-1`, scaling `1` dangling warm replica) |
| `test_rl_sync_recycle_reclaim_prime_failure_unwarms_jit_pool` | `recycle.py:337-354, 389-394` | **`F-28b`** (post-rotation `prime` failure leaks sync `replicas_override=1` JIT warm replica) |

### 4.3 Modular 22-Run TLA+ Formal Suite (`handoff/work/tla/runall_subsystems.sh`)

All `5` new subsystem specifications (`BatchHandle.tla`, `BatchWatch.tla`, `BatchConsumer.tla`, `BatchAcquireRelease.tla`, `RLBatchLifecycle.tla`) run with TLC deadlock checking enabled (`0` deadlocks) and `100%` `-coverage 1` action reachability (`0` zero-hit actions); only legacy finite-clock `BatchLease.tla` (`MaxTime = 14`) passes `-deadlock`. Across all `22` model-checking runs (`54s`), TLC explores **`3,275,347` distinct states** (`17,873,233` generated states: `3,272,848` distinct / `17,866,937` generated across `8` positive specs + `2,499` distinct / `6,296` generated across `14` deterministic `-workers 1` negative controls):

| Spec Module | Config | Mode | Distinct States | Generated States | Outcome | Exact `.cfg` `INVARIANTS` Verified / Violated |
| :--- | :--- | :--- | ---: | ---: | :--- | :--- |
| `BatchHandle.tla` | `bh_fixed` | Positive (`-workers auto`) | `3,280` | `9,266` | `PASS` | `ActiveWorkloadProtected`, `AuthoritySeparation`, `CleanMemberReleaseAllowed`, `CachedLeaseRvConsistent`, `ExpiredImpliesDegraded` |
| `BatchHandle.tla` | `bh_ext_loss` | Positive (`-workers auto`) | `4,392` | `11,758` | `PASS` | `AuthoritySeparation`, `CleanMemberReleaseAllowed`, `CachedLeaseRvConsistent`, `ExpiredImpliesDegraded` (under external `404`/`409` Lease loss) |
| `BatchWatch.tla` | `bw_main` | Positive (`-workers auto`) | `228,680` | `2,367,197` | `PASS` | `NoStaleEndpointCached`, `ReleasedOrLostEvicted`, `ReleasedNeverResurrected`, `NoFalseLostOnRelist`, `NoOrphanedLiveReleasedClaim` |
| `BatchConsumer.tla` | `bc_main` | Positive (`-workers auto`) | `1,884,448` | `12,921,545` | `PASS` | `ResolvedPartitionAccounting`, `NoFailedGroupMemberDispatched`, `MonotonicUniqueOrdinals`, `NoStrandedWaitingAfterOutcome`, `ReleasedPoolImmediatelySettled` |
| `BatchAcquireRelease.tla` | `bar_main` | Positive (`-workers auto`) | `2,016` | `5,752` | `PASS` | `NoForeignGroupSurvival`, `NoPendingDeleteLost`, `DispatchedProtectedFromReleaseNotReady`, `NoOrphanedClaimAfterBatchRelease` |
| `RLBatchLifecycle.tla` | `rl_main` | Positive (`-workers auto`) | `643` | `2,074` | `PASS` | `ClosedPreventsLiveInstall`, `NoOrphanedRenewingBatch` |
| `BatchLease.tla` | `main_claim_setup` | Positive (`-workers auto -deadlock`) | `663,265` | `1,221,646` | `PASS` | `ReaperSafety`, `ReaperSafetyConnected`, `AtMostOneBeliever`, `BelieverIsHolder`, `BelieverKeepsClaims`, `ReleaseFencing`, `ClaimSetupFencing` |
| `BatchLease.tla` | `main_member_ops` | Positive (`-workers auto -deadlock`) | `486,124` | `1,327,699` | `PASS` | `ReaperSafety`, `ReaperSafetyConnected`, `AtMostOneBeliever`, `BelieverIsHolder`, `BelieverKeepsClaims`, `ReleaseFencing`, `ClaimSetupFencing` |
| `BatchHandle.tla` | `bh_neg_janet` | Baseline Bug (`-workers 1`) | `74` | `148` | `VIOLATE` (expected) | Violates `ActiveWorkloadProtected` -> reproduces **`F-01`** (renewal halt) |
| `BatchHandle.tla` | `bh_neg_janet_release` | Baseline Bug (`-workers 1`) | `11` | `15` | `VIOLATE` (expected) | Violates `CleanMemberReleaseAllowed` -> reproduces **`F-01`** (blocked release) |
| `BatchWatch.tla` | `bw_neg_late_added` | Baseline Bug (`-workers 1`) | `20` | `29` | `VIOLATE` (expected) | Violates `NoOrphanedLiveReleasedClaim` -> reproduces **`F-03`** (late `ADDED` leak) |
| `BatchConsumer.tla` | `bc_neg_waiting_leak` | Baseline Bug (`-workers 1`) | `77` | `140` | `VIOLATE` (expected) | Violates `ResolvedPartitionAccounting` -> reproduces **`F-12`** (`_waiting` clear) |
| `BatchConsumer.tla` | `bc_neg_popped_slot` | Baseline Bug (`-workers 1`) | `33` | `37` | `VIOLATE` (expected) | Violates `ReleasedPoolImmediatelySettled` -> reproduces **`F-25`** (popped slot stall) |
| `BatchAcquireRelease.tla` | `bar_neg_inflight_release` | Baseline Bug (`-workers 1`) | `88` | `170` | `VIOLATE` (expected) | Violates `NoOrphanedClaimAfterBatchRelease` -> reproduces **`F-24`** (in-flight `acquire` vs `release`) |
| `RLBatchLifecycle.tla` | `rl_neg_teardown_race` | Baseline Bug (`-workers 1`) | `16` | `21` | `VIOLATE` (expected) | Violates `NoOrphanedRenewingBatch` -> reproduces **`F-07a`** (`_batch_for` race) |
| `RLBatchLifecycle.tla` | `rl_neg_clear_upfront` | Baseline Bug (`-workers 1`) | `14` | `19` | `VIOLATE` (expected) | Violates `NoOrphanedRenewingBatch` -> reproduces **`F-07b`** (`release_all` upfront clear) |
| `BatchWatch.tla` | `bw_neg_stale_conn` | Mutation Control (`-workers 1`) | `1,057` | `3,121` | `VIOLATE` (expected) | Violates `NoStaleEndpointCached` (`skipEvictOnPodSwap` -> `INV-CONN-2`) |
| `BatchWatch.tla` | `bw_neg_drop_page1` | Mutation Control (`-workers 1`) | `412` | `974` | `VIOLATE` (expected) | Violates `NoFalseLostOnRelist` (`dropPage1OnRelist` -> `INV-ACCT-4`) |
| `BatchWatch.tla` | `bw_neg_resurrect` | Mutation Control (`-workers 1`) | `142` | `369` | `VIOLATE` (expected) | Violates `ReleasedNeverResurrected` (`resurrectReleasedOnRelist` -> `INV-OWN-4`) |
| `BatchConsumer.tla` | `bc_neg_stale_ordinal` | Mutation Control (`-workers 1`) | `176` | `453` | `VIOLATE` (expected) | Violates `MonotonicUniqueOrdinals` (`staleOrdinalOnReattach` -> `INV-ACCT-3`) |
| `BatchAcquireRelease.tla` | `bar_neg_seq` | Mutation Control (`-workers 1`) | `249` | `546` | `VIOLATE` (expected) | Violates `NoPendingDeleteLost` (`unsequencedPendingDelete` -> `INV-OWN-4`) |
| `BatchAcquireRelease.tla` | `bar_neg_pool_seq` | Mutation Control (`-workers 1`) | `130` | `254` | `VIOLATE` (expected) | Violates `NoForeignGroupSurvival` (`ignorePoolReleaseSeq` -> `INV-OWN-4`) |

---

## 5. Developer & Builder/Verifier Subagent Runbook

### Applying the Verification Suite to Any Branch Checkout

We packaged all `10` repository-targeted verification files cleanly without modifying `/usr/local/google/home/brianknguyen/agent-sandbox` or `/usr/local/google/home/brianknguyen/agent-sandbox-restack`:
- **Git Patch (`7,455` lines):** `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/deliverables/verification_suite/verification_suite.patch`
- **Directory Overlay:** `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/deliverables/verification_suite/overlay/`
- **TLA+ Formal Suite & BFS Models:** `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/work/`

Apply the entire suite cleanly inside any `review/<area>/<branch>` checkout:
```bash
git apply --whitespace=error /usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/deliverables/verification_suite/verification_suite.patch
```
Note for SDK-only branches (`22/N`..`24/N`, before `25/N` adds `batch_claims.py`): `verify_batch_static.py` and `run_batch_verification.sh` automatically handle branches with or without `batch_claims.py` (and auto-`SKIP` Stages 2/3 when sibling `agent-sandbox-notes` is absent unless the caller passes `--tla-dir` / `--bfs-dir` explicitly); on pre-`25/N` branches, exclude the RL test file:
```bash
git apply --whitespace=error --exclude='examples/agent-sandbox-rl/*' /usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/deliverables/verification_suite/verification_suite.patch
```

### Single-Command Verification Pipelines

1. **Full 10-Stage Master Suite (`~95s` warm cache, zero cache pollution inside `REPO_ROOT`):**
   ```bash
   TLA_DIR=/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/work/tla \
   BFS_DIR=/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/work \
     ./dev/tools/run_batch_verification.sh
   ```
2. **Fast Pre-Commit Sanity Sweep (`~45s`, skips TLA+ & full RL mypy):**
   ```bash
   ./dev/tools/run_batch_verification.sh --quick
   ```
3. **Targeted Individual Pipelines for Builder/Verifier Pairs:**
   - **Static AST & Call-Graph Auditor (`<1s`):** `python dev/tools/verify_batch_static.py`
   - **22-Run TLA+ Formal Suite (`54s`):** `/usr/local/google/home/brianknguyen/agent-sandbox-notes/batch-primitives/handoff/work/tla/runall_subsystems.sh`
   - **Pure SDK Fault-Injection Suite (`7s`):** `pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py`
   - **RL Batch Fault-Injection Suite (`1s`):** `pytest -o asyncio_mode=auto examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py`
   - **Stateful Hypothesis Fuzzer (`15s`):** `pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_state_fuzz.py`
   - **Sync/Async Lockstep Differential Fuzzer (`3s`):** `pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_differential.py`
   - **Concurrency, Lease CAS, & Lock Stress Suite (`4s`):** `pytest clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_concurrency_stress.py`

---

## 6. PR-Stack Routing Map for Builder/Verifier Subagents

1. **Open PR `22/N` (`#2188`, `origin/feat/batch-10-deletecollection-retry`):**
   - Fold **`F-01`** (`self._lease_error` authority separation + `_stop_renewal` setting `_watch_stop` / cancelling `_watch_task`) and **`F-04`** (`deadline` parameter on `count_live_sandbox_claims` + clamp `_delete_claims_in_rounds` `_request_timeout` and `deadline` to `_lease_expires_at` for `release_group`) into commit `540465e8`.
   - Fold **`F-02`** (`ConnectTimeoutError` alongside `NewConnectionError` in `test_k8s_helper.py:1243`) and **`F-23b`** (`K8sHelper.close()`) into commit `cd965322`.
   - Fold **`F-18`** (`batch_reaper.find_stale_leases` `410` pagination restart + naive `renew_time` isolation) and optionally **`F-27`** (`get_batch` Lease `PUT` timeout/expiry fence + `check_pool_size`) into `22/N` (or standalone PRs off `upstream/feature/batch-claim`).
2. **Open Branch `25/N` (`origin/feat/rl-batch-claim-mode`):**
   - Fold **`F-07`** (`BatchClaims._batch_for` unconditional `_opened` registration + retry-preserving `release_all`), **`F-10`** (`SandboxHandle.refresh()` batch reconnect, `release()` `_snapshots` trigger cleanup without lazy `SnapshotEngine` allocation, and fallback `close_connection()`), **`F-14`** (shared `constants.REQUEST_TIMEOUT` on `Resources.delete_claim`), **`F-19`** (`1s` `.strftime` floor adjustment on `BatchClaim.expires_at` + `FakeFleet` `claim_expires_at`), **`F-20`** (`constants.DEFAULT_LABELS` on `BatchClaims` + batch `Lease` accounting in `_run_leftovers` / `reaper.reap`), **`F-21`** (`BatchError` wrapping on `batch.connect` `SandboxNotReadyError`, `leases` check in `preflight()`, and `acquire_batch` rollback), and `batch_claims.py:135` `SandboxBatch` type annotation (**`F-13`**) into `25/N`.
   - Fold **`F-05`** (`AsyncSandboxFleet.acquire` `self._thread_pool().submit` + `concurrent.futures.Future.add_done_callback` handle/slot reclaim), **`F-06`** (`except BaseException:` across all `5` rollback sites in `fleet.py:1473, 1567, 1737, 1852, 1869`), **`F-11`** (`_teardown` complete handle + pool/template sweep before re-raising `first_exc`, plus entry `_torndown` check in `acquire()`), and **`F-28`** (`counted_hold` in `reuse_git_restore_sandbox_async` + `jit_warmed` unwarm in `reuse_git_restore_sandbox`) into `25/N` (or land on `upstream/main` first and restack `25/N`).
3. **Standalone PRs off `upstream/feature/batch-claim` (Already-Merged Batch SDK Commits):**
   - **`F-03`** (`BatchState.upsert_claim` `needs_delete=True` + `_watch_loop` / `resync_from_list` `_delete_claim` drain), **`F-08`** & **`F-15`** (`AsyncSandboxClient._stop_batches` checking `batch._release_started() and not batch._lease_released`, `close_connection` outside `_lock`, and `_lifecycle_lock` serialization after `_finish_detach` before `k8s_helper.close()`), **`F-09`** (`AsyncK8sHelper.watch_sandbox_claims` wrapping `func` passed to `Watch.stream` to check HTTP status and preserve `Retry-After` headers), **`F-12`** (`BatchState._time_out_undecided_groups` `_resolved.update`), **`F-13`** (`__all__` in `k8s_agent_sandbox/__init__.py`), **`F-16`** (sync `Watch.stop()` + `_watch_thread.join()` + async `contextlib.aclosing`), **`F-17`** (`connect` recreating closed cached `Sandbox` + `terminate()` Lease fencing), **`F-23a`** (`with batch._lock:` in `sandbox_client.py:593, 603`), **`F-24`** (draining in-flight dynamic `acquire()` HTTP `POST`s before `release()` / `detach()`), **`F-25`** (resolving popped pacing-slot items in `_drop_planned_creates`), **`F-26`** (waking `acquire()` pacing/backoff sleeps on `waiter` + Lease-clamping `_in_flight`), **`F-27`** (if not folded into `22/N`), and **`F-29`** (handling `ApiException(status=404)` TOCTOU on `replace_batch_lease` across `_take_batch_lease`, `_attach`, and `detach`).
4. **Standalone PR off `upstream/main`:**
   - **`F-22`** (`41` `mypy` static type errors across `10` modules in `examples/agent-sandbox-rl/agent_sandbox_rl` + CI wiring in `dev/tools/test-unit`).
