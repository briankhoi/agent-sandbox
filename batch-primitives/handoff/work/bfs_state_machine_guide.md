# How Exhaustive BFS State-Machine Checking Works (and How It Caught Our 5-Step Deadlock)

When two or more locks, semaphores, condition variables, signals, or cancellation points interact, unit tests only test the **one** thread schedule that happened to run (or the specific race you thought to script). Exhaustive finite-state model checking tests **every reachable interleaving** across all threads, tasks, retries, cancellations, and signal handlers, and prints the **shortest counterexample trace** when any invariant breaks.

---

## 1. Did the verifier use a special external tool?

**No external tool at all.** Industry teams often use dedicated model checkers like **TLA+ / PlusCal (`TLC`)** (used heavily by AWS on DynamoDB, S3, and EBS; see *"How Amazon Web Services Uses Formal Methods"*), **Spin / Promela**, or Rust's **Loom**.

Under the hood, explicit-state model checkers like `TLC` are literally **Breadth-First Search (BFS) over a directed graph of program states**. For a single protocol (like our batch handle's delete + detach + release lifecycle), you can write the entire checker in **~150 lines of standard-library Python** (`collections.deque` + `@dataclass(frozen=True)`), with zero dependencies, and check 10,000 states in **under 0.2 seconds**.

---

## 2. How do 4 threads produce ~5,000 finite states instead of infinity?

Real code has infinite values (timestamps, strings, HTTP payloads, 1,000 sandboxes). A state-machine model strips away everything that does not affect lock/wait control flow:

1. **Symmetry / small-model property:** Nearly all concurrency bugs (deadlocks, lost wakeups, race windows, leaked permits) show up with **2 worker actors** (`D1`, `D2` with `cap=1` and `cap=2`) + **1 `detach()` actor** + **1 `release()` actor** + **1 signal/cancel event**. You do not need 1,000 threads to prove whether `cap` is respected or whether two locks can cycle.
2. **Program counters (`pc`) only stop at atomic boundaries:** Code that runs under `with self._lock:` with no `await` and no blocking I/O executes as **one indivisible transition**. You only need a `pc` label (`"IDLE"`, `"WAIT_SLOT"`, `"PRE_HTTP"`, `"IN_HTTP"`, `"DONE_OK"`) at points where another thread, coroutine, or POSIX signal can actually interleave!
3. **Hashable immutable states (`@dataclass(frozen=True)`):** Every combination of `(shared_fields, actor_program_counters)` is one node in a graph:

```python
from collections import deque
from dataclasses import dataclass, replace

@dataclass(frozen=True)
class State:
    # Shared handle fields
    detached: bool = False
    lease_released: bool = False
    lc_owner: int | None = None          # Which thread holds _lifecycle_lock (RLock)
    lc_depth: int = 0                    # RLock recursion depth
    del_slots: tuple[int, int] = (0, 0)  # Active deletes on thread 1 and thread 2

    # Each actor's current step ("program counter")
    d1_pc: str = "IDLE"
    d2_pc: str = "IDLE"
    det_pc: str = "IDLE"
    rel_pc: str = "IDLE"
    sig_pc: str = "IDLE"                 # Optional POSIX signal handler interrupting T1
```

Because `State` is frozen, Python hashes it directly into `visited: dict[State, State | None]`. Even if 50,000 interleavings reach the exact same state, BFS visits that state **once**, and `visited[next_state] = current_state` automatically records the shortest path from the initial state!

---

## 3. How the BFS engine works (35-line core pattern)

Instead of letting the OS scheduler pick *one* thread to advance, a generator `transitions(s)` yields **every** `(step_label, next_state)` for **every** actor that is currently runnable from `s`:

```python
def model_check(initial: State):
    visited: dict[State, State | None] = {initial: None}
    edge_label: dict[State, str] = {}
    queue: deque[State] = deque([initial])

    while queue:
        s = queue.popleft()

        # 1. Check SAFETY invariants ("nothing bad is true in state s")
        if s.det_pc == "LEASE_WRITE" and s.d2_pc == "IN_HTTP":
            return reconstruct_trace(s, visited, edge_label)  # Shortest bug trace!

        # 2. Generate every possible next atomic step by ANY runnable thread/signal
        outs = list(transitions(s))
        if not outs:
            # No thread can take a step. Is every actor cleanly DONE/IDLE?
            if not is_clean_terminal(s):
                return reconstruct_trace(s, visited, edge_label)  # DEADLOCK!
            continue

        for label, nxt in outs:
            if nxt not in visited:
                visited[nxt] = s
                edge_label[nxt] = label
                queue.append(nxt)
```

### Safety vs. Liveness (two complementary checks)
1. **Safety ("nothing bad ever happens"):** Checked *forward* on every visited state `s`:
   - *Cap invariant:* `sum(s.del_slots) <= cap`
   - *Lease safety:* Neither `detach()`'s Lease write nor `release()`'s deletecollection runs while another thread's `DELETE` is in flight.
   - *Mutual exclusion:* `detach()` and `release()` never write/delete the Lease at the same time, and whoever entered `_lifecycle_lock` first wins.
   - *Post-detach refusal:* No HTTP `DELETE` starts after `detached == True`.
   - *Deadlock freedom:* Any state with `0` outgoing transitions has all actors finished, `del_slots == (0, 0)`, and `lc_owner is None`.
2. **Liveness ("no livelock / permanently wedged states"):** Checked *backward* in 10 lines after forward BFS finishes. Build reverse edges `next_state -> prev_state`, start a second BFS from all `clean_terminal` states, and walk backward. If `len(can_reach_clean_terminal) < len(visited)`, the leftover states are **livestuck** (trapped in a cycle or waiting on a leaked counter that will never reach zero).

---

## 4. How it caught the 5-step sync ABBA deadlock

In our first candidate ordering (**Ordering 1**), `release()` entered `with self._lifecycle_lock:` first and then waited for active deletes to hit zero (just like `release()` starts with `with self._lifecycle_lock:` today).

Forward BFS found `12` deadlocked terminal states and printed this **5-step shortest trace**:

1. `D1:take_slot` (Main thread `T1` calls `release_member()` and increments `_deletes_in_flight` to `1`.)
2. `D1:start_http` (`T1` releases `self._lock` and starts its blocking socket `DELETE`.)
3. `REL:want_lc` (Background/worker thread `T2` calls `batch.release()`.)
4. `REL:acq_lc_and_commit_release` (`T2` acquires `self._lifecycle_lock`, sets `_detached = True`, and waits for `T1`'s active delete to finish.)
5. `SIG_on_D1:start_release` (User presses Ctrl-C. In Python, POSIX signals **always** run on the main thread `T1`, interrupting `D1:start_http`. The signal handler calls `batch.release()`, which tries `with self._lifecycle_lock:`!)

Look at the cycle at Step 5:
- **Main thread `T1`** holds `del_slots[T1] = 1` (paused mid-HTTP by the signal handler) and blocks waiting for `_lifecycle_lock` (held by `T2`).
- **Thread `T2`** holds `_lifecycle_lock` and blocks waiting for `del_slots[T1]` to drop to `0`.
- `threading.RLock` only lets **`T2`** re-enter `_lifecycle_lock`, not `T1`. Both threads hang forever!

When we flipped to **Ordering 2** (both `detach()` and `release()` set `_detached = True` under `_lock` and wait for active deletes **before** acquiring `with self._lifecycle_lock:`), `T2` has not acquired `_lifecycle_lock` while waiting for `T1`'s delete, and `T1`'s signal handler subtracts `T1`'s own delete slot (`1 - 1 = 0`), acquires `_lifecycle_lock`, finishes `release()`, and returns. Across all **5,905 sync states** and **3,782 async states**, deadlocks and livestuck states dropped to **0**.

---

## 5. Why "negative controls" matter when model checking

A model checker is code too: if you accidentally forget a transition, BFS might report `0 violations` because the buggy path was never generated! In our verifier run, we tested **5 intentional bug toggles** ("mutation tests for the model") to prove the checker catches each class of bug:

| Toggle | Intentional Flaw | Result |
| --- | --- | --- |
| **Ordering 1** | Wait for deletes *inside* `with self._lifecycle_lock:` | Caught 5-step signal ABBA deadlock (`12` deadlocks) |
| **Flaw A** | `release()` clears `_detach_grace = None` *before* `_lifecycle_lock` | Caught 8-step violation of `12/N`'s "first call wins" when `detach()` already holds `_lifecycle_lock` |
| **Flaw B** | `_check_lease_held()` checks `_lease_released` instead of `_detached` | Caught 3-step post-detach `DELETE` after failed `detach()` / `client.close()` |
| **Flaw C** | Sync drain forgets to subtract current thread's own delete count | Caught self-deadlock when a signal handler interrupts `release_member()` on 1 thread |
| **Flaw D** | Async `finally:` does `async with self._lock:` before decrementing counter | Caught 6-step leaked slot (`284` livestuck states) when a task is cancelled twice |

---

## 6. When to use this trick in future projects

You do **not** need this for ordinary single-lock code. Reach for a 100-line Python BFS checker whenever **two or more** of these show up together:
- Two locks, or a lock + semaphore/condition wait (`ABBA` lock-order risk)
- Signal handlers / reentrant callbacks (`RLock` + thread ownership)
- Async task cancellation (`CancelledError` at every `await`, including inside `finally:`)
- Optimistic-concurrency retries + shared state flags (`detached`, `lease_released`, `released`)

Writing the frozen `State` + `transitions()` takes ~10 minutes (or 2 minutes for an agent), runs in `< 1` second, and checks schedules a human would never think to write a unit test for.
