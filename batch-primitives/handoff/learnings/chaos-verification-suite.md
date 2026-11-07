<a id="sec-chaos-suite"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

# Automated batch verification and chaos suite

**How to read this page.** Before building the automated verification and chaos suite on `origin/feat/rl-batch-claim-mode` (`e84e964ce2d87987ec2443a5b5edccc84a374289`, tree `f798166c129e24f4f0ae59b842a0a32ce40fe177`), all `1,100` unit tests in our baseline sweep (`399` across the `5` `test_*batch*.py` SDK files + `701` across all `34` `examples/agent-sandbox-rl/tests/` files; `1,990` total Python unit tests when including all `1,289` tests across the `23` Python SDK unit files, with `18` in `test_batch_claims.py`) passed without error. Building a 10-stage deterministic verification pipeline (`dev/tools/run_batch_verification.sh`, `7,455`-line drop-in patch across `10` repository files plus `5` modular TLA+ specs) and driving it through `5` rounds of adversarial subagent audits surfaced `29` verified production bugs (`4 CRITICAL`, `9 HIGH`, `14 MEDIUM`, `2 LOW`, backed by `48` failing test reproductions, `21` static AST/call-graph violations, and `22` TLA+ model runs over `3,275,347` distinct states) alongside `18` subtle harness and fix-recipe defects that only appeared when we simulated applying our own fixes in `/tmp`:

- **Verification harness, TLA+, AST, and fuzzing architecture (`CHAOS-1` to `CHAOS-4`, `CHAOS-10`):** why negative-only reproductions lie until post-fix `/tmp` simulation passes, how to split subsystem TLA+ specs without exponential state blowup or inverted TLC flags, how to share one structural invariant oracle across Hypothesis and lockstep sync/async differential testing, and how to write non-brittle AST and import-binding checks.
- **Non-obvious distributed, concurrency, and lifecycle bugs caught (`CHAOS-5` to `CHAOS-9`):** subsystem authority separation (`watch` vs. `renew`), post-stop watch `ADDED` tombstoning (`needs_delete=False`) and popped pacing-slot stalls, dynamic `acquire()` escaping `release()`/`detach()` drain barriers, `get_batch()` Lease timeout and GC rollback traps, Lease `PUT` `404` TOCTOU, `concurrent.futures.Future` cancellation leaks, `.strftime` sub-second floor truncation, and `recycle.py` `-1` warm-pool scale-up underflow.

## 10-stage verification pipeline scoreboard (`e84e964ce2d87987ec2443a5b5edccc84a374289`)

| Stage | Pipeline component (`dev/tools/run_batch_verification.sh`) | Runtime | Coverage / scale | Unfixed `e84e964c` result |
| :-- | :-- | --: | :-- | :-- |
| **Stage 0** | Harness self-check (`Import & Mypy`) | `~6 s` warm (`39 s` cold) | Verifies `k8s_agent_sandbox` resolves inside `REPO_ROOT`; `mypy` across `6` SDK harness files, `1` RL test file, & `2` BFS scripts | **PASS** (`0` `mypy` errors) |
| **Stage 1** | Static AST & call-graph auditor (`dev/tools/verify_batch_static.py`) | `1 s` | `7` AST/call-graph checks across SDK + RL (`--fail-on LOW`) | **FAIL** (`21` real violations: `4 CRITICAL, 9 HIGH, 5 MEDIUM, 3 LOW`; `0` false positives) |
| **Stage 2** | Modular TLA+ formal suite (`handoff/work/tla/runall_subsystems.sh`) | `56 s` | `6` specs (`5` new subsystem + `BatchLease`), `22` TLC runs, `3,275,347` distinct states (`17,873,233` generated) | **PASS** (`8` positive pass + `14` `-workers 1` negative controls violate target invariants; `0` subsystem deadlocks) |
| **Stage 3** | Exhaustive BFS lock & cancel models (`verify_delete_lifecycle_model.py` + `verify_cancel5_exercise.py`) | `4 s` | `62,579` states across `15` sync/async lock/signal models (`7` positive + `8` negative) + `19` `CANCEL-5` states | **PASS** (`15/15` + `3/3` expected outcomes) |
| **Stage 4** | BSD/macOS non-listening socket portability (`test_k8s_helper.py`) | `2 s` | Deterministic BSD silent-`SYN`-drop socket simulation (`test_k8s_helper.py:1243`) | **FAIL** (`1` reproduction fails: `ConnectTimeoutError`, `F-02`) |
| **Stage 5a** | Pure SDK fault-injection suite (`test_batch_fault_injection.py`, `0` RL imports) | `7 s` | `36` targeted tests (`12`-cell fault matrix across sync & async SDK + `batch_reaper`) | **FAIL** (`23` reproductions fail across `14` findings, `13` guards pass) |
| **Stage 5b** | RL batch fault-injection suite (`examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py`) | `3 s` | `20` targeted RL batch, fleet, handle, preflight, reaper, & recycle tests | **FAIL** (`20` reproductions fail across `10` findings) |
| **Stage 6** | Stateful Hypothesis fuzzer (`test_batch_state_fuzz.py`, cold `-o cache_dir`) | `15 s` | `10,000`-step healthy + `6,000`-step reattached `RuleBasedStateMachine` + `BatchInvariantOracle` | **FAIL** (`2` healthy suites pass; shrinks `F-12` & `F-03` on cold cache) |
| **Stage 7** | Sync/async lockstep differential fuzzer (`test_batch_differential.py`) | `3 s` | `50` seeded lockstep traces + `CANCEL-5` interruption parity + group timeout trace | **FAIL** (`51` parity traces pass; `F-12` fails identically on sync & async) |
| **Stage 8** | Concurrency, Lease CAS, & lock-starvation stress suite (`test_batch_concurrency_stress.py`) | `4 s` | `50`-worker sync/async churn, `15`-round `9`-thread Lease CAS, creator flood, & `close()` lock hold | **FAIL** (`4` stress suites pass; `0.250 s` `AsyncSandboxClient.close()` lock starvation `F-15` fails) |
| **Stage 9** | Production `mypy` & PEP 561 `--strict` verification (`SDK + RL`) | `~8 s` warm (`32 s` cold) | SDK internal (`44` files), PEP 561 `--strict -c` external consumer, & RL `agent_sandbox_rl` (`29` files) | **FAIL** (`0` SDK internal errors; `3` PEP 561 `--strict` errors `F-13`; `41` RL errors across `10` files `F-22`) |

