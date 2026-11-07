<a id="sec-verification"></a>

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)

# Verification: checking every interleaving, not just the ones you thought of

A unit test runs one schedule: the one the OS picked, or the one you scripted with Events and doubles. Concurrency bugs live in the schedules nobody thought to script, like a Ctrl-C landing on the one thread that is mid-delete while another thread holds a lock. An explicit-state model checker turns the code's control flow into a small state machine, then visits every state reachable under every interleaving and prints the shortest path to any state that breaks a rule. Ours needs no tool at all: the search engine is about 35 lines of standard-library Python (`collections.deque` plus a frozen dataclass), and the whole checker, with a sync model, an async model, six invariant families, and 15 configurations, is about 1,000 lines that run in about 4.5 seconds.

## The story: the 15/N deadlock the checker caught

During 15/N, `detach()` and `release()` had to wait for in-flight member deletes before touching the Lease ([LOCK-11](field-guide/lock.md#lock-11), [LOCK-17](field-guide/lock.md#lock-17)). There were two candidate orders:

- **Ordering 1:** take `_lifecycle_lock`, then wait for active deletes inside it.
- **Ordering 2:** set `_detached` under `_lock` (so no new delete starts), wait for active deletes with `_lifecycle_lock` released, then take `_lifecycle_lock` and re-check.

Ordering 1 looked simpler and passed every test we had. The checker (`handoff/work/verify_delete_lifecycle_model.py`, notes commit `77e1d7b5`) disagreed. In the sync model with a signal allowed on the delete thread, it found 12 deadlocked terminal states and 51 livestuck states at `cap=1` (102 livestuck at `cap=2`). The shortest doomed trace it prints is 5 steps:

1. `D1:take_slot`. The main thread T1 calls `release_member()` and counts one active delete.
2. `D1:start_http`. T1 drops `_lock` and starts its blocking HTTP DELETE.
3. `DET:begin_detach`. Thread T3 calls `detach()` and sets `_detached`.
4. `DET:acq_lc`. T3 takes `_lifecycle_lock` and waits for T1's delete to finish (Ordering 1).
5. `SIG_on_D1:start_detach`. Ctrl-C arrives. Python runs signal handlers on the main thread, so the handler runs on T1, on top of the paused DELETE, and calls `detach()`.

Nobody is blocked yet at step 5, but every continuation is stuck. The handler will want `_lifecycle_lock`, which T3 holds. T3 waits for T1's delete, which cannot finish until the handler returns. The backward liveness pass flags this state; the forward pass reaches a fully stuck terminal state 3 steps later (8 steps total). The guide (`handoff/work/bfs_state_machine_guide.md` section 4) tells the same cycle with `release()` on T2 instead of `detach()` on T3, also 5 steps.

**Why the RLock didn't help.** `_lifecycle_lock` is a `threading.RLock` precisely so that a signal handler can re-enter it ([LOCK-18](field-guide/lock.md#lock-18)). But reentrancy is per thread: only the owner may re-acquire. Here the owner is T3 and the handler runs on T1, so the RLock blocks like any other lock. Reentrancy fixes self-deadlock, not cross-thread wait cycles.

**The fix: Ordering 2 ([LOCK-19](field-guide/lock.md#lock-19)).** Never wait on the drain while holding a lock the drained work's handler may need. With the wait moved outside `_lifecycle_lock`, T3 holds nothing while it waits, and the handler on T1 subtracts T1's own delete ([CANCEL-12](field-guide/cancel.md#cancel-12)), takes the lock, finishes, and returns. The checker's numbers before and after:

| Model (sync, signals + failed-detach retry on) | States | Deadlocks | Livestuck |
| --- | --- | --- | --- |
| Ordering 1, `cap=1` | 3,425 | 12 | 51 |
| Ordering 1, `cap=2` | 2,732 | 12 | 102 |
| Ordering 2, `cap=1` | 5,905 | 0 | 0 |
| Ordering 2, `cap=2` | 4,394 | 0 | 0 |

Ordering 2 has more states because more interleavings stay alive instead of collapsing into stuck ones. The async model passes with both orderings (Ordering 2: 3,782 and 3,182 states; Ordering 1: 3,648 and 3,184), because a task can't be re-entered on one thread the way a signal re-enters a thread.

**What shipped (checked against 23/N code).**

- Sync `SandboxBatch.release()` runs a short first `with self._lifecycle_lock:` block (check, set `_detached`, set `_stop_creating`, clear `_detach_grace`, notify), then calls `self._wait_active_deletes()` with the lock released (the comment says why), then re-takes `_lifecycle_lock` and calls `_check_releasable_locked()` again.
- Sync `SandboxBatch.detach()` sets its flags under `_lock` only, calls `_wait_active_deletes(for_detach=True)`, and takes `_lifecycle_lock` only around the Lease read and write.
- Sync `_wait_active_deletes()` loops `while self._active_deletes.total() > self._active_deletes[tid]`, so a handler never waits for its own thread's delete (the fix for negative control Flaw C below).
- Async `AsyncSandboxBatch.detach()` waits before `_lifecycle_lock`; async `release()` waits inside it, as [LOCK-19](field-guide/lock.md#lock-19) documents. The checker applied one ordering to both actors at a time, so it never ran this exact mix. A copy with one extra config flag, `handoff/work/verify_delete_lifecycle_model_async_mix.py`, runs it: 3,914 states (`cap=1`) and 3,366 (`cap=2`), 0 violations.
- One small drift: the sync model's Ordering 2 `release()` marks `_detached` without `_lifecycle_lock` and clears `_detach_grace` after the wait, while the shipped code clears it inside the brief first lock block. That block contains no wait, so it can't join a wait cycle, but the model no longer matches the code line for line (see [VER-7](#ver-7)).

The checker's 5-step trace also became a deterministic test: `test_lifecycle_calls_wait_for_active_deletes_and_stop_further_deletes`, subtest "same-thread signal-handler reentry does not deadlock". A mocked `delete_sandbox_claim` plays T1's signal handler and calls `detach()` or `release()` while a real `detach()` thread waits, then asserts the waiter wakes before the delete returns ([VER-9](#ver-9)).

## How an explicit-state model checker works

Four ideas carry the whole technique.

1. **A state is a frozen value.** Put every shared field and every actor's program counter (`pc`) into one `@dataclass(frozen=True)`. Frozen means hashable, so a dict can remember every state you have seen.
2. **Program counters stop only at atomic boundaries.** Code between two interleaving points runs as one step. Our `pc` labels are things like `IDLE`, `WAIT_SLOT`, `PRE_HTTP`, `IN_HTTP`, `WAIT_LC_LOCK`, `DONE_OK`.
3. **`transitions(s)` yields every runnable step of every actor.** The OS picks one; the checker takes all of them. Blocking is just "this actor yields nothing until its guard holds."
4. **BFS with a parent map.** Breadth-first search reaches each state first by a shortest path, and `visited[next] = current` records it, so the first violation you find comes with the shortest counterexample for free.

A compact skeleton, adapted from `check_sync_model`:

```python
from collections import deque
from dataclasses import dataclass, replace

@dataclass(frozen=True)
class State:
    detached: bool = False
    lc_owner: int | None = None        # which thread holds _lifecycle_lock
    slots: tuple[int, int] = (0, 0)    # active deletes per thread
    d1_pc: str = "IDLE"
    det_pc: str = "IDLE"

def transitions(s):
    if s.d1_pc == "IDLE" and not s.detached:
        yield "D1:take_slot", replace(s, d1_pc="IN_HTTP", slots=(1, s.slots[1]))
    if s.d1_pc == "IN_HTTP":
        yield "D1:http_ok", replace(s, d1_pc="DONE", slots=(0, s.slots[1]))
    # ...detach, release, signal handler, retries...

def check(init, invariants, is_clean):
    visited, label, queue = {init: None}, {}, deque([init])
    def trace(s):
        steps = []
        while visited[s] is not None:
            steps.append(label[s]); s = visited[s]
        return steps[::-1]
    while queue:
        s = queue.popleft()
        for name, holds in invariants:          # safety: a predicate on one state
            if not holds(s):
                return name, trace(s)
        outs = list(transitions(s))
        if not outs and not is_clean(s):        # nothing can move, yet not finished
            return "deadlock", trace(s)
        for lbl, nxt in outs:
            if nxt not in visited:
                visited[nxt], label[nxt] = s, lbl
                queue.append(nxt)
    return None
```

Our real checker adds one more pass after the BFS: build reverse edges, BFS backward from every clean terminal state, and report any reachable state that can't get back to one. That is the liveness check ([VER-4](#ver-4)).

## Principles

<a id="ver-1"></a>

### VER-1 Model only what affects control flow, and keep the scope small

- **Principle:** Strip out payloads, names, timestamps, and counts that don't change which branch runs. Use the fewest actors that can still collide: here two deleters (`D1`, `D2`), one `detach()`, one `release()`, one signal, and `cap` of 1 and 2.
- **Why it works:** The small-scope hypothesis (from the Alloy work) observes that most design bugs show up in small instances. A wait cycle needs two parties, not a thousand. Small models also run in about a second or less, so you rerun them freely.
- **How we applied it:** 1,000 sandboxes became `del_slots: tuple[int, int]`. HTTP payloads became two outcomes, `http_ok` and `http_503_retry`. The whole sync model has 1,958 to 8,247 states per configuration.
- **Watch out:** Pick the scope per bug class. `cap=1` alone would never show two concurrent DELETEs, so the checker asserts that `cap=2` runs actually observe them (`Inv1_Parallelism`). A model that is too small to exercise a feature proves nothing about it.

<a id="ver-2"></a>

### VER-2 An atomic step is the code between two interleaving points

- **Principle:** A lock-protected block with no `await` and no blocking call is one step. Every `await`, every blocking I/O call, every `Condition.wait`, and (in sync Python) every bytecode boundary where a signal may land is a boundary.
- **Why it works:** Too few boundaries hides real interleavings. Too many explodes the state space with interleavings that can't happen. Matching the runtime's actual preemption points gives you exactly the schedules that exist.
- **How we applied it:** `D1:take_slot` covers `with self._lock: self._check_healthy(); self._active_deletes[tid] += 1` as one step. The async model puts `CancelledError` at every await, and the sync model lets a signal start while `D1` is in `WAIT_SLOT` or `IN_HTTP`. The same rule explains [LOCK-20](field-guide/lock.md#lock-20): an await-free asyncio block is atomic, so its getters need no lock.
- **Watch out:** In asyncio, `await lock.acquire()` on a free lock takes a no-yield fast path, so it is not a boundary unless someone can hold the lock across an await ([CANCEL-5](field-guide/cancel.md#cancel-5)). When we sketched the [CANCEL-5](field-guide/cancel.md#cancel-5) exercise below, the first two drafts treated a free acquire as a suspension point and reported bugs that can't happen.

<a id="ver-3"></a>

### VER-3 State safety rules as predicates on a single state

- **Principle:** Write each promise as one sentence ([PROC-1](process.md#proc-1)), then as a boolean function of one state. Add ghost fields when a rule depends on history.
- **Why it works:** A predicate on one state is cheap to check at every visited state, and a violation points at one exact state with a trace to it.
- **How we applied it:** `Inv2_Cap` is `sum(del_slots) <= cap`. `Inv3_LeaseSafety` says no Lease write or `deletecollection` runs while another thread's DELETE is in flight. `Inv6_PostDetachRefusal` uses ghost flags `d1_started_after_detach` that record whether `detached` was already true when the HTTP call started. `Inv4_FirstCallWins` uses the ghost field `first_lc_winner` to encode 12/N's "first of detach() and release() to enter the lock wins".
- **Watch out:** Ghost fields multiply states. Record only the one fact the rule needs, not a log.

<a id="ver-4"></a>

### VER-4 Check deadlock forward and liveness backward

- **Principle:** A deadlock is a state with no enabled transitions that is not a clean finish. A livestuck state is any reachable state from which no clean finish is reachable. Check the first during BFS and the second afterward, by BFS over reversed edges from all clean terminals.
- **Why it works:** Deadlock detection only fires once every actor is stuck. Liveness catches the earlier point of no return, and cycles where actors keep moving but never finish.
- **How we applied it:** `is_clean_terminal` requires every actor done or idle, `del_slots == (0, 0)`, and `lc_owner is None`, so a leaked slot or a held lock counts as stuck. Ordering 1's liveness trace (5 steps) is shorter than its deadlock trace (8 steps), because `release()` could still take a harmless step before everyone was stuck.
- **Watch out:** "Clean" must include resources, not just program counters. A model where every actor reaches `DONE` while a counter stays at 1 would pass a naive deadlock check (that is Flaw D).

<a id="ver-5"></a>

### VER-5 Model the environment as an adversary

- **Principle:** The environment moves too. Give it every move the real world allows, at every point it can make it, bounded to keep the model finite.
- **Why it works:** The bugs we found all needed an environment move: a signal on the main thread, a second cancel during cleanup, a 503 that forces a retry, a failed write followed by a retry.
- **How we applied it:** The sync model allows at most one signal, on the delete thread or the detach thread, running either `detach()` or `release()`, plus HTTP 503 retries and one retry after a failed detach (`followup_used`). The async model cancels at every await and adds client `close()` (`_stop_batches`) as an actor.
- **Watch out:** Bound each adversary move explicitly (one signal, two cancels, one retry) and write the bound down. An unbounded retry loop makes the state space infinite, and a silent bound reads as "proved" when it means "proved up to one signal".

<a id="ver-6"></a>

### VER-6 Run negative controls: break the model on purpose

- **Principle:** A checker that reports 0 violations might be right, or might never generate the bad path. Add toggles that reintroduce known bugs and require each one to fail.
- **Why it works:** It is mutation testing for the model ([TEST-1](testing.md#test-1)). Each failing toggle proves one bug class is reachable and detectable.
- **How we applied it:** `main()` asserts the expected outcome for every configuration. The controls and the shortest traces they print today:

  | Toggle | Bug reintroduced | Result |
  | --- | --- | --- |
  | Ordering 1 + signal on delete thread | wait inside `_lifecycle_lock` | 12 deadlocks, 5-step liveness trace |
  | Flaw A | `release()` clears `_detach_grace` before `_lifecycle_lock` | 8-step `Inv4_FirstCallWins` |
  | Flaw B (sync / async) | lease check reads `_lease_released`, not `_detached` | 3-step / 4-step `Inv6_PostDetachRefusal` |
  | Flaw C | drain does not subtract the caller's own thread | 6 deadlocks, 3-step liveness trace |
  | Flaw D (async) | `finally` awaits `_lock` before decrementing | 12 deadlocks, 284 livestuck, 6-step liveness trace ([CANCEL-4](field-guide/cancel.md#cancel-4)) |

- **Watch out:** Variant E (`_stop_batches()` skips waiting for active deletes) "passes" while reporting `close_during_http_states=68`. Closing the HTTP session during a DELETE was a counter we printed, not an invariant we enforced, so the checker could not fail on it. If a property matters, make it an invariant and give it a control.

<a id="ver-7"></a>

### VER-7 Keep the model next to the code, and rerun it on lock changes

- **Principle:** Treat the model as code that describes the code. Name `pc` labels and helpers after the real methods, and rerun the checker whenever lock order, a wait, or a flag check moves.
- **Why it works:** A model that drifts from the code verifies a program nobody runs.
- **How we applied it:** Helpers like `_other_active_deletes`, `_release_started`, and `_lease_check_blocks_delete` mirror `_wait_active_deletes`, `_release_started()`, and `_check_lease_held()`. The model lives in the notes repo next to the guide that explains it.
- **Watch out:** We already have drift. Async `release()` shipped with the wait inside the lock while async `detach()` waits outside, a mix the committed checker never ran, and sync `release()` gained a brief first `_lifecycle_lock` block the model doesn't have. Both look safe (the mix passes in `verify_delete_lifecycle_model_async_mix.py`), but the committed model doesn't say so. Adding the model to CI, or at least to the PR checklist for lock changes, would catch this.

<a id="ver-8"></a>

### VER-8 A Sans-I/O core makes the real code checkable too

- **Principle:** Put state transitions in a core with no I/O, locks, or clock, and let thin shells do the waiting. Then a race in the core is a plain sequence of method calls.
- **Why it works:** Without hidden suspension points, every core method is one atomic step by construction. A test that calls methods in race order is a hand-picked path through the same state graph a checker would explore.
- **How we applied it:** `batch_state.BatchState` holds every transition, and `test_batch_state.py` replays races like "release while a READY event is queued" as straight-line calls ([TEST-3](testing.md#test-3), and the layering section of patterns.md). The shells follow one rhythm: lock, check, call one core method, copy to locals, unlock, then do I/O.
- **Watch out:** The core has no lock of its own, so the shells' lock discipline is now the thing to check. That is where the lifecycle model and the [CANCEL-5](field-guide/cancel.md#cancel-5) AST scan come in. A natural next step is a Hypothesis stateful test that drives `BatchState` with random call sequences against invariants.

<a id="ver-9"></a>

### VER-9 Turn every counterexample into a deterministic regression test

- **Principle:** The checker proves the design; a test pins the code. Replay each trace as a test that forces the same interleaving.
- **Why it works:** The model can drift ([VER-7](#ver-7)), and the code can regress without anyone rerunning the model. A unit test runs on every PR.
- **How we applied it:** The tools in [TEST-8](testing.md#test-8) to [TEST-14](testing.md#test-14) are exactly what you need to replay a trace: fire the competing action from inside a test double ([TEST-8](testing.md#test-8)), instrument the lock to know when a racer blocks ([TEST-9](testing.md#test-9)), hit the window between two writes ([TEST-10](testing.md#test-10)), simulate the signal handler from inside a mocked call with a bounded join ([TEST-11](testing.md#test-11)), and cancel at an exact await ([TEST-14](testing.md#test-14)). The 5-step deadlock became the "same-thread signal-handler reentry does not deadlock" subtest described above. [TEST-24](testing.md#test-24) keeps the sync and async suites name-aligned so each trace gets its twin.
- **Watch out:** A replayed trace still needs its own mutation check ([TEST-1](testing.md#test-1)). Run it against the old Ordering 1 code, or swap `RLock` for `Lock`, and confirm it fails or times out instead of hanging the suite.

<a id="ver-10"></a>

### VER-10 Use cheap static checks for invariants the model assumes

- **Principle:** When safety depends on a code-shape rule ("no await under this lock"), check the rule mechanically.
- **Why it works:** A model assumes its atomic steps. A static scan or a targeted test confirms the code still has that shape.
- **How we applied it:** [CANCEL-5](field-guide/cancel.md#cancel-5) records an AST scan of `async_sandbox_batch.py` at 23/N: the only await under `_lock` or `_changed` is `Condition.wait` in `_next`, which releases the lock while waiting. [TEST-14](testing.md#test-14) does the dynamic version: a fake pre-holds the lock, so an accidental await in a section that must be atomic blocks until the test's cancel, and the assertion fails.
- **Watch out:** The scan ran once and nothing enforces it, so the safety is inferred ([CANCEL-5](field-guide/cancel.md#cancel-5) says so). Static checks also have blind spots ([TEST-25](testing.md#test-25)), such as awaits hidden behind a helper call.

<a id="ver-11"></a>

### VER-11 Know when to graduate to a real tool

- **Principle:** Hand-rolled BFS is great for one protocol with a handful of actors. Reach for a dedicated tool when the model grows, when you need temporal properties beyond "always" and "eventually finishes", or when you want to check the real code instead of a model.
- **Why it works:** Real tools bring state compression, symmetry and partial-order reduction, fairness, temporal logic, and better trace viewers.
- **How we applied it:** We stayed with Python because each configuration ran in about a second or less and the author could read it. Options for next time:
  - **TLA+ / PlusCal with TLC:** design-level specs of protocols; what AWS used for S3 and DynamoDB designs.
  - **Spin / Promela:** classic explicit-state checker for concurrent processes and LTL properties.
  - **Loom (Rust):** runs the real code under every permitted interleaving of its threads and atomics.
  - **Hypothesis stateful testing (Python):** random call sequences with invariants and shrinking. It explores randomly rather than exhaustively, but it tests the real code.
  - **P:** state machines that communicate by events, with a model checker; also used at AWS.
- **Watch out:** Each tool still checks a model or a harness you wrote. [VER-1](#ver-1) to [VER-6](#ver-6) apply unchanged.

<a id="ver-12"></a>

### VER-12 Know what your formal model abstracts away, and audit that boundary separately

- **Principle:** List the operations and failure modes your formal spec omits, and pair the model with a mechanical code audit for those omitted axes.
- **Why it works:** A passed TLC run gives strong confidence inside its abstraction and zero coverage outside it. Naming the boundary stops "TLC passed 31 configs" from masking bugs in unmodeled calls.
- **How we applied it:** `BatchLease.tla` ([tla.md](tla.md)) proved cross-process ownership and fencing invariants (`BelieverKeepsClaims`, `ClaimSetupFencing`, `ReaperSafety`) across 31 positive and negative configs in `22/N`, catching unscoped `deletecollection`, premature reaper confirmation waits, and unpreconditioned setup rollback. Because the TLA+ spec intentionally abstracted away read-only warm-pool and template `GET`s and caller-local latency budgets (`acquire(timeout=...)`), `23/N`'s `_check_pool` still let `get_sandbox_warmpool`, `get_sandbox_template`, and single-flight waits overrun `acquire(timeout=...)` or `_lease_expires_at` and shared caller-local `BatchError`s across coalesced waiters ([LOCK-21](field-guide/lock.md#lock-21)). Auditing every wait and network call on user-bounded paths (`sandbox_batch.py: SandboxBatch._check_pool`, `_precheck_pools`) and testing against stalled loopback sockets ([TEST-20](testing.md#test-20)) caught what the protocol model omitted (22/N, 23/N).
- **Watch out:** Whenever you add a timeout or Lease fence to write paths because a model flagged them, grep every read and helper call on the same caller path too; hanging a caller past its deadline is a liveness bug even when it violates no ownership safety invariant.

<a id="ver-13"></a>

### VER-13 Graduate one-off checks into a single-command multi-layer verification pipeline

- **Principle:** Combine formal specs (cross-process + subsystem TLA+ and in-process BFS), transitive AST checks, stateful Hypothesis fuzzers, and lockstep sync/async differential harnesses behind one deterministic runner script (`dev/tools/run_batch_verification.sh`), and validate both negative controls (unfixed code fails) and `/tmp` post-fix simulations (fixed code passes).
- **Why it works:** Each layer catches what the others abstract away: TLA+ catches multi-step authority and fencing interleavings (`F-01`, `F-03`, `F-07`, `F-24`, `F-25`), transitive AST call-graph walks enforce model atomicity and lock assumptions across helper boundaries ([VER-10](#ver-10), `F-15`), Hypothesis shrinks `BatchState` partition drift (`F-12`), and lockstep sync/async differential and fault-injection suites catch shell-level divergences (`F-08`, `F-09`) that symmetric-looking unit suites miss.
- **How we applied it:** On `e84e964c`, all `1,100` pre-existing unit tests were green, yet the 10-stage pipeline ([Automated batch verification and chaos suite](chaos-verification-suite.md)) uncovered `29` verified production bugs (`48` failing test reproductions, `21` static AST violations, `22` TLA+ runs over `3,275,347` distinct states). Extending `verify_delete_lifecycle_model.py` (`62,579` total reachable states across `15` models) also closed [VER-6](#ver-6)'s Variant E gap by promoting `close_during_http` from a printed counter to a failing invariant.
- **Watch out:** Never trust a verification harness solely because it turns red on unfixed code. Simulate the proposed fixes in `/tmp` ([CHAOS-1](chaos-verification-suite.md#chaos-1)) and run Hypothesis with a cold cache directory ([TEST-28](testing.md#test-28)) before treating the suite as an oracle.

## When to reach for it

Reach for a model checker when **two or more** of these meet in one design:

- Two locks, or a lock plus a semaphore, Condition, or Event wait (lock-order risk).
- Signal handlers or re-entrant callbacks, especially with an `RLock`.
- Async cancellation, which can strike at every `await`, including inside `finally`.
- Optimistic retries plus shared flags (`detached`, `lease_released`, `released`).
- A drain barrier ("wait until active work finishes") that someone may call from inside that work.

Skip it for single-lock code, pure functions, and logic whose risk is in data rather than in interleavings. Plain tests or property tests cover those better.

## Try it yourself: model CANCEL-5

[CANCEL-5](field-guide/cancel.md#cancel-5) describes two async cancel handlers that `async with self._changed:` before their state change. In `acquire()`, a cancel calls `_drop_acquired()`, which takes the lock, then pops the waiter and calls `BatchState.drop_acquired`. If that acquire ever suspends, a second cancel escapes before the state change, and the claim name stays in `_acquiring` forever.

Sketch the state:

```python
@dataclass(frozen=True)
class S:
    lock: str | None = None   # who holds _changed's lock
    acquiring: bool = True    # name still in _acquiring / _acquire_waiters
    a: str = "WAITING"        # WAITING -> HANDLER_WANT_LOCK -> DONE | ESCAPED
    cancels: int = 0          # environment bound: 2 cancels
    b: str = "IDLE"           # another task that uses _changed
```

The invariant: once the acquire task is finished, it left nothing behind.

```python
def no_leak(s): return not (s.a in ("DONE", "ESCAPED") and s.acquiring)
```

Model three variants: today (task B never holds the lock across an await), the regression (B awaits while holding it), and the hardening [CANCEL-5](field-guide/cancel.md#cancel-5) proposes (call the pure core transition first, then notify). Mind [VER-2](#ver-2): with the lock free, the handler's acquire takes the no-yield fast path, so the whole handler is one step. A worked version lives in `handoff/work/verify_cancel5_exercise.py`: today and the hardened variant had no violations, and the regression failed in 3 steps: `B:lock_then_await`, `A:cancel1_handler_blocks_on_lock`, `A:cancel2_escapes`. Then try the quorum handler in `wait_for_quorum` the same way.

## Further reading

Next step: [Verifying the Lease protocol with TLA+](tla.md) applies the same ideas with TLA+ and TLC to the cross-process Lease protocol, which this in-process checker does not cover.


- Chris Newcombe et al., "How Amazon Web Services Uses Formal Methods", *Communications of the ACM* 58(4), April 2015. PDF: https://lamport.azurewebsites.net/tla/formal-methods-amazon.pdf
- Leslie Lamport's TLA+ home page (video course, PlusCal, TLC): https://lamport.azurewebsites.net/tla/tla.html
- Hillel Wayne, *Learn TLA+*: https://learntla.com
- Loom, permutation testing for concurrent Rust: https://github.com/tokio-rs/loom
- Hypothesis stateful testing: https://hypothesis.readthedocs.io/en/latest/stateful.html
- Alloy and the small-scope hypothesis (Daniel Jackson, *Software Abstractions*): https://alloytools.org
- Spin and Promela: https://spinroot.com
- The P language: https://github.com/p-org/P
- Our own: `handoff/work/bfs_state_machine_guide.md` and `handoff/work/verify_delete_lifecycle_model.py` (notes commit `77e1d7b5`).

---

[Learnings home](../learnings.md) · [Field guide index](../learnings.md#sec-field-guide)
