# Comprehensive Batch Verification Strategy: Formal Modeling, Fault Injection, and Platform Invariance

## 1. Executive Summary & Root Cause Analysis

### The Verification Blind Spot
The Python SDK batch claim architecture previously employed two independent verification methods:
1. **Cross-Process TLA+ Model Checking (`BatchLease.tla`):** Modeled the distributed Lease protocol across processes (Kubernetes API server, competing client handles, reaper CronJob, garbage collection, and network delays).
2. **In-Process State Machine Testing (`verification.md` & unit tests):** Tested in-memory locks, task cancellation, and isolated subsystem methods.

**Why Janet's and Aditya's bugs were missed:**
- **The Subsystem Seam Problem (Janet's Bug):** `BatchLease.tla` modeled a handle giving up on its Lease as an atomic event (`GiveUp`), while unit tests tested `_renew_once()` and `_watch_loop()` in isolation. Neither model checked the seam between them: `_renew_loop` coupled its termination to `self._state.error()`, which meant a non-retryable watch stream failure (403 Forbidden or TLS cert error) silently terminated Lease renewals for active, connected sandboxes.
- **The Kernel Stack Blind Spot (Aditya's Bug):** Prow CI and Google development containers run exclusively on Linux kernels, where connecting to a bound, non-listening port immediately returns TCP RST (`NewConnectionError`). On Darwin/macOS (BSD network stack), the kernel drops the SYN packet, causing `urllib3` to time out (`ConnectTimeoutError`). Linux cannot natively observe Darwin BSD socket behavior.

### Core Verification Principle: Test the Harness First (Negative Controls)
**We must NOT fix Janet's and Aditya's bugs prior to running the new verification tools.** 
Instead, we must keep the existing unfixed code as our baseline **negative control**:
1. When we run the new TLA+ model `BatchHandle.tla` against the current logic (`if self._detached or self._state.error() is not None: return False`), TLC must fail and generate a counterexample trace that exactly reproduces Janet's bug.
2. When we run the new Python fault-injection suite against the current code, the test must fail and prove live that a watch error causes Lease renewal to stop.
3. When we simulate or test Darwin socket behavior, the socket test must fail.
4. Only after the tools demonstrate they catch these bugs will we apply the fixes and verify that the tools turn green.

---

## 2. Pillar 1: Expanded TLA+ Formal Verification Suite

We will expand formal verification by introducing three modular specifications in `handoff/work/tla/`:

```
+-----------------------------------------------------------------------------------------+
|                               TLA+ SUBSYSTEM SPECIFICATIONS                             |
+------------------------------+----------------------------------------------------------+
| 1. BatchHandle.tla           | Models internal subsystem composition:                   |
|    (Subsystem Composition &  | - renew_loop, watch_loop, create_loop, acquire()         |
|     Fencing Invariants)      | - Proves renewal NEVER dies while active sandboxes live  |
+------------------------------+----------------------------------------------------------+
| 2. BatchWatch.tla            | Models K8s event stream & in-memory cache sync:          |
|    (Watch, Re-listing &      | - ADDED, MODIFIED, DELETED, 410 Gone compaction          |
|     State Reconciliation)    | - Proves state monotonicity and stale connection cleanup |
+------------------------------+----------------------------------------------------------+
| 3. BatchConsumer.tla         | Models the 3 consumption modes & dynamic membership:     |
|    (Quorum, Group Outcomes,  | - wait_for_quorum, group_outcomes, events, acquire       |
|     Events & Ordinals)       | - Proves deadlock-free dispatch and ordinal safety       |
+------------------------------+----------------------------------------------------------+
```

### Spec 1: `BatchHandle.tla` (Subsystem Composition & Fencing)
- **Scope:** Models the concurrent execution and failure modes of background tasks within a single `SandboxBatch` handle.
- **State Variables:**
  - `renewState`: `{"IDLE", "RENEWING", "DEGRADED", "STOPPED"}`
  - `watchState`: `{"STREAMING", "RECONNECTING", "DEAD_403", "DEAD_NETWORK"}`
  - `createState`: `{"ACTIVE", "DRAINED", "STOPPED"}`
  - `activeSandboxes`: Integer (count of sandboxes actively connected and executing commands)
  - `leaseExpiresAt`: Simulated timestamp
  - `detached`: Boolean
  - `leaseExpiredLocally`: Boolean
  - `stateError`: `{"NONE", "WATCH_FAILED", "LEASE_EXPIRED", "CANCELLED"}`
- **Actions:**
  - `WatchFails(errType)`: Transitions `watchState` to `DEAD_403` or `DEAD_NETWORK` and notes error in `stateError`.
  - `RenewTick`: Performs renewal write. Checks exit condition.
  - `LocalExpiryPasses`: Triggers `_expire_lease_if_due()`, setting `leaseExpiredLocally := TRUE`.
  - `DetachHandle`: Sets `detached := TRUE`.
  - `AcquireMember`: Increments `activeSandboxes` when a member becomes Ready.
  - `ReleaseMember`: Decrements `activeSandboxes`.
- **Invariants to Check:**
  1. **`ActiveWorkloadProtected` (Catches Janet's Bug):**
     ```tla
     ActiveWorkloadProtected ==
         (activeSandboxes > 0 /\ now < leaseExpiresAt /\ ~detached /\ ~leaseExpiredLocally) =>
             renewState /= "STOPPED"
     ```
     *Baseline check (with current bug):* TLC must violate this invariant on the path where `WatchFails("403")` sets `stateError = "WATCH_FAILED"` and `RenewTick` transitions `renewState` to `"STOPPED"`.
     *Post-fix check:* TLC must pass when the exit condition is restricted to `detached \/ leaseExpiredLocally`.
  2. **`WatchDeadWakesWaiters`:**
     ```tla
     watchState \in {"DEAD_403", "DEAD_NETWORK"} =>
         \A waiter \in PendingWaiters : waiter.state = "UNBLOCKED_WITH_ERROR"
     ```
  3. **`NoStaleRenewalAfterExpiry`:**
     ```tla
     now >= leaseExpiresAt => renewState \in {"DEGRADED", "STOPPED"}
     ```

### Spec 2: `BatchWatch.tla` (Watch Stream, 410 Compaction & Cache Reconciliation)
- **Scope:** Models the event stream from the Kubernetes API server into in-memory `BatchState`.
- **State Variables:**
  - `k8sClaims`: Map of claim names to server status (`PENDING`, `READY`, `DELETION_TIMESTAMP`, `NON_EXISTENT`).
  - `cacheClaims`: Map of claim names to cached status in `BatchState` (`PENDING`, `READY`, `LOST`, `FAILED`).
  - `connectedCache`: Set of claim names with open cached TCP connections.
  - `serverResourceVersion`: Integer.
  - `watchResourceVersion`: Integer.
  - `compactionHorizon`: Integer (resourceVersions older than this return 410 Gone).
- **Actions:**
  - `K8sUpdateClaim(name, newStatus)`: Apiserver state change.
  - `WatchEventDeliver`: Streams an `ADDED`, `MODIFIED`, or `DELETED` event to the handle.
  - `WatchDisconnect(reason)`: Simulates TLS EOF, 503 backoff, or 410 Gone.
  - `Perform410Relist`: Issues collection `LIST`, reconciles cache via `resync_from_list()`.
- **Invariants to Check:**
  1. **`MonotonicMemberState`:** A member in `cacheClaims` never regresses from `READY` to `PENDING`, nor from `LOST`/`FAILED` back to `READY`.
  2. **`TerminalClaimConnectionEviction`:**
     ```tla
     \A c \in DOMAIN cacheClaims :
         cacheClaims[c] \in {"LOST", "FAILED"} => c \notin connectedCache
     ```
  3. **`WarmCreditConservation`:** Warm credit never exceeds the actual number of Ready unconsumed sandboxes during relists or watch reconnects.

### Spec 3: `BatchConsumer.tla` (Consumption Modes, Dynamic Ordinals & Quorum)
- **Scope:** Models the caller-facing consumption modes and dynamic `acquire()` interactions.
- **State Variables:**
  - `consumptionMode`: `{"UNSET", "QUORUM", "GROUP", "STREAM"}`
  - `yieldedMembers`: Set of member names handed to caller.
  - `allocatedOrdinals`: Set of integers.
  - `nextOrdinal`: Integer.
  - `groupStatuses`: Map of group names to Ready counts.
- **Actions:**
  - `CallWaitForQuorum`, `CallGroupOutcomes`, `CallEvents`.
  - `CallAcquire(pool)`.
  - `CallReleaseMember(name)`, `CallReleaseGroup(pool)`, `CallReleaseNotReady`.
- **Invariants to Check:**
  1. **`MutualExclusionOfModes`:** Calling `events()` first permanently prevents entering `wait_for_quorum()` or `group_outcomes()`.
  2. **`NoDoubleYield`:** No member is yielded more than once across any combination of iterators.
  3. **`UniqueOrdinals`:** `\A o1, o2 \in allocatedOrdinals : o1 = o2 => IdenticalClaim`.
  4. **`UnreachableQuorumFailsFast`:** If remaining available members in a group fall below `min_ready`, `QuorumUnreachableError` raises immediately without waiting for `quorum_timeout`.

---

## 3. Pillar 2: Automated Subsystem Chaos & Fault-Injection Suite

We will implement `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_batch_fault_injection.py`. This suite will programmatically inject faults into one subsystem and verify the safety of all other subsystems.

### Fault Injection Test Matrix

| Injected Fault | Target Subsystem | Required Behavior of Other Subsystems |
|---|---|---|
| **Watch 403 Forbidden** | `_watch_loop` | Lease renewal **continues uninterrupted**; active connected sandboxes stay alive; pending `acquire()` fails fast with `BatchError`. |
| **Watch 410 Gone during rapid deletes** | `_watch_loop` | `resync_from_list` reconciles state; stale connections evicted; no duplicate events yielded. |
| **Lease Renewal 500/Timeout** | `_renew_loop` | `_renewal_degraded` becomes `True`; watch continues until `_lease_expires_at`; existing connections stay open. |
| **Lease Renewal 409 Conflict** | `_renew_loop` | `read_batch_lease` validates holder identity; if holder matches, renewal recovers; if holder changed, marks `BatchInUseError` and fences immediately. |
| **Create 429 Too Many Requests** | `_create_loop` | `create_rps` pacer enters cooldown; Lease renewal and watch stay healthy. |
| **Warm Pool Check 404** | `acquire()` | Raises `SandboxWarmPoolNotFoundError`; leaves batch Lease and all existing members healthy. |
| **Acquire Timeout Mid-Flight** | `acquire()` | Drops allocated member from `members()`; deletes created claim; releases `_in_flight` slot; does not poison initial batch quorum. |
| **Driver SIGTERM / `client.close()`** | Batch Client | Calls `detach()` for `get_batch` handles or `release()` for `claim_batch` handles; closes connection pools without hanging on dead sockets. |

---

## 4. Pillar 3: Dynamic Concurrency & Lock Interleaving Stress Suite

To guarantee that internal locks (`_lock`, `_batches_lock`, `_credit_refresh_locks`, `_lifecycle_lock`) never deadlock or leak state under high concurrency, we will implement `test_batch_concurrency_stress.py`:

1. **High-Concurrency Acquire / Release Race:**
   - 50 concurrent worker threads calling `acquire()` on the same warm pool while another thread repeatedly calls `release_member()` on ready members.
   - Assertions: Zero duplicate ordinals; `_in_flight` semaphore count accurately returns to initial capacity; zero leaked tasks in the event loop.
2. **Concurrent Detach / Re-Attach Race:**
   - Thread 1 calls `detach(grace=10)`.
   - Thread 2 simultaneously calls `SandboxClient.get_batch()`.
   - Assertions: Exactly one handle holds the Lease; the other yields or raises `BatchInUseError`; no claims are orphaned or double-deleted.
3. **Paced Creation vs Immediate Cancellation:**
   - Launch a batch with 100 planned claims at `create_rps=10`.
   - Cancel the consumer context after 5 claims are sent.
   - Assertions: `_stop_creating` halts background creation immediately; in-flight creates are cleaned up; no orphaned claims remain in Kubernetes.

---

## 5. Pillar 4: Platform & Network Transport Invariance

To eliminate the class of platform discrepancies that caused Aditya's bug, we will establish strict platform-agnostic guidelines and automated mock transport validation:

### 1. Eliminating Kernel-Dependent Loopback Tests
- **The Rule:** Unit tests must never rely on OS-specific socket state transitions (such as Linux returning RST on bound non-listening ports vs Darwin dropping SYN packets).
- **Sanitization Patterns:**
  - **Pattern A (Transport-Level Mocking):** For testing retry policies, mock at the `urllib3.connectionpool.HTTPConnectionPool` or `urllib3.util.Retry` boundary instead of binding real loopback sockets.
  - **Pattern B (OS-Agnostic Socket Closure):** If an ephemeral port is needed to verify connection failure, bind the socket, record the port, and call `sock.close()` *before* attempting connection. An unbound port triggers an immediate TCP RST on Linux, macOS, and BSD alike.

### 2. Multi-Platform Local & CI Verification
- Audit all existing unit tests under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/` for `socket.socket()` usage.
- Add an explicit GitHub Actions matrix or Docker container profile testing across both Linux and BSD/Darwin network behaviors.

---

## 6. Execution Roadmap & Verification Milestones

```
+-----------------------------------------------------------------------------------------+
|                                    EXECUTION PHASES                                     |
+-------------------+---------------------------------------------------------------------+
| Phase 1           | 1. Draft BatchHandle.tla and test_batch_fault_injection.py          |
| (Negative-Control | 2. Run against current code: verify BOTH tools fail and reproduce   |
|  Validation)      |    Janet's bug live.                                                |
|                   | 3. Run test_k8s_helper.py under Darwin socket simulation: verify    |
|                   |    Aditya's bug reproduces.                                         |
+-------------------+---------------------------------------------------------------------+
| Phase 2           | 1. Draft BatchWatch.tla and BatchConsumer.tla                       |
| (Full Formal      | 2. Run TLC across all configurations to hunt for any unmodeled      |
|  Verification)    |    state machine or deadlock bugs.                                  |
+-------------------+---------------------------------------------------------------------+
| Phase 3           | 1. Implement the fixes for Janet's bug, Aditya's test, and any new  |
| (Fix Application  |    discoveries from Phases 1 & 2.                                   |
|  & Sign-Off)      | 2. Verify all TLA+ models and Python test suites turn green.        |
+-------------------+---------------------------------------------------------------------+
```

### Next Steps
1. Share this strategy document with other agents and engineers to gather additional failure modes and attack angles.
2. Review aggregated feedback and finalize the formal model specifications.
3. Execute Phase 1 (negative-control reproduction) to validate the test harness against the live repository.
