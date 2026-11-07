<a id="sec-chaos-suite"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

# Automated batch verification and chaos suite

**How to read this page.** Before building the automated verification and chaos suite on `origin/feat/rl-batch-claim-mode` (`e84e964ce2d87987ec2443a5b5edccc84a374289`, tree `f798166c129e24f4f0ae59b842a0a32ce40fe177`), all `1,100` existing Python SDK and `agent-sandbox-rl` unit tests passed (`956` SDK + `144` RL). Building a 10-stage deterministic verification pipeline (`dev/tools/run_batch_verification.sh`) and driving it through `5` rounds of adversarial subagent audits surfaced `29` verified production bugs (`48` failing test reproductions, `21` static AST violations, `22` TLA+ model runs over `3,275,347` distinct states) and `18` subtle pipeline and fix-recipe defects that only appeared when we simulated applying our own fixes in `/tmp`:

- **Verification harness, TLA+, AST, and fuzzing architecture (`CHAOS-1` to `CHAOS-4`, `CHAOS-10`):** why negative-only reproductions lie until post-fix `/tmp` simulation passes, how to split subsystem TLA+ specs without exponential state blowup or inverted TLC flags, how to share one structural invariant oracle across Hypothesis and lockstep sync/async differential testing, and how to write non-brittle AST and import-binding checks.
- **Non-obvious distributed, concurrency, and lifecycle bugs caught (`CHAOS-5` to `CHAOS-9`):** subsystem authority separation (`watch` vs. `renew`), post-stop watch `ADDED` reconciliation and popped pacing-slot starvation, dynamic `acquire()` escaping `release()`/`detach()` drain barriers, `get_batch()` GC rollback traps, Lease `PUT` `404` TOCTOU, `concurrent.futures.Future` cancellation leaks, `.strftime` sub-second truncation, and `recycle.py` `-1` warm-pool scale-up underflow.

## 10-stage verification pipeline scoreboard (`e84e964ce2d87987ec2443a5b5edccc84a374289`)

| Stage | Pipeline component | Runtime | Coverage / scale | Unfixed `e84e964c` result |
| :-- | :-- | --: | :-- | :-- |
| **Stage 0** | Harness import-binding check + `mypy` across all `9` verification files | `~2 s` | `9` test & tool modules (`0` type errors) | **PASS** (`0` issues) |
| **Stage 1** | Cross-process `BatchLease.tla` (`handoff/work/tla/runall.sh`) | `~45 s` | `31` TLC configs (safety + negative controls) | **PASS** (`31/31` expected) |
| **Stage 2** | Modular subsystem TLA+ suite (`handoff/work/tla/runall_subsystems.sh`) | `~21 s` | `5` specs, `22` TLC configs, `3,275,347` distinct states | **PASS** (`7` positive + `15` negative controls) |
| **Stage 3** | In-process BFS lock/cancel checker (`verify_delete_lifecycle_model.py` + `verify_cancel5_exercise.py`) | `<1 s` | `15` runs (`7` positive + `8` negative controls; `763`-state `_stop_batches` model) | **PASS** (`15/15` expected) |
| **Stage 4** | Static AST, transitive call-graph, & `--strict` PEP 561 auditor (`dev/tools/verify_batch_static.py`) | `~1.8 s` | `8` structural checks across SDK + RL | **FAIL** (`21` real violations across `F-08, F-09, F-11, F-13, F-15, F-16, F-18`) |
| **Stage 5a** | SDK fault-injection & chaos suite (`test_batch_fault_injection.py`, `0` RL imports) | `~1.8 s` | `37` targeted tests (`12`-cell fault matrix + BSD socket simulation) | **FAIL** (`24` reproductions fail, `13` guards pass) |
| **Stage 5b** | RL fault-injection & lifecycle suite (`examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py`) | `~1.4 s` | `20` targeted RL batch, fleet, handle, & recycle tests | **FAIL** (`18` reproductions fail, `2` guards pass) |
| **Stage 6** | BSD/macOS silent-`SYN`-drop negative control (`test_k8s_helper.py`) | `~0.6 s` | Deterministic non-accepting socket drop simulation | **FAIL** (`1` reproduction fails: `ConnectTimeoutError`, `F-02`) |
| **Stage 7** | Stateful Hypothesis fuzzer (`test_batch_state_fuzz.py`, cold `-o cache_dir` verified) | `~2.4 s` | `15`-rule `RuleBasedStateMachine` + `BatchInvariantOracle` | **FAIL** (`3` minimal counterexamples: `F-12` sync/async + `F-03`) |
| **Stage 8** | Sync/async lockstep differential harness (`test_batch_sync_async_diff.py`) | `~0.6 s` | `10` deterministic traces comparing state snapshots, return/error parity, and RPC call counts | **FAIL** (`2` divergences fail: `F-08`, `F-09`; `8` parity traces pass) |

## 5-round adversarial convergence scoreboard