## 5-round adversarial convergence scoreboard

| Round | Independent subagent roles | Production findings (`F-01..F-29`) | What went wrong & got fixed before the next round |
| :-- | :-- | --: | :-- |
| **Initial build** | Main verification builder | `12` (`F-01`..`F-12`) | Negative-control-first harness built; tests verified to fail on unfixed `e84e964c`, but not yet tested against simulated production fixes, cold Hypothesis caches, or standalone SDK-only `pytest` envs. |
| **Pass 1** | Suite Auditor + SDK Core Auditor + RL Auditor | `23` (`+F-13`..`F-23`) | Caught TLC's inverted `-deadlock` flag disabling deadlock checks, cold Hypothesis cache missing `F-03`, shared venv editable `.pth` shadowing `REPO_ROOT`, proposed `_exit_detaches` `batch._lock` self-deadlock, and `F-05` attaching `add_done_callback` to `asyncio.Future` instead of `concurrent.futures.Future`. |
| **Pass 2** | `/tmp` Fix Simulator + Un-Anchored Code Hunter | `28` (`+F-24`..`F-28`) | Simulating fixes in `/tmp` exposed `3` tests (`F-08, F-09, F-25`) and `3` brittle string-matching AST rules that stayed red *after* fixing production code, `142` `mypy` errors inside excluded `test/` files, SDK->RL package boundary leakage, `F-21`'s `BatchError` contract nuance, and `F-27`'s `delete_batch_lease` GC hazard on `get_batch()`. |
| **Pass 3** | `/tmp` Fix Simulator + Un-Anchored Code Hunter | `29` (`+F-29`) | Caught `F-08` `atexit` hanging on `gke-gcloud-auth-plugin` at interpreter exit when `_active_batches` retained a failed batch, proposed `_stop_batches` `_lifecycle_lock` self-deadlock, `cleanup=False` regression in `F-08`, `F-11a` swallowing `first_exc`, and `404` TOCTOU on `replace_batch_lease` (`F-29`). |
| **Pass 4** | `/tmp` Fix Simulator + Hunter + Prose/AST Fact-Checker | `29` (stabilized) | Caught `F-29` test mutating a shared `unheld` `V1Lease` mock across two `get_batch` calls (`BatchInUseError` post-fix!), non-deterministic TLC state counts under `-workers auto`, and `4` memory-recalled symbol names in report prose (`reap_batches` vs `reap`, `CLOCK_SKEW_MARGIN`, `explicit=False`). |
| **Pass 5** | All `3` independent adversarial auditors in parallel | **`29` (`ZERO_FINDINGS` x3)** | Unanimous `ZERO_FINDINGS` across code coverage, `/tmp` post-fix simulation, cold-cache fuzzing, static AST rules, and line-by-line SHA/line-number audit. |

---

## Verification harness, TLA+, AST, and differential architecture

<a id="chaos-1"></a>

## CHAOS-1 A failing test on unfixed code proves only half the contract; simulate the production fix in `/tmp` before shipping the test

**Takeaway:** When building a negative-control or chaos reproduction suite without merging production fixes in the same commit, apply each proposed surgical fix to an isolated `/tmp` copy of the codebase and run both the new test and the existing unit suite. Six of our initial tests and static checks failed on unfixed code for the right surface reason, then stayed red or hung *after* the production bug was fixed.

<details>
<summary>The full story (Passes 2-4, test_batch_fault_injection.py & verify_batch_static.py)</summary>

- **Situation:** Workspace isolation rules forbade modifying production files while constructing the verification sweep. In the initial build, we proved every reproduction test failed against `e84e964c`, which satisfied the negative-control check ([TEST-1](testing.md#test-1)).
- **What goes wrong:** Proving a test fails before the fix does not prove the test *passes* after the fix, or that the proposed fix preserves existing callers:
  1. **`F-08` (`AsyncSandboxClient.__aexit__` retaining failed batches):** The test injected `ApiException(status=403)` on `delete_batch_lease` and set `count_live_sandbox_claims.side_effect = [1, 0]`. Because `403` is non-transient (`_is_transient_error(403) == False`), the failing `__aexit__` never consumed item `0` (`1`) of `side_effect`. Once `__aexit__` retained the failed batch, the subsequent `await client.delete_all()` consumed `1` instead of `0`, issuing an extra `delete_sandbox_claims` call (`AssertionError: 3 != 2`). Worse, leaving a mock-backed batch in `_active_batches` caused `SandboxClient._atexit_cleanup` to run at Python exit, construct a real `K8sHelper()`, and hang on `gke-gcloud-auth-plugin` ([TEST-27](testing.md#test-27)).
  2. **`F-29` (`replace_batch_lease` `404` TOCTOU):** The test set `helper.read_batch_lease.return_value = unheld` and called `get_batch("b1")` twice. On unfixed code, call 1 crashed with raw `ApiException(404)` so call 2 never executed. With `F-29` fixed, call 1 mutated `unheld.spec.holder_identity = client_holder` in place before `replace_batch_lease` raised `404`, so call 2 saw its own mutated `unheld` object and raised `BatchInUseError` instead of `BatchLeaseExpiredError`.
  3. **`F-09` (`_FakeResponse` in `test_batch_fault_injection.py`):** Our fake `aiohttp` response implemented `content.readline()` for `Watch.stream` but omitted `await resp.read()`, and our Pass-1 prose suggested inspecting `w.resp.status` *after* `async for event in w.stream(...)` exited, missing that `kubernetes_asyncio.watch.Watch.close()` sets `self.resp = None` inside `finally:` ([LIB-23](field-guide/lib.md#lib-23)).
  4. **Proposed self-deadlocks:** Pass-0 prose proposed taking `batch._lock` inside a helper already called under non-reentrant `with batch._lock:`, and Pass-2 prose proposed `async with batch._lifecycle_lock:` at the top of `_stop_batches()` around `await self._finish_detach(batch)` (which calls `await batch.detach()`, self-deadlocking `asyncio.Lock`).
- **What we do:** Have an adversarial reviewer copy the target tree to `/tmp`, apply the exact surgical fixes described in the report, and run `pytest` (`-o addopts=""` so `atexit` runs too), `verify_batch_static.py`, and the existing unit tests (`test_sandbox_batch.py`, `test_async_sandbox_batch.py`, `test_batch_claims.py`) before declaring any finding or test done.
- **Example:**
  ```text
  Negative-only check:  unfixed code -> FAIL (looks good, hides broken post-fix assertions)
  Two-sided check:      unfixed code -> FAIL  AND  /tmp patched code -> PASS (0 regressions, clean exit)
  ```
- **Related:** [TEST-1](testing.md#test-1), [TEST-27](testing.md#test-27), [LIB-23](field-guide/lib.md#lib-23), [AGENT-6](process.md#agent-6).

</details>

<a id="chaos-2"></a>

## CHAOS-2 Decompose TLA+ models by subsystem instead of widening one global spec, and mind TLC's `-deadlock`, `-workers 1`, and `-coverage 1` flags

**Takeaway:** Keep cross-process protocol fencing in `BatchLease.tla` and model intra-process subsystems in focused specs (`BatchHandle`, `BatchWatch`, `BatchConsumer`, `BatchAcquireRelease`, `RLBatchLifecycle`). In TLC CLI invocations, omit `-deadlock` whenever you want deadlock checking enabled, pin `-workers 1` on negative controls for deterministic state counts, and run `-coverage 1` to prove zero Next-state actions have `0` hits.

<details>
<summary>The full story (Stage 2, handoff/work/tla/*.tla, run.sh & runall_subsystems.sh)</summary>

- **Situation:** `BatchLease.tla` ([TLA-1](tla.md#tla-1)) already explored `663K` + `486K` distinct states for cross-process Lease ownership, reaper confirmation, and takeover fencing (`MaxTime = 14`). Adding watch paging, consumer modes (`quorum` / `outcomes` / `events`), pacing slots, and RL handle pooling into `BatchLease.tla` would multiply state spaces past `10^9` states.
- **What goes wrong:** Beyond state explosion, three TLC CLI sharp edges bit our first drafts in Pass 1 and Pass 4:
  1. **Inverted `-deadlock` flag:** Passing `-deadlock` on the `tlc2.TLC` command line *disables* deadlock checking (TLC checks deadlocks by default; only finite-clock `BatchLease.tla` with `MaxTime = 14` should pass `-deadlock`). Passing `-deadlock` unconditionally in `run.sh` silently disabled deadlock detection on every non-bounded subsystem spec.
  2. **Parallel BFS state-count jitter:** Under `-workers auto`, parallel BFS workers race to reach invariant counterexamples at slightly different queue depths, causing negative-control state counts to vary across runs.
  3. **Vacuously satisfied invariants:** A typo in an action guard leaves an invariant green because the action never fires (`0` distinct states added).
- **What we do:** `handoff/work/tla/runall_subsystems.sh` runs `22` TLC configurations (`8` positive specs exploring `3,272,848` distinct states + `14` `-workers 1` negative controls exploring `2,499` distinct states = `3,275,347` distinct states in `56 s`, with `0` subsystem deadlocks and `100%` `-coverage 1` action reachability):
  - `BatchHandle.tla`: watch vs. renew authority separation (`ActiveWorkloadProtected`, `AuthoritySeparation`, `CleanMemberReleaseAllowed`, `CachedLeaseRvConsistent`, `ExpiredImpliesDegraded`; reproduces `F-01` in `74` and `11` states).
  - `BatchWatch.tla`: watch stream + `410 Gone` multi-page relist + `_connected` cache (`NoStaleEndpointCached`, `ReleasedOrLostEvicted`, `ReleasedNeverResurrected`, `NoFalseLostOnRelist`, `NoOrphanedLiveReleasedClaim`; `228,680` distinct states; reproduces `F-03` in `20` states).
  - `BatchConsumer.tla`: `BatchState` consumer modes, dynamic ordinals, and popped pacing slots (`ResolvedPartitionAccounting`, `NoFailedGroupMemberDispatched`, `MonotonicUniqueOrdinals`, `NoStrandedWaitingAfterOutcome`, `ReleasedPoolImmediatelySettled`; `1,884,448` distinct states; reproduces `F-12` in `77` states and `F-25` in `33` states).
  - `BatchAcquireRelease.tla`: concurrent initial fill + dynamic `acquire()` + `release_*` + drain barrier (`NoForeignGroupSurvival`, `NoPendingDeleteLost`, `DispatchedProtectedFromReleaseNotReady`, `NoOrphanedClaimAfterBatchRelease`; reproduces `F-24` in `88` states).
  - `RLBatchLifecycle.tla`: `BatchClaims` `_batch_for` race + `release_all` (`ClosedPreventsLiveInstall`, `NoOrphanedRenewingBatch`; reproduces `F-07a` in `16` states and `F-07b` in `14` states).
- **Example:**
  ```bash
  # Subsystem specs run with deadlock checking ON; negative controls pin -workers 1.
  TLC_WORKERS=1 ./handoff/work/tla/run.sh BatchHandle.tla bh_neg_janet.cfg
  ```
- **Related:** [TLA-1](tla.md#tla-1), [TLA-4](tla.md#tla-4), [VER-6](verification.md#ver-6), [VER-12](verification.md#ver-12).

</details>

<a id="chaos-3"></a>

## CHAOS-3 Share one Sans-I/O structural invariant oracle across Hypothesis fuzzing, lockstep sync/async differential tests, and chaos suites

**Takeaway:** Encode structural state invariants once in a reusable `BatchInvariantOracle` (`batch_invariant_oracle.py`) and assert it after *every* step of stateful Hypothesis fuzzing (`test_batch_state_fuzz.py`) and sync/async lockstep differential execution (`test_batch_differential.py`). Pair broad `RuleBasedStateMachine` exploration with focused `@given` rules so cold Hypothesis caches still find narrow multi-step races deterministically.

<details>
<summary>The full story (Stages 6-7, batch_invariant_oracle.py, test_batch_state_fuzz.py, test_batch_differential.py)</summary>

- **Situation:** `BatchState` (`batch_state.py`), `SandboxBatch` (`sandbox_batch.py`), and `AsyncSandboxBatch` (`async_sandbox_batch.py`) maintain tightly coupled partition sets and counters (`_waiting`, `_resolved`, `_released`, `_pending_ready`, `_connected`, `_active_deletes`, `_acquiring`, `_acquire_waiters`). Point assertions in unit tests check return values, leaving internal partition drift undetected.
- **What goes wrong:** Two distinct issues surfaced:
  1. **`F-12` (`INV-ACCT-1` / `ResolvedPartitionAccounting`):** In `GROUP` (`group_outcomes`) mode, `BatchState._time_out_undecided_groups()` (`batch_state.py:708-714`) cleared `self._waiting[pool]` on timeout without calling `self._resolved.update(self._waiting[pool])`, immediately breaking `_resolved` partition accounting on both sync and async shells.
  2. **Cold-cache blind spot on `F-03`:** In Pass 1, our Hypothesis `RuleBasedStateMachine` hit `F-03` (`create_claim -> release_not_ready -> late watch ADDED`) immediately when `.hypothesis/examples` was warm, but missed that exact 3-step permutation on a cold `-o cache_dir=/tmp/...` run because unrelated rules diluted transition probability ([TEST-28](testing.md#test-28)).
- **What we do:**
  - `batch_invariant_oracle.py` enforces core partition, terminal monotonicity, ordinal/seq monotonicity, and shell connection/waiter invariants after every state transition.
  - `test_batch_differential.py` drives `50+` seeded lockstep traces and `CANCEL-5` interruption scripts against `SandboxBatch` and `AsyncSandboxBatch` side by side, comparing normalized oracle snapshots, return values, raised exception types, and K8s RPC counts at every step.
  - `test_batch_state_fuzz.py` runs `10,000`-step `TestHealthyCoreBatchStateFuzz` and `6,000`-step `TestReattachedBatchStateFuzz` state machines plus focused `@given` late-watch properties (`test_late_watch_added`), shrinking `F-12` and `F-03` deterministically on a cold cache directory.
- **Related:** [VER-8](verification.md#ver-8), [TEST-3](testing.md#test-3), [TEST-24](testing.md#test-24), [TEST-28](testing.md#test-28).

</details>

<a id="chaos-4"></a>

## CHAOS-4 Write control-flow-aware AST and call-graph checks rather than string matchers, and test downstream `--strict` PEP 561 consumers

**Takeaway:** Static AST auditors (`dev/tools/verify_batch_static.py`) catch whole classes of authority, lock, timeout, rollback, and typing bugs in `1 s` (`21` real violations, `0` false positives), provided each check inspects AST control-flow structure and transitive call graphs rather than `ast.unparse()` substrings or private helper names. Also run `mypy --strict -c` as an external consumer whenever a package ships `py.typed`.

<details>
<summary>The full story (Stages 1 & 9, dev/tools/verify_batch_static.py, F-13, F-15 & F-22)</summary>

- **Situation:** [VER-10](verification.md#ver-10) noted that `23/N`'s AST scan for `await` under `_lock`/`_changed` ran once by hand and could miss `await`s hidden inside helper calls. Meanwhile, `k8s_agent_sandbox` ships `py.typed` (PEP 561), promising downstream `--strict` callers clean imports even though internal `mypy` does not set `no_implicit_reexport = true`.
- **What goes wrong:**
  1. **Syntactic brittleness in Pass 1 AST rules:** Three initial rules matched surface syntax (for example, checking literal substrings in `ast.unparse(func)`, requiring `claim_batch` inside `try:` regardless of prior allocations, or requiring 1:1 private `_...` helper names across sync and async), causing false positives when `/tmp` fixes refactored private helpers.
  2. **Hidden `await` under `AsyncSandboxClient._lock` (`F-15`):** `AsyncSandboxClient.close()` (`async_sandbox_client.py:255-264`) held `self._lock` across `await sandbox.close_connection()` (starving concurrent callers for `0.250 s` in Stage 8's stress test) and closed `k8s_helper` without serializing on `batch._lifecycle_lock`, crashing an in-flight `await batch.release()`.
  3. **PEP 561 `--strict` export rejection (`F-13`):** `k8s_agent_sandbox/__init__.py:15-71` imported public batch classes without declaring `__all__`, causing `mypy --strict -c 'from k8s_agent_sandbox import SandboxBatch, AsyncSandboxBatch, BatchGroup'` to fail with `3` `[attr-defined]` errors (`no_implicit_reexport`), while `batch_claims.py:155` returned untyped `Any` under `--strict`.
- **What we do:** `dev/tools/verify_batch_static.py` runs `7` structural AST and transitive call-graph checks (`check_shared_authority_isolation`, `check_async_await_under_lock`, `check_lock_hierarchy_and_guards`, `check_k8s_timeout_and_client`, `check_base_exception_rollback`, `check_sync_async_ast_parity`, `check_pep561_exports`) plus optional `--with-mypy` across SDK and `agent_sandbox_rl`.
- **Related:** [VER-10](verification.md#ver-10), [VER-13](verification.md#ver-13), [TEST-25](testing.md#test-25), [CANCEL-5](field-guide/cancel.md#cancel-5).

</details>

---

## Distributed, concurrency, and lifecycle bugs caught by the sweep

<a id="chaos-5"></a>

## CHAOS-5 Separate watch-stream authority from Lease-renewal and delete authority

**Takeaway:** A fatal watch error (`403`/`404`/cert error on `watch`) means the handle is *blind* to cluster updates, not that it lost Lease ownership. Never write fatal watch errors into a shared error channel (`_state.note_error(e)`) that halts `_renew_loop`, discards renewed Lease `resourceVersion`s, suppresses `lease_degraded()`, or blocks `release_member()` / `release_group()` (`F-01`, `22/N` review).

<details>
<summary>The full story (F-01, BatchHandle.tla & test_batch_fault_injection.py)</summary>

- **Situation:** `SandboxBatch` (`sandbox_batch.py:491,506,1640,1654,1738,1745`) and `AsyncSandboxBatch` (`async_sandbox_batch.py:508,524,1625,1636,1720`) run `_watch_loop` (observing `SandboxClaim` status) alongside `_renew_loop` (renewing the `coordination.k8s.io/v1` Lease every `D/3`).
- **What goes wrong:** When `_watch_loop` hit a non-transient error (`403 Forbidden`), it called `_state.note_error(e)`. Because `_renew_loop`, `lease_degraded()`, `release_member()`, and `release_group()` read that same `_state.error`, a watch `403` immediately stopped Lease renewal while sandboxes were actively running user workloads, suppressed `lease_degraded()`, and blocked clean member/group release. Once `D` (`60 s`) elapsed, `batch_reaper` deleted the unrenewed Lease and Kubernetes GC terminated active pods underneath the caller. `BatchHandle.tla` reproduced both halves deterministically (`bh_neg_janet.cfg` violates `ActiveWorkloadProtected` across `74` states; `bh_neg_janet_release.cfg` violates `CleanMemberReleaseAllowed` across `11` states).
- **What we do:** Separate `self._lease_error` (Lease loss / expiry authority) from watch-stream errors (`_state.note_error` for readiness waiters). Keep `_renew_loop` renewing the Lease until explicit `release()`, `detach()`, or Lease loss (`404`/`409`/UID mismatch/expiry), allow `release_member()` and `release_group()` when only the watch failed, and have `_stop_renewal` signal `_watch_stop` / cancel `_watch_task` on true Lease loss.
- **Example:**
  ```text
  Coupled _state.error: watch 403 -> _renew_loop stops + release_member blocked -> Lease expires -> GC kills active pods
  Split _lease_error:   watch 403 -> readiness waiters wake -> _renew_loop keeps Lease alive + release_* still works
  ```
- **Related:** [LEASE-4](field-guide/lease.md#lease-4), [LEASE-15](field-guide/lease.md#lease-15), [CHAOS-2](#chaos-2).

</details>

<a id="chaos-6"></a>

## CHAOS-6 Pass `needs_delete=True` when late watch `ADDED` events arrive after `release_not_ready()`/`release_group()`, and resolve popped pacing-slot items

**Takeaway:** Stopping unready creates (`release_not_ready()`, `release_group()`) has two asynchronous tails: a `create_sandbox_claim` `POST` that already left the client can arrive via watch `ADDED` (or `410` relist) *after* the pool/batch stopped creating (`F-03`), and up to `max_in_flight=20` `_create_worker` threads/tasks that already popped `(claim_name, group)` from `_create_plan` can sit sleeping on staggered `create_rps` slots (`F-25`).

<details>
<summary>The full story (F-03 & F-25, BatchWatch.tla, BatchConsumer.tla, test_batch_fault_injection.py)</summary>

- **Situation:** `_create_worker` pops `(claim_name, group)` from `_create_plan` (`sandbox_batch.py:1787-1801`, `async_sandbox_batch.py:1769-1783`) before sleeping on `create_rps` pacing slots, while `BatchState.upsert_claim` (`batch_state.py:310-314, 357`) reconciles incoming watch `ADDED` and relist snapshots against stopped groups.
- **What goes wrong:**
  1. **Silent tombstoning without K8s `DELETE` (`F-03`):** When a late watch `ADDED` (or `410` relist) observed a live claim after `release_not_ready()` or `release_group()`, `BatchState.upsert_claim` called `self.release_claim(claim_name, member.warmpool)` with default `needs_delete=False`. That added `claim_name` to `_released` (hiding it forever from future relists!) while issuing **`0` Kubernetes `DELETE` calls**, permanently leaking the live `SandboxClaim` until full batch teardown.
  2. **Popped pacing-slot consumer stall (`F-25`):** Up to `max_in_flight=20` workers pop `(claim_name, group)` from `_create_plan` *before* sleeping on staggered `create_rps` pacing slots. Calling `release_group()` or `release_not_ready()` invokes `_drop_planned_creates` (`sandbox_batch.py:1220-1238`, `async_sandbox_batch.py:1253-1270`), which drops only *unpopped* items still sitting in `_create_plan` and misses the up-to-`20` popped items sleeping in workers, stalling `events()`, `group_outcomes()`, and `wait_for_quorum()`.
- **What we do:** Pass `needs_delete=True` when `BatchState.upsert_claim` tombstones a newly observed live claim for a stopped batch/group and drain those `_delete_claim` calls in `_watch_loop` / `resync_from_list` (`F-03`); and track/resolve popped pacing-slot items in `_drop_planned_creates` while waking sleeping workers on group stop (`F-25`, `F-26`).
- **Related:** [RACE-1](field-guide/race.md#race-1), [RACE-3](field-guide/race.md#race-3), [RETRY-15](field-guide/retry.md#retry-15), [RETRY-16](field-guide/retry.md#retry-16).

</details>

<a id="chaos-7"></a>

## CHAOS-7 Drain caller-thread `acquire()` `POST`s before `release()`/`detach()`, clamp `get_batch()` Lease `PUT`s, and never delete the Lease on `get_batch()` rollback

**Takeaway:** Dynamic `acquire()` dispatches `create_sandbox_claim` outside `_creators` and `_active_deletes`, so teardown must drain in-flight `acquire()` `POST`s before `deletecollection` and `_detached = True` (`F-24`). On `get_batch()` (`_attach`), clamp the takeover Lease `PUT` to `lease_duration` and run `check_pool_size`, and if attach rollback is ever needed, relinquish `holderIdentity = ""` rather than calling `delete_batch_lease` (`F-27`).

<details>
<summary>The full story (F-24 & F-27, BatchAcquireRelease.tla, sandbox_batch.py & async_sandbox_batch.py)</summary>

- **Situation:** `release()` and `detach()` join `_creators` and wait for `_active_deletes` before deleting claims or handing off the Lease (`15/N`, [VER-3](verification.md#ver-3)). Meanwhile, `get_batch()` (`sandbox_batch.py:414,442-466`, `async_sandbox_batch.py:411,439-463`) takes over an existing batch's Lease via `replace_batch_lease`.
- **What goes wrong:**
  1. **In-flight `acquire()` vs. `release()` orphan (`F-24`):** Dynamic `acquire()` runs on the caller's thread/task (`sandbox_batch.py:864,887-918`, `async_sandbox_batch.py:873,896-927`) outside `self._creators` and `_active_deletes`. When `release()` runs concurrently while `acquire()` is mid-`POST`, `release()` completes `deletecollection` and sets `_detached = True` *before* the `POST` returns; `_drop_acquired -> _delete_claim` then sees `_detached == True` and skips `delete_sandbox_claim`, leaking an orphaned `SandboxClaim` (`BatchAcquireRelease.tla` `bar_neg_inflight_release.cfg`, `88` states).
  2. **Unclamped `get_batch()` takeover & missing `check_pool_size` (`F-27`):** `_attach` sent `replace_batch_lease(..., _request_timeout=30)` unclamped by `lease_duration` (returning an already-expired handle when `lease_duration < 30 s` and the `PUT` stalled) and omitted `batch_utils.check_pool_size` (accepting `pool_size < 3` and leaving `max_in_flight=20`). Furthermore, during Pass-2 review we caught a lethal fix-recipe landmine: copying `_claim`'s `delete_batch_lease` setup-timeout rollback onto `_attach` would trigger Kubernetes `ownerReferences` GC and *destroy all live sandboxes of the existing batch*!
- **What we do:** Track and drain in-flight `acquire()` `POST`s alongside `_active_deletes` before `release()`/`detach()` runs `deletecollection` or sets `_detached = True` (`F-24`); clamp `_attach`'s `replace_batch_lease` timeout to `lease_duration`, verify `_lease_expires_at` after the `PUT`, call `batch_utils.check_pool_size`, and only ever roll back an attached Lease via `holderIdentity = ""` (`F-27`).
- **Related:** [LEASE-22](field-guide/lease.md#lease-22), [LEASE-33](field-guide/lease.md#lease-33), [CLEAN-1](field-guide/clean.md#clean-1), [VER-3](verification.md#ver-3).

</details>

<a id="chaos-8"></a>

## CHAOS-8 Handle `404 NotFound` alongside `409 Conflict` on every Lease `PUT`, and clamp `release_group` collection rounds to `_lease_expires_at`

**Takeaway:** Between `read_batch_lease` (`GET`) and `replace_batch_lease` (`PUT`), `batch_reaper` or a racing client can delete a Lease, turning an expected `409` optimistic-concurrency conflict into an HTTP `404 NotFound` (`F-29`). Likewise, `release_group()`'s multi-round `deletecollection` + paginated `count_live_sandbox_claims` loop must clamp both `_request_timeout` and list pagination `deadline` to `_lease_expires_at` (`F-04`).

<details>
<summary>The full story (F-29 & F-04, sandbox_batch.py, async_sandbox_batch.py, k8s_helper.py)</summary>

- **Situation:** `_take_batch_lease` (`release`), `_attach` (`get_batch`), and `detach` perform a `GET -> PUT` (`read_batch_lease -> replace_batch_lease`) cycle with `metadata.resourceVersion`, while `release_group()` calls `_delete_claims_in_rounds` (`sandbox_batch.py:1212-1218,2209-2259`, `async_sandbox_batch.py:1245-1251,2208-2258`).
- **What goes wrong:**
  1. **`GET -> PUT` `404` TOCTOU (`F-29`):** `_take_batch_lease`, `_attach`, and `detach` (`sandbox_batch.py:441-450,1518-1544,2137-2147`, `async_sandbox_batch.py:438-447,1520-1544,2137-2147`) caught only `status == 409`. When `batch_reaper` deleted the Lease between `GET` and `PUT`, raw `ApiException(status=404)` aborted `release()` *before* `deletecollection`, escaped `except BatchError:` in `get_batch()`, and left `_lease_released = False` in `detach()`.
  2. **Unclamped `release_group` delete/list rounds (`F-04`):** `release_group()` invoked `_delete_claims_in_rounds` with an unclamped `90.0 s` `_request_timeout` and called `count_live_sandbox_claims` (`k8s_helper.py:731-761`, `async_k8s_helper.py:709-737`), which forwarded `deadline=None` to `_claim_list_pages`, allowing both `DELETE` and paginated `LIST` calls to run past `_lease_expires_at` (`INV-OWN-3`).
- **What we do:** Handle `ApiException(status=404)` across `_take_batch_lease`, `_attach`, and `detach` (`F-29`), and thread a Lease-clamped `deadline` into `_delete_claims_in_rounds` and `count_live_sandbox_claims` (`F-04`).
- **Related:** [LEASE-2](field-guide/lease.md#lease-2), [LEASE-17](field-guide/lease.md#lease-17), [LEASE-29](field-guide/lease.md#lease-29), [K8S-1](field-guide/k8s.md#k8s-1).

</details>

<a id="chaos-9"></a>

## CHAOS-9 Audit RL orchestration layers (`agent-sandbox-rl`) for executor-future cancellation, `BaseException` rollbacks, retryable `_opened` tracking, `.strftime` truncation, and `held[img]` `-1` underflow

**Takeaway:** High-level RL orchestration modules (`async_fleet.py`, `fleet.py`, `batch_claims.py`, `utils.py`, `recycle.py`) introduce their own thread-pool, signal, TTL, and warm-pool scaling invariants. Six subtle bugs surfaced there during the sweep, including refinements to `CLEAN-24` and `LEASE-32`.

<details>
<summary>The full story (F-05, F-06, F-07, F-11, F-19a, F-28)</summary>

- **Situation:** `examples/agent-sandbox-rl` wraps `SandboxBatch` inside `SandboxFleet`, `AsyncSandboxFleet`, `BatchClaims`, and `recycle.py`.
- **What goes wrong:**
  1. **`asyncio.Future` vs. `concurrent.futures.Future` on cancel (`F-05`, `async_fleet.py:179-182`):** Cancelling `await AsyncSandboxFleet.acquire(...)` abandoned `_to_thread` (`self._thread_pool().submit`) while the worker thread kept running `self._sync.acquire(...)`, permanently leaking the eventual `SandboxHandle`, `BatchClaim`, and `_claims_reserved` slot. Note that attaching `add_done_callback` to the wrapped *`asyncio.Future`* fails to fix this because `asyncio.Future` resolves immediately with `CancelledError`; the cleanup callback must attach to the underlying `concurrent.futures.Future`!
  2. **Five `except Exception:` rollback gaps (`F-06`, `fleet.py:401-403,1473,1567,1737,1852,1869`):** `acquire`, `_acquire_reserved`, `release`, `resume`, and `restore` caught `except Exception:` instead of `BaseException`, leaking `_claims_reserved` slots, `BatchClaim`s, or dropping handles from `_handles` on `KeyboardInterrupt`.
  3. **Refining `CLEAN-24`: `_batch_for` race, `release_all` upfront clear, and `_teardown` early abort (`F-07` & `F-11`, `batch_claims.py:135-155,184-192`, `fleet.py:1875,1983-2015`):** `BatchClaims._batch_for` omitted a raced `SandboxBatch` from `_opened` when `_closed` won the race, `release_all()` cleared `_opened = []` upfront before `batch.release()`, and `SandboxFleet._teardown()` set `_torndown = True` before `release_all()` and aborted on handle `0`'s exception without sweeping handles `1..N-1` or warmpools/templates (and still must re-raise `first_exc` after completing the sweep to preserve `test_batch_claims.py:238`!).
  4. **Refining `LEASE-32`: `.strftime("%Y-%m-%dT%H:%M:%SZ")` sub-second floor truncation (`F-19a`, `utils.py:109,115`, `batch_claims.py:176`):** `construct_sandbox_claim_lifecycle_spec` formatted `shutdownTime` with `.strftime("%Y-%m-%dT%H:%M:%SZ")`, truncating up to `0.999999 s` off the Kubernetes `shutdownTime` so `BatchClaim.expires_at` (`now + ttl`) exceeded actual pod expiry by nearly a full second!
  5. **`recycle.py` `held[img]` `-1` underflow scales warm pools UP (`F-28`, `recycle.py:336-355,389-394,498-519,558-575`):** When a recyclable image rotated (`pos > 0`) under `scale_on_hold=True` and `reset.prime(handle)` failed before `held[img] += 1`, async `reuse_git_restore_sandbox_async` still ran `held[img] -= 1` on cleanup, underflowing `held[img]` from `0` to `-1` and scaling the warm pool to `1` dangling warm replica, while sync `reuse_git_restore_sandbox` leaked its `replicas_override=1` JIT warm replica!
- **Related:** [CLEAN-24](field-guide/clean.md#clean-24), [LEASE-32](field-guide/lease.md#lease-32), [CANCEL-1](field-guide/cancel.md#cancel-1), [CLEAN-1](field-guide/clean.md#clean-1).

</details>

<a id="chaos-10"></a>

## CHAOS-10 Self-check the verification runner's own import paths, package boundaries, and `test/` `mypy` coverage in Stage 0

**Takeaway:** A verification pipeline can silently test the wrong worktree, break CI's package isolation, or accumulate type errors inside excluded `test/` directories unless Stage 0 asserts `os.path.realpath(k8s_agent_sandbox.__file__).startswith(REPO_ROOT + os.sep)`, separates SDK unit tests from `examples/agent-sandbox-rl` tests, and runs `mypy` explicitly on the verification test modules.

<details>
<summary>The full story (Passes 1-2, dev/tools/run_batch_verification.sh)</summary>

- **Situation:** `dev/tools/run_batch_verification.sh` orchestrates all `10` stages (`Stage 0` to `Stage 9`) across `k8s_agent_sandbox`, `examples/agent-sandbox-rl`, and `handoff/work/`, writing caches to `/tmp` so `REPO_ROOT` stays completely clean.
- **What goes wrong:** Three harness-level traps surfaced in Passes 1 and 2:
  1. **Editable `.pth` shadowing:** A shared developer venv had an editable `.pth` pointing at an older scratch checkout, causing `import k8s_agent_sandbox` to load code outside `REPO_ROOT` until `PYTHONPATH` was prepended and verified in Stage 0.
  2. **SDK vs. `examples/agent-sandbox-rl` boundary:** `dev/tools/test-unit` installs only `clients/python/agentic-sandbox-client[test]` (and branches `22/N` to `24/N` predated `25/N`'s `batch_claims.py`). Having `test_batch_fault_injection.py` under `k8s_agent_sandbox/test/unit/` import `agent_sandbox_rl` broke standalone SDK unit runs.
  3. **`142` hidden `mypy` errors in test files:** Because `[tool.mypy]` in `pyproject.toml` excludes `test/` ([TEST-25](testing.md#test-25)), none of our new verification test files were type-checked until Auditor `4f0c076b` ran `mypy` directly on them in Pass 2 and uncovered `142` annotation issues.
- **What we do:** Stage 0 (`run_batch_verification.sh:144-170`) asserts `os.path.realpath(k8s_agent_sandbox.__file__).startswith(repo + os.sep)` and runs `mypy` across all `6` SDK verification modules, the RL fault-injection module, and both BFS model scripts (`0` errors); Stage 5 splits cleanly into Stage 5a (`test_batch_fault_injection.py`, `0` `agent_sandbox_rl` imports) and Stage 5b (`examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py`, auto-skipped when absent on pre-`25/N` branches).
- **Example:**
  ```python
  repo = os.path.realpath(sys.argv[1])
  mod = os.path.realpath(k8s_agent_sandbox.__file__)
  assert mod.startswith(repo + os.sep), f"ERROR: k8s_agent_sandbox resolved to {mod}, outside {repo}"
  ```
- **Related:** [TEST-25](testing.md#test-25), [BENCH-8](gke-benchmarking.md#bench-8).

</details>

<a id="chaos-11"></a>

## CHAOS-11 Verify suspect behaviors against intentional design contracts before filing (`7` audited non-bugs `C-01..C-07`)

**Takeaway:** Adversarial subagents rewarded for finding bugs will flag subtle, load-bearing optimizations and edge-case contracts as defects unless every candidate finding is cross-checked against existing tests, formal invariants, and design notes. Recording verified *non-bugs* explicitly (`C-01..C-07` in `VERIFICATION_SWEEP_RESULTS.md` Section 3.13) prevents well-meaning "fixes" from breaking production contracts.

<details>
<summary>The full story (Passes 2-4, VERIFICATION_SWEEP_RESULTS.md Section 3.13: C-01..C-07)</summary>

- **Situation:** Across `5` adversarial audit rounds, independent reviewers flagged dozens of candidate issues across `BatchState`, `AsyncSandboxBatch`, `AsyncK8sHelper`, `SandboxFleet`, `handles.py`, and `recycle.py`.
- **What goes wrong:** Accepting plausible-sounding bug reports without checking why the code was written that way introduces regressions:
  - Flagging `BatchState.fill_settled()` reopening when a missing claim on a re-attached batch (`get_batch`) arrives late (`C-01`) would drop the delayed claim's eventual `MEMBER_READY` / `MEMBER_FAILED` event (`test_batch_state.py:695-710`).
  - Flagging `SandboxHandle.is_alive()` calling `read_namespaced_pod_status` once `record_pod_identity()` sets `pod_uid` (`C-02`) ignores that `SandboxClaim` status lacks container restart counts needed to detect in-place OOM restarts (`WATCH-10`).
  - Flagging `SandboxFleet._teardown()` for re-raising `first_exc` after releasing handles (`C-04`) would break `test_batch_claims.py:238` (only aborting *early* on handle `0` before sweeping handles `1..N-1` and pools in `F-11a` was a bug); likewise, including `BATCH_SHUTDOWN_MARGIN_SECONDS` in `recycle._claim_expiring` (`C-03`, `LEASE-32`) and binding planned images to clusters in `SandboxFleet.plan()` (`C-05`) are deliberate tested contracts.
  - Flagging `AsyncK8sHelper._init_lock` and per-warmpool `refresh_lock` for `await`ing under a lock (`C-06`) confuses intentional single-flight I/O coalescing locks with shared state locks (`self._lock` / `self._changed`), while flagging `async with self._changed:` inside `wait_for_quorum` and `_drop_acquired` cancel handlers (`C-07`) overlooks that `INV-LOCK-ASYNC-1` guarantees zero `await`s inside `_changed`, keeping `asyncio.Lock.acquire()` synchronous (`verify_cancel5_exercise.py`: `19` states, `0` leaks).
- **What we do:** Require every candidate finding to pass a three-step filter before entering the defect catalog: (1) check git blame, PR notes, and existing unit tests for an explicit contract, (2) build a runnable reproduction or formal counterexample, and (3) document subtle intentional designs in a dedicated "Verified Intended Contracts (`NO Production Change`)" section so downstream builders never "fix" working invariants.
- **Related:** [AGENT-1](process.md#agent-1), [AGENT-6](process.md#agent-6), [VER-16](verification.md#ver-16), [WATCH-10](field-guide/watch.md#watch-10).

</details>

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