| Round | Independent subagent roles | Production findings (`F-01..F-29`) | What went wrong & got fixed before the next round |
| :-- | :-- | --: | :-- |
| **Initial build** | Main verification builder | `12` (`F-01`..`F-12`) | Negative-control-first harness built; tests verified to fail on unfixed `e84e964c`, but not yet tested against simulated production fixes, cold Hypothesis caches, or standalone SDK-only `pytest` envs. |
| **Pass 1** | Suite Auditor + SDK Core Auditor + RL Auditor | `23` (`+F-13`..`F-23`) | Caught TLC's inverted `-deadlock` flag disabling deadlock checks, cold Hypothesis cache missing `F-03`, shared venv editable `.pth` shadowing `REPO_ROOT`, proposed `_exit_detaches` `self._lock` self-deadlock, and `F-05` attaching `add_done_callback` to `asyncio.Future` instead of `concurrent.futures.Future`. |
| **Pass 2** | `/tmp` Fix Simulator + Un-Anchored Code Hunter | `28` (`+F-24`..`F-28`) | Simulating fixes in `/tmp` exposed `3` tests (`F-08, F-09, F-25`) and `3` brittle string-matching AST rules that stayed red *after* fixing production code, `142` `mypy` errors inside excluded `test/` files, SDK->RL package boundary leakage, `F-21a`'s backwards `BatchError` contract, and `F-27a`'s `delete_batch_lease` GC hazard on `get_batch()`. |
| **Pass 3** | `/tmp` Fix Simulator + Un-Anchored Code Hunter | `29` (`+F-29`) | Caught `F-08` `atexit` hanging on `gke-gcloud-auth-plugin` at interpreter exit when `_active_batches` retained a failed batch, proposed `_stop_batches` `_lifecycle_lock` self-deadlock, `cleanup=False` regression in `F-08`, `F-11a` swallowing `first_exc` (`test_batch_claims.py:238`), and `404` TOCTOU on `replace_batch_lease` (`F-29`). |
| **Pass 4** | `/tmp` Fix Simulator + Hunter + Prose/AST Fact-Checker | `29` (stabilized) | Caught `F-29` test mutating a shared `unheld` `V1Lease` mock across two `get_batch` calls (`BatchInUseError` post-fix!), non-deterministic TLC state counts under `-workers auto`, and `4` memory-recalled symbol names in report prose (`reap_batches` vs `reap`, `CLOCK_SKEW_MARGIN`, `explicit=False`). |
| **Pass 5** | All `3` independent adversarial auditors in parallel | **`29` (`ZERO_FINDINGS` x3)** | Unanimous `ZERO_FINDINGS` across code coverage, `/tmp` post-fix simulation (`62/62` passing), cold-cache fuzzing, static AST rules, and line-by-line SHA/line-number audit. |

---

## Verification harness, TLA+, AST, and differential architecture

<a id="chaos-1"></a>

## CHAOS-1 A failing test on unfixed code proves only half the contract; simulate the production fix in `/tmp` before shipping the test

**Takeaway:** When building a negative-control or chaos reproduction suite without merging production fixes in the same commit, apply each proposed surgical fix to an isolated `/tmp` copy of the codebase and run both the new test and the existing unit suite. Six of our initial tests and static checks failed on unfixed code for the right surface reason, then stayed red or hung *after* the production bug was fixed.

<details>
<summary>The full story (Passes 2-4, test_batch_fault_injection.py & verify_batch_static.py)</summary>

- **Situation:** Workspace isolation rules forbade modifying production files while constructing the verification sweep. In the initial build, we proved every reproduction test failed against `e84e964c`, which satisfied the negative-control check ([TEST-1](testing.md#test-1)).
- **What goes wrong:** Proving a test fails before the fix does not prove the test *passes* after the fix, or that the proposed fix preserves existing callers:
  1. **`F-08` (`test_async_client_aexit_retains_failed_batch_for_cleanup_retry`):** The test injected `ApiException(status=403)` on `delete_batch_lease` and set `count_live_sandbox_claims.side_effect = [1, 0]`. Because `403` is non-transient (`_is_transient_error(403) == False`), the failing `__aexit__` never consumed item `0` (`1`) of `side_effect`. Once `__aexit__` retained the failed batch, the subsequent `await client.delete_all()` consumed `1` instead of `0`, issuing an extra `delete_sandbox_claims` call (`AssertionError: 3 != 2`). Worse, leaving a mock-backed batch in `_active_batches` caused `SandboxClient._atexit_cleanup` to run at Python exit, construct a real `K8sHelper()`, and hang on `gke-gcloud-auth-plugin` ([TEST-27](testing.md#test-27)).
  2. **`F-29` (`test_lease_replace_404_toctou_in_release_get_batch_and_detach`):** The test set `helper.read_batch_lease.return_value = unheld` and called `get_batch("b1")` twice. On unfixed code, call 1 crashed with raw `ApiException(404)` so call 2 never executed. With `F-29` fixed, call 1 mutated `unheld.spec.holder_identity = client_holder` in place before `replace_batch_lease` raised `404`, so call 2 saw its own mutated `unheld` object and raised `BatchInUseError` instead of `BatchLeaseExpiredError`.
  3. **`F-09` (`_FakeResponse` in `test_batch_fault_injection.py` & `test_batch_sync_async_diff.py`):** Our fake `aiohttp` response implemented `content.readline()` for `Watch.stream` but omitted `await resp.read()`, and our Pass-1 prose suggested inspecting `w.resp.status` *after* `async for event in w.stream(...)` exited, missing that `kubernetes_asyncio.watch.Watch.close()` sets `self.resp = None` inside `finally:` ([LIB-23](field-guide/lib.md#lib-23)).
  4. **Proposed self-deadlocks:** Pass-0 prose proposed taking `self._lock` inside `_exit_detaches()` (already called under non-reentrant `with batch._lock:` at `sandbox_client.py:719`), and Pass-2 prose proposed `async with batch._lifecycle_lock:` at the top of `_stop_batches()` around `await self._finish_detach(batch)` (which calls `await batch.detach()`, self-deadlocking `asyncio.Lock`).
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

**Takeaway:** Keep cross-process protocol fencing in one spec (`BatchLease.tla`) and model intra-process subsystems in focused specs (`BatchHandle`, `BatchWatch`, `BatchConsumer`, `BatchAcquireRelease`, `RLBatchLifecycle`) of `4` to `7` variables each. In TLC CLI invocations, omit `-deadlock` whenever you want deadlock checking enabled, pin `-workers 1` on negative controls for deterministic state counts, and run `-coverage 1` once to prove zero Next-state actions have `0` hits.

<details>
<summary>The full story (Stage 2, handoff/work/tla/*.tla & run.sh)</summary>

- **Situation:** `BatchLease.tla` ([TLA-1](tla.md#tla-1)) already explored `~890K` states for cross-process Lease ownership, reaper confirmation, and takeover fencing. Adding watch paging, consumer modes (`quorum` / `outcomes` / `events`), pacing slots, and RL handle pooling into `BatchLease.tla` would multiply state spaces past `10^9` states.
- **What goes wrong:** Beyond state explosion, three TLC CLI sharp edges bit our first drafts in Pass 1 and Pass 4:
  1. **Inverted `-deadlock` flag:** Passing `-deadlock` on the `tlc2.TLC` command line *disables* deadlock checking (TLC checks deadlocks by default). A spec script that passes `-deadlock` thinking it turns deadlock detection on silently suppresses terminal hangs.
  2. **Parallel BFS state-count jitter:** Under `-workers auto`, parallel BFS workers race to reach invariant counterexamples at slightly different queue depths, causing negative-control state counts and trace lengths to vary across runs.
  3. **Vacuously satisfied invariants:** A typo in an action guard leaves an invariant green because the action never fires (`0` distinct states added).
- **What we do:** Split the suite into `5` focused subsystem specs (`22` `.cfg` files total: `7` positive safety models + `15` bug-reproducing negative controls) checking `3,275,347` distinct states in `~21 s`:
  - `BatchHandle.tla`: watch vs. renew authority separation (`ActiveWorkloadProtected`, `NoOrphanedActiveMembers`, reproducing `F-01` in `6` states).
  - `BatchWatch.tla`: watch stream + `410 Gone` multi-page relist + `_connected` cache (`TerminalMonotonicity`, `NoPostStopOrphan`, `NoStaleConnection`, `CompleteRelistBeforeDrop`).
  - `BatchConsumer.tla`: `BatchState` consumer modes, dynamic ordinals, and popped pacing slots (`WaitingNonNegative`, `QuorumSoundness`, `OutcomesPartition`, `FillProgressOrTerminal`).
  - `BatchAcquireRelease.tla`: concurrent initial fill + dynamic `acquire()` + `release_*` + drain barrier (`SeqMonotonicity`, `PoolCheckConsistency`, `NoPostReleaseLiveMember`).
  - `RLBatchLifecycle.tla`: `BatchClaims` handle pool + `release_group` retry rounds + `_teardown` (`NoUndrainedBatchDrop`, `CompleteGroupTeardown`, `NoPostExpiryDelete`).
  `handoff/work/tla/run.sh` enables TLC deadlock checking by default (passing `-deadlock` only when `CHECK_DEADLOCK=0` is set explicitly) and accepts `TLC_WORKERS=1` for deterministic negative-control traces; `-coverage 1` verified non-zero action firing across every positive spec.
- **Example:**
  ```bash
  # Default: TLC checks deadlocks; TLC_WORKERS=1 gives deterministic negative-control traces.
  TLC_WORKERS=1 bash handoff/work/tla/run.sh BatchHandle.tla bh_neg_janet.cfg
  ```
- **Related:** [TLA-1](tla.md#tla-1), [TLA-4](tla.md#tla-4), [VER-6](verification.md#ver-6), [VER-12](verification.md#ver-12).

</details>

<a id="chaos-3"></a>

## CHAOS-3 Share one Sans-I/O structural invariant oracle across Hypothesis fuzzing, lockstep sync/async differential tests, and chaos suites

**Takeaway:** Encode structural state invariants once in a reusable `BatchInvariantOracle` (`check_core`, `check_sync_shell`, `check_async_shell`, `snapshot`) and assert it after *every* step of stateful Hypothesis fuzzing and sync/async lockstep execution. Pair broad `RuleBasedStateMachine` exploration with targeted multi-step property rules so cold Hypothesis caches still find narrow race sequences deterministically.

<details>
<summary>The full story (Stages 7-8, batch_invariant_oracle.py, test_batch_state_fuzz.py, test_batch_sync_async_diff.py)</summary>

- **Situation:** `BatchState` (`batch_state.py`), `SandboxBatch` (`sandbox_batch.py`), and `AsyncSandboxBatch` (`async_sandbox_batch.py`) maintain tightly coupled counters and sets (`_waiting`, `_pending_ready`, `_outcomes_left`, `_fill_unresolved`, `_connected`, `_active_deletes`, `_acquiring`, `_acquire_waiters`). Point assertions in unit tests check return values, leaving internal counter drift undetected until a later consumer hangs.
- **What goes wrong:** Two distinct bugs hid behind green unit tests:
  1. **`F-12` (`INV-ACCT-4`):** Calling `release_Failed()` (`sandbox_batch.py:936` / `async_sandbox_batch.py:905`) after `group_outcomes()` started failed a `PENDING` member inside `mark_fill_failed` (`batch_state.py:567`), transitioning its group into `_outcomes` and decrementing `_outcomes_left` without decrementing `_waiting` (when `_waiting > 0` for another unresolved group). That violated `_waiting <= _outcomes_left` immediately.
  2. **Cold-cache blind spot on `F-03`:** In Pass 1, our `15`-rule Hypothesis `RuleBasedStateMachine` hit `F-03` (`create_claim -> release_not_ready -> late watch ADDED`) immediately when `.hypothesis/examples` was warm, but missed the exact 3-step sequence within `max_examples=200` on a cold `-o cache_dir=/tmp/...` run because 12 unrelated rules diluted transition probability ([TEST-28](testing.md#test-28)).
- **What we do:**
  - `batch_invariant_oracle.py` checks `8` core accounting/monotonicity invariants (`INV-ACCT-1..8`: terminal irreversibility, `0 <= _waiting <= _outcomes_left` when `_mode == "outcomes"`, `len(_ready_events)` vs. `READY` members, exact `_fill_unresolved` count, monotonic `_ordinal` and `member.seq`, `_stopping => _create_plan == []`) plus shell invariants (`_connected.keys() <= READY`, non-negative `_active_deletes`, `_detached => _lease_released` or in-flight exit, `_acquiring == _acquire_waiters.keys()`).
  - `test_batch_sync_async_diff.py` drives identical `Step` scripts against `SandboxBatch` and `AsyncSandboxBatch` in lockstep, comparing normalized `BatchInvariantOracle.snapshot(...)` dicts, return values, exception types, and mock K8s RPC counts after every step (catching `F-08` and `F-09` as immediate sync/async divergences).
  - `test_batch_state_fuzz.py` keeps the broad `15`-rule state machine (which shrinks `F-12` to a 2-step sequence from cold start) *and* adds focused Hypothesis `@given` properties for post-stop late watch events, reproducing both `F-12` and `F-03` with `100%` cold-cache determinism.
- **Example:**
  ```python
  # INV-ACCT-4 inside BatchInvariantOracle.check_core(state):
  if state._mode == "outcomes":
      assert 0 <= state._waiting <= state._outcomes_left
  ```
- **Related:** [VER-8](verification.md#ver-8), [TEST-3](testing.md#test-3), [TEST-24](testing.md#test-24), [TEST-28](testing.md#test-28).

</details>

<a id="chaos-4"></a>

## CHAOS-4 Write control-flow-aware AST and call-graph checks rather than string matchers, and test downstream `--strict` PEP 561 consumers

**Takeaway:** Static AST auditors (`dev/tools/verify_batch_static.py`) catch whole classes of lock, cancellation, and typing bugs in `<2 s`, provided each check inspects AST control-flow structure and transitive call graphs rather than `ast.unparse()` substrings or private helper names. Also run `mypy --strict` on a synthetic external consumer script once `py.typed` ships.

<details>
<summary>The full story (Stage 4, dev/tools/verify_batch_static.py, F-13 & F-15)</summary>

- **Situation:** [VER-10](verification.md#ver-10) noted that `23/N`'s AST check for `await` under `_lock`/`_changed` ran once by hand and could miss `await`s hidden inside helper methods. Meanwhile, `k8s_agent_sandbox` ships `py.typed` (PEP 561), promising downstream `--strict` callers clean type checking even though the repo's own `pyproject.toml` does not enable `disallow_any_generics = true` or `no_implicit_reexport = true`.
- **What goes wrong:**
  1. **Syntactic brittleness in Pass 1 AST rules:** Three initial checks in `verify_batch_static.py` matched surface syntax: Check 2 required `".status" not in ast.unparse(func)` inside `async_k8s_helper.py` (which rejected *any* fix that didn't literally spell `.status`), Check 5 required `claim_batch` to sit inside `try:` (false-positive when resources allocated *before* `claim_batch` wrap `claim_batch` cleanly), and Check 3 enforced 1:1 parity on private `_...` helper names (preventing sync or async from extracting a shell-specific helper).
  2. **Hidden PEP 561 `--strict` failures (`F-15`):** Running `mypy --strict` on a 15-line consumer importing `SandboxClient`, `SandboxBatch`, `BatchGroup`, and `BatchLeaseExpiredError` failed with `7` errors across `k8s_agent_sandbox` (`Callable` bare generics in `sandbox_batch.py:213`, `async_sandbox_batch.py:216`, `connector.py:92`, `trace_manager.py:163`, plus implicit re-exports in `__init__.py` missing `__all__`).
  3. **Unguarded signal-window allocation (`F-13`):** Control-flow inspection of `BatchClaims.open()` (`batch_claims.py:72`) caught `self._batches[group] = client.claim_batch(...)` executing *outside* `try: ... except BaseException:`, leaving an `SIGINT`/`KeyboardInterrupt` arriving on batch `k` leaking batches `0..k-1` when `open()` was called directly.
- **What we do:** `dev/tools/verify_batch_static.py` implements `8` structural checks:
  - Transitive intra-class call-graph walk proving zero non-`Condition.wait` `await` expressions are reachable under `self._lock` or `self._changed` (`Check 1`).
  - Pre-`Watch().stream` HTTP status validation or `_preload_content=True` check (`Check 2`).
  - Public method & signature parity across sync/async batch and client classes (`Check 3`).
  - Monotonic `time.monotonic()` enforcement (`0` `time.time()` calls in timeout/deadline logic, `Check 4`).
  - Multi-step resource acquisition rollback under `except BaseException:` by statement-order analysis (`Check 5`).
  - PEP 561 external `--strict` `mypy` consumer simulation (`Check 6`).
  - `_active_batches.discard` gated on cleanup outcome (`Check 7`).
  - Bare `asyncio.Lock()` construction compatibility across Python 3.11+ (`Check 8`).
- **Related:** [VER-10](verification.md#ver-10), [VER-13](verification.md#ver-13), [TEST-25](testing.md#test-25), [CANCEL-5](field-guide/cancel.md#cancel-5).

</details>

---

## Distributed, concurrency, and lifecycle bugs caught by the sweep

<a id="chaos-5"></a>

## CHAOS-5 Separate watch-stream authority from Lease-renewal and delete authority

**Takeaway:** A fatal watch error (`403 Forbidden` on `watch`, watch stream drop) means the handle is *blind* to cluster updates, not that it lost Lease ownership. Never couple `_renew_loop` or `release()`/`release_Failed()`/`delete_claim()` to a shared `_fatal` watch error flag while active sandboxes remain in use (`F-01`, PR #2188 review).

<details>
<summary>The full story (F-01, BatchHandle.tla & test_watch_fatal_error_does_not_halt_lease_renewal)</summary>

- **Situation:** `SandboxBatch` and `AsyncSandboxBatch` run two background loops: `_watch_loop` (observing `SandboxClaim` status) and `_renew_loop` (renewing the `coordination.k8s.io/v1` Lease every `D/3` so the reaper and `ownerReferences` GC keep live claims alive).
- **What goes wrong:** When `_watch_loop` hit a non-transient error (`403 Forbidden`), `_fail_fatal(err)` set `BatchState._error = err` (`sandbox_batch.py:1305`, `async_sandbox_batch.py:1244`). `_renew_once()` checked `if self._detached or self._state.error is not None: return False` (`sandbox_batch.py:1221`, `async_sandbox_batch.py:1174`) and exited immediately. Worse, `_check_healthy()` (`sandbox_batch.py:1066`) raised `self._state.error`, blocking `release_not_ready()`, `release_Failed()`, and `delete_claim()` too. Existing `READY` sandboxes kept running user workloads while the Lease silently expired (`D = 60 s`) and `batch_reaper` garbage-collected active pods underneath the caller. `BatchHandle.tla` (`bh_neg_janet.cfg` and `bh_neg_janet_release.cfg`) violated `ActiveWorkloadProtected` and `NoOrphanedActiveMembers` in `6` states (`State 5 -> State 6`).
- **What we do:** Split `_watch_error` (which fails observation waiters like `wait_for_quorum`, `group_outcomes`, `events`, and `acquire` waiting on readiness) from Lease authority (`_lease_error` / `_lease_released` / `_detached`). Keep `_renew_loop` renewing the Lease until explicit `release()`, `detach()`, or Lease loss (`404`/`409`/UID mismatch/expiry), and keep `release_*` and `delete_claim` unblocked by `_watch_error`.
- **Example:**
  ```text
  Coupled _error:  watch 403 -> _state.error set -> _renew_once() returns False -> Lease expires -> GC kills active pods
  Split authority: watch 403 -> _watch_error wakes waiters -> _renew_loop keeps renewing until caller calls release()/detach()
  ```
- **Related:** [LEASE-4](field-guide/lease.md#lease-4), [LEASE-15](field-guide/lease.md#lease-15), [CHAOS-2](#chaos-2).

</details>

<a id="chaos-6"></a>

## CHAOS-6 Reconcile late watch `ADDED` events after `stop_unready()` and avoid popping `_create_plan` slots before sleeping on `create_rps`

**Takeaway:** Stopping unready creates (`release_not_ready()`, `release_group()`) has two asynchronous tails: a `create_sandbox_claim` RPC that already left the client can surface via watch `ADDED` *after* `stop_unready()` cleared `_create_plan`, and `max_in_flight` workers that already popped items from `_create_plan` can sit sleeping on `create_rps` pacing slots for another slow group (`F-03`, `F-25`).

<details>
<summary>The full story (F-03 & F-25, BatchWatch.tla, BatchConsumer.tla, test_batch_fault_injection.py)</summary>

- **Situation:** `SandboxBatch` spawns `max_in_flight` (`20` default) create workers pulling `(ordinal, group)` pairs via `pop_create()` (`batch_state.py:254`) and pacing cold starts via `_pace_create(pool)` (`sandbox_batch.py:1141`, `async_sandbox_batch.py:1098`).
- **What goes wrong:**
  1. **Orphaned post-stop `ADDED` claims (`F-03`):** Worker T1 sends `POST /sandboxclaims/b-0`. Before T1 records the response or the watch delivers `ADDED`, the caller invokes `release_not_ready()`. `stop_unready()` marks known `PENDING` members `LOST` and clears `_create_plan`, returning `to_delete = []` because `b-0` is not in `_members` yet. When the watch subsequently delivers `ADDED(b-0, Ready=True)`, `BatchState.apply()` (`batch_state.py:322`) inserts `b-0` as `READY` even though `self._stopping` (or `group in _stopped_groups`) is already `True`, leaving `b-0` alive on the cluster until full batch teardown.
  2. **Popped-slot pacing starvation (`F-25`):** In a two-group batch (`pool-slow` with `0` warm credit at `create_rps=0.2`, followed by `pool-fast` with `10` warm credit), all `20` workers immediately pop `pool-slow` items from `_create_plan` and reserve staggered `5 s` pacing slots (`5 s, 10 s, ..., 100 s`). Two bugs compound: `pool-fast` sits starved in `_create_plan` behind `20` sleeping workers despite having warm credit ([RETRY-15](field-guide/retry.md#retry-15)), and calling `release_group("pool-slow")` clears only *unpopped* `_create_plan` entries while the `20` sleeping workers neither wake on `_changed` nor release their concurrency seats for up to `100 s`.
- **What we do:**
  - In `BatchState.apply()`, when a brand-new member arrives while `self._stopping` or `group in self._stopped_groups`, mark it `LOST` immediately and return a `needs_delete=True` signal so `_apply_event` / `_relist` schedules best-effort Lease-fenced `delete_sandbox_claim` (`F-03`).
  - Acquire the group's pacing slot *before* popping an item for that group (or wake pacing sleeps on group stop / shutdown events), so warm-credited groups never queue behind cold-paced groups and `release_group()` frees worker concurrency immediately (`F-25`).
- **Related:** [RACE-1](field-guide/race.md#race-1), [RACE-3](field-guide/race.md#race-3), [RETRY-15](field-guide/retry.md#retry-15), [RETRY-16](field-guide/retry.md#retry-16).

</details>

<a id="chaos-7"></a>

## CHAOS-7 Drain caller-thread `acquire()` `POST`s before `release()`/`detach()`, and never copy create-time Lease `DELETE` rollback onto `get_batch()`

**Takeaway:** Every RPC that mutates cluster state under a batch Lease must either join the teardown drain barrier (`_active_creates` alongside `_active_deletes`) or leave existing cluster state untouched on read-side attach failure (`F-24`, `F-27a`, `F-27b`).

<details>
<summary>The full story (F-24 & F-27, BatchAcquireRelease.tla, sandbox_batch.py & async_sandbox_batch.py)</summary>

- **Situation:** `release()` and `detach()` call `_wait_active_deletes()` (`sandbox_batch.py:986`, `async_sandbox_batch.py:953`) before deleting or handing off the Lease (`15/N`, [VER-3](verification.md#ver-3)). Meanwhile, `claim_batch()` (`_claim`) rolls back its newly created Lease via `delete_batch_lease` if `_start()` hits a setup deadline (`LEASE-33`), whereas `get_batch()` (`_attach`) attaches to an *existing* batch whose sandboxes are already running.
- **What goes wrong:**
  1. **In-flight `acquire()` escapes teardown (`F-24`):** Dynamic `acquire()` runs on the caller's thread/task, checks `_check_lease_held()` before `create_sandbox_claim`, and is not tracked by `_create_threads` or `_active_deletes`. If `release()` runs concurrently while `acquire()`'s `POST` is in flight, `release()` runs `deletecollection` + `delete_batch_lease` first, and the in-flight `POST` lands milliseconds later with `ownerReferences` pointing to a deleted Lease UID (either leaving a brief orphan until K8s GC sweeps it or racing `detach()` handoff after `holderIdentity` was cleared).
  2. **The `get_batch()` GC rollback trap (`F-27a`):** `_attach` (`sandbox_batch.py:423`, `async_sandbox_batch.py:412`) lacked `_claim`'s pre-`_start()` `_lease_expires_at` fence, allowing `_attach` to list claims and spawn loops on an already-expired Lease. When drafting the fix in Pass 2, copying `_claim`'s setup-timeout rollback (`delete_batch_lease`) onto `_attach` would have triggered Kubernetes `ownerReferences` garbage collection and *destroyed all live sandboxes of the pre-existing batch*! On `_attach` failure, rollback must *relinquish* `holderIdentity = ""` via `replace_batch_lease(resourceVersion)` (detach semantics), never `delete_batch_lease`.
  3. **Asymmetric `check_pool_size` on `get_batch()` (`F-27b`):** `SandboxBatch._attach` called `_precheck_pools()` (`explicit=False`, raising `SandboxTemplateNotFoundError` on deleted templates when `check_pool_size=True`), whereas `AsyncSandboxBatch._attach` skipped `_precheck_pools()` completely. On `get_batch()`, `check_pool_size` only needs warm-pool `status.readyReplicas` to seed warm credit, not template existence for already-created claims.
- **What we do:** Track caller-driven `acquire()` `POST`s in an `_active_creates` counter drained alongside `_active_deletes` before `release()`/`detach()` touches `deletecollection` or the Lease (`F-24`); fence `_attach` before `_start()` and relinquish `holderIdentity = ""` (never `DELETE`) if `_attach` aborts after taking the Lease (`F-27a`); and unify sync/async `_attach` warm-credit seeding without failing on deleted templates (`F-27b`).
- **Related:** [LEASE-22](field-guide/lease.md#lease-22), [LEASE-33](field-guide/lease.md#lease-33), [CLEAN-1](field-guide/clean.md#clean-1), [VER-3](verification.md#ver-3).

</details>

<a id="chaos-8"></a>

## CHAOS-8 Handle `404 NotFound` alongside `409 Conflict` on every Lease `PUT`, and fence `release_group` retry rounds against `_lease_expires_at`

**Takeaway:** Between `read_batch_lease` (`GET`) and `replace_batch_lease` (`PUT`), `batch_reaper` or `kubectl delete lease` can delete an expired Lease, turning an expected `409` optimistic-concurrency rejection into an HTTP `404 NotFound` (`F-29`). Likewise, multi-round `deletecollection` loops (`release_group`) must re-check `_lease_expires_at` before *every* retry sleep and RPC (`F-04`).

<details>
<summary>The full story (F-29 & F-04, sandbox_batch.py, async_sandbox_batch.py, RLBatchLifecycle.tla)</summary>

- **Situation:** `release()`, `detach()`, and `get_batch()` (`_attach`) perform a read-modify-write cycle on the batch `Lease`: `GET` the Lease, inspect `uid`, `holderIdentity`, and expiry, and then `PUT` (`replace_batch_lease`) with `metadata.resourceVersion` set.
- **What goes wrong:**
  1. **`GET -> PUT` `404` TOCTOU (`F-29`):** `_renew_once` (`sandbox_batch.py:1244`) already treated `status in (404, 409)` as Lease loss, and `release()` handled `404` on `delete_batch_lease`. However, three `replace_batch_lease` call sites caught only `status == 409`:
     - `release()`'s expired-Lease self-fencing `PUT` (`sandbox_batch.py:678`, `async_sandbox_batch.py:648`) leaked raw `ApiException(404)` instead of returning cleanly when the reaper deleted the expired Lease one millisecond earlier (`INV-OWN-4`).
     - `_attach()`'s takeover `PUT` (`sandbox_batch.py:420`, `async_sandbox_batch.py:409`) leaked raw `ApiException(404)` instead of `BatchNotFoundError` / `BatchLeaseExpiredError`.
     - `detach()`'s handoff `PUT` (`sandbox_batch.py:739`, `async_sandbox_batch.py:709`) leaked raw `ApiException(404)` without setting `_lease_released = True` and `_detached = True`.
  2. **Unfenced `release_group` collection rounds (`F-04`):** `_delete_group_claims` (`sandbox_batch.py:1022`, `async_sandbox_batch.py:988`) verified `_check_lease_held()` on entry and passed `_lease_timeout()` to each HTTP call, but looped across transient `503` retries using `deadline = time.monotonic() + _DELETE_COLLECTION_WAIT` (`30 s`) without clamping `deadline` to `self._lease_expires_at` or re-checking `_check_lease_held()` before subsequent `delete_sandbox_claims` calls (`INV-OWN-2`).
- **What we do:** Treat `404` on `replace_batch_lease` symmetrically with Lease disappearance in `release()` (idempotent success once claims are gone or GC-owned), `_attach()` (`BatchLeaseExpiredError` if the read Lease was already expired, else `BatchNotFoundError`), and `detach()` (mark `_lease_released = True` and raise `BatchLeaseExpiredError`); and clamp `_delete_group_claims`'s loop deadline to `min(now + _DELETE_COLLECTION_WAIT, self._lease_expires_at)` while calling `_check_lease_held()` at the top of each round.
- **Related:** [LEASE-2](field-guide/lease.md#lease-2), [LEASE-17](field-guide/lease.md#lease-17), [LEASE-29](field-guide/lease.md#lease-29), [K8S-1](field-guide/k8s.md#k8s-1).

</details>

<a id="chaos-9"></a>

## CHAOS-9 Audit orchestration layers (`agent-sandbox-rl`) for executor-future cancellation, retryable teardown, `.strftime` floor truncation, and pool-count underflow

**Takeaway:** High-level RL wrappers around `SandboxBatch` (`AsyncSandboxFleet`, `BatchClaims`, `SandboxClient.create_sandbox`, `recycle.py`) introduce their own concurrency and time boundaries. Five non-obvious bugs surfaced there during the sweep, including two subtleties inside patterns we had already documented (`CLEAN-24` and `LEASE-32`).

<details>
<summary>The full story (F-05, F-06, F-07, F-11, F-19a, F-28)</summary>

- **Situation:** `examples/agent-sandbox-rl` layers async thread-pool bridges (`async_fleet.py`), multi-pool batch pools (`batch_claims.py`), pod recycling (`recycle.py`), and `shutdownTime` formatting (`utils.py`) on top of the SDK.
- **What goes wrong:**
  1. **`asyncio.Future` vs. `concurrent.futures.Future` on cancel (`F-05`, `async_fleet.py:86`):** `await asyncio.wait_for(loop.run_in_executor(executor, sync_acquire), timeout)` cancels the `asyncio.Future` wrapper on timeout while the worker thread keeps running `sync_acquire()`. Attaching `add_done_callback` to the *`asyncio.Future`* fires immediately upon `cancel()` with `CancelledError` (never seeing the eventual `SandboxHandle`!). You must submit directly via `executor.submit(sync_acquire)`, wrap with `asyncio.wrap_future(cf_fut)`, and attach the `cancelled` Flag + `terminate()` callback to `cf_fut` (`concurrent.futures.Future`).
  2. **Refining `CLEAN-24`: `_opened` retryability & full-sweep `_teardown` (`F-07` & `F-11a`, `batch_claims.py`):** Setting `self._opened = True` *before* `open()` succeeds and clearing `self._batches.clear()` up front in `close()`/`_teardown()` meant a transient `503` during `release()` orphaned remaining batches *and* permanently bricked the `BatchClaims` instance (`BatchClaims is closed`). Keep failed batches in `self._batches` on `release()` error, finish sweeping every remaining batch in `_teardown` before re-raising `first_exc`, and only mark `_closed = True` once `self._batches` is empty.
  3. **Refining `LEASE-32`: `.strftime("%Y-%m-%dT%H:%M:%SZ")` sub-second floor truncation (`F-19a`, `utils.py:42`):** `format_shutdown_time(ttl)` computed `(now + timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")`, which *floors* fractional microseconds (`10:00:00.900Z + 60s -> "10:01:00Z"`, effective TTL `59.1 s`), while the batch Lease gets `leaseDurationSeconds = 60` (`60.0 s`). In that `0.9 s` tail window, the controller sets `Ready=False (SandboxExpired)` and deletes the backing `Sandbox` pod while the Lease is still valid! Round `shutdownTime` *up* to the next whole second whenever `microsecond > 0`.
  4. **`recycle.py` `held[img]` `-1` underflow scales warm pools UP (`F-28`, `recycle.py:216`):** When `retire_handle` hit `on_Dead` for a handle acquired *outside* `handles_by_image` (or retired twice), `held[img] -= 1` decremented `0` to `-1`. Then `target = held[-1] + need[0] = -1`, and Python's truthiness check `if target:` evaluated `bool(-1) == True`, patching the warm pool's `replicas` to `-1` (admission `422`) or miscomputing positive scale targets! Clamp `held[img] = max(0, held[img] - 1)` and check `if target > 0:`.
  5. **`SandboxClient.create_sandbox` partial-failure leak (`F-06`, `sandbox_client.py:432`):** When `wait_for_sandbox_ready` timed out or `connect_sandbox` failed after `create_claim()` succeeded, `create_sandbox` propagated the exception without calling `delete_sandbox_claim`, unlike `AsyncSandboxClient.create_sandbox` (`async_sandbox_client.py:239`), which already wrapped post-claim steps in `try: ... except BaseException: await self.delete_claim(...)`.
- **Related:** [CLEAN-24](field-guide/clean.md#clean-24), [LEASE-32](field-guide/lease.md#lease-32), [CANCEL-1](field-guide/cancel.md#cancel-1), [CLEAN-1](field-guide/clean.md#clean-1).

</details>

<a id="chaos-10"></a>

## CHAOS-10 Self-check the verification runner's own import paths, package boundaries, and `test/` `mypy` coverage in Stage 0

**Takeaway:** A verification pipeline can silently test the wrong checkout, break CI's package isolation, or rot under excluded `test/` directories unless Stage 0 verifies `Path(module.__file__).resolve().is_relative_to(REPO_ROOT)`, splits SDK tests from `examples/` tests, and runs `mypy` explicitly on the verification test files themselves.

<details>
<summary>The full story (Passes 1-2, dev/tools/run_batch_verification.sh)</summary>

- **Situation:** `dev/tools/run_batch_verification.sh` orchestrates all `10` verification stages across `k8s_agent_sandbox` and `examples/agent-sandbox-rl`, reusing an available virtualenv when present so local runs finish in under two minutes.
- **What goes wrong:** Three harness-level traps surfaced in Passes 1 and 2:
  1. **Editable `.pth` shadowing:** A shared developer venv had an editable install (`__editable__.k8s_agent_sandbox.pth`) pointing at an older scratch worktree. Plain `pytest` imported `k8s_agent_sandbox` from the scratch worktree instead of `REPO_ROOT` until `PYTHONPATH="${SDK_DIR}:${RL_DIR}${PYTHONPATH:+:${PYTHONPATH}}"` was prepended *and* asserted in Stage 0.
  2. **SDK vs. `examples/agent-sandbox-rl` boundary:** `dev/tools/test-unit` creates a clean venv with `pip install -e clients/python/agentic-sandbox-client[test]` and does *not* install `examples/agent-sandbox-rl` (and branches `22/N` to `24/N` predated `25/N`'s RL integration). Having `test_batch_fault_injection.py` under `k8s_agent_sandbox/test/unit/` import `agent_sandbox_rl` broke standalone SDK unit runs.
  3. **`142` hidden `mypy` errors in test files:** Because `[tool.mypy]` in `pyproject.toml` excludes `test/` ([TEST-25](testing.md#test-25)), none of our new verification test files were type-checked until Auditor `4f0c076b` ran `mypy` directly on them and uncovered `142` annotation issues.
- **What we do:**
  - Stage 0 of `dev/tools/run_batch_verification.sh` resolves `k8s_agent_sandbox.__file__` (and `agent_sandbox_rl.__file__` when present) and hard-fails if either resolves outside `REPO_ROOT`, then runs `mypy` explicitly across all `9` verification harness and test files (`0` issues).
  - Stage 5 splits cleanly into Stage 5a (`k8s_agent_sandbox/test/unit/test_batch_fault_injection.py`, `0` `agent_sandbox_rl` imports, safe on every SDK branch and inside `dev/tools/test-unit`) and Stage 5b (`examples/agent-sandbox-rl/tests/test_batch_fault_injection_rl.py`, auto-skipped when `examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py` is absent on lower-stack branches).
- **Example:**
  ```python
  # Stage 0 import-origin assertion in run_batch_verification.sh:
  mod_path = Path(k8s_agent_sandbox.__file__).resolve()
  assert mod_path.is_relative_to(repo_root), f"Shadowed import: {mod_path}"
  ```
- **Related:** [TEST-25](testing.md#test-25), [BENCH-8](gke-benchmarking.md#bench-8).

</details>

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
