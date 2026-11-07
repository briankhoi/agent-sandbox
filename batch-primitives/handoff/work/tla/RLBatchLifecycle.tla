-------------------------- MODULE RLBatchLifecycle --------------------------
\* Formal TLA+ specification of agent-sandbox-rl BatchClaims multi-cluster
\* batch claimer, error-triggered batch rotation, and per-batch release_all()
\* teardown coordination (examples/agent-sandbox-rl/agent_sandbox_rl/batch_claims.py).
\*
\* Checks invariants:
\*   - ClosedPreventsLiveInstall: While closed = TRUE, _live remains empty.
\*   - NoOrphanedRenewingBatch (INV-LEAK-1): Every renewing SandboxBatch not
\*     actively executing in a worker or in-flight release_all() loop remains
\*     tracked in _opened so a subsequent release_all() retry can release it.
\*
\* Negative controls:
\*   1. Bugs = {"untrackedClosedRelease"} reproduces _batch_for skipping
\*      self._opened when self._closed is True and failing transiently on inline
\*      batch.release() (batch_claims.py:135-153).
\*   2. Bugs = {"clearOpenedUpfront"} reproduces release_all() clearing
\*      self._opened upfront before iterating batch.release(), dropping batches
\*      on mid-loop transient failure (batch_claims.py:185-191).

EXTENDS Integers, FiniteSets

CONSTANTS
  Workers,      \* e.g. {"w1", "w2"}
  MaxBatches,   \* e.g. 2
  Bugs          \* Subset of {"untrackedClosedRelease", "clearOpenedUpfront"}

Batches == 1..MaxBatches

VARIABLES
  closed,         \* BatchClaims._closed
  liveBatch,      \* 0 (none) or b \in Batches
  opened,         \* Subset of Batches tracked in BatchClaims._opened
  releasingSet,   \* Subset of Batches currently iterating inside release_all()
  batchState,     \* [Batches -> {"UNUSED", "RENEWING", "STOPPED_ERR", "RELEASED"}]
  nextBatchId,    \* 1..(MaxBatches + 1)
  wPc,            \* [Workers -> {"IDLE", "CLAIMING_BATCH", "CLOSING_RACED_BATCH", "ACQUIRING_MEMBER", "DONE"}]
  wBatch          \* [Workers -> 0..MaxBatches]

vars == <<closed, liveBatch, opened, releasingSet, batchState, nextBatchId,
          wPc, wBatch>>

UntrackedClosedBug == "untrackedClosedRelease" \in Bugs
ClearOpenedUpfrontBug == "clearOpenedUpfront" \in Bugs

Init ==
  /\ closed = FALSE
  /\ liveBatch = 0
  /\ opened = {}
  /\ releasingSet = {}
  /\ batchState = [b \in Batches |-> "UNUSED"]
  /\ nextBatchId = 1
  /\ wPc = [w \in Workers |-> "IDLE"]
  /\ wBatch = [w \in Workers |-> 0]

\* Worker enters _batch_for: reuses healthy liveBatch or starts claim_batch([]).
WorkerStartBatchFor(w) ==
  /\ wPc[w] = "IDLE"
  /\ ~closed
  /\ IF liveBatch /= 0 /\ batchState[liveBatch] = "RENEWING"
     THEN /\ wPc' = [wPc EXCEPT ![w] = "ACQUIRING_MEMBER"]
          /\ wBatch' = [wBatch EXCEPT ![w] = liveBatch]
          /\ UNCHANGED <<batchState, nextBatchId>>
     ELSE /\ nextBatchId <= MaxBatches
          /\ LET b == nextBatchId
             IN /\ nextBatchId' = nextBatchId + 1
                /\ batchState' = [batchState EXCEPT ![b] = "RENEWING"]
                /\ wBatch' = [wBatch EXCEPT ![w] = b]
                /\ wPc' = [wPc EXCEPT ![w] = "CLAIMING_BATCH"]
  /\ UNCHANGED <<closed, liveBatch, opened, releasingSet>>

\* Worker finishes claim_batch([]) and takes self._lock in _batch_for.
WorkerCommitBatchFor(w) ==
  /\ wPc[w] = "CLAIMING_BATCH"
  /\ LET b == wBatch[w]
     IN IF closed
        THEN /\ opened' = IF UntrackedClosedBug THEN opened ELSE opened \cup {b}
             /\ wPc' = [wPc EXCEPT ![w] = "CLOSING_RACED_BATCH"]
             /\ UNCHANGED liveBatch
        ELSE /\ opened' = opened \cup {b}
             /\ liveBatch' = b
             /\ wPc' = [wPc EXCEPT ![w] = "ACQUIRING_MEMBER"]
  /\ UNCHANGED <<closed, releasingSet, batchState, nextBatchId, wBatch>>

\* Worker runs release on the batch that lost the race with release_all().
WorkerFinishRacedReleaseOk(w) ==
  /\ wPc[w] = "CLOSING_RACED_BATCH"
  /\ LET b == wBatch[w]
     IN /\ batchState' = [batchState EXCEPT ![b] = "RELEASED"]
        /\ opened' = opened \ {b}
  /\ wPc' = [wPc EXCEPT ![w] = "DONE"]
  /\ UNCHANGED <<closed, liveBatch, releasingSet, nextBatchId, wBatch>>

WorkerFinishRacedReleaseTransientErr(w) ==
  /\ wPc[w] = "CLOSING_RACED_BATCH"
  /\ wPc' = [wPc EXCEPT ![w] = "DONE"]
  /\ UNCHANGED <<closed, liveBatch, opened, releasingSet, batchState,
                 nextBatchId, wBatch>>

\* Worker acquires a member or observes batch error and finishes step.
WorkerFinishAcquire(w) ==
  /\ wPc[w] = "ACQUIRING_MEMBER"
  /\ wPc' = [wPc EXCEPT ![w] = "DONE"]
  /\ UNCHANGED <<closed, liveBatch, opened, releasingSet, batchState,
                 nextBatchId, wBatch>>

\* Live batch encounters an error, rotating liveBatch on next _batch_for.
LiveBatchErrors ==
  /\ liveBatch /= 0
  /\ batchState[liveBatch] = "RENEWING"
  /\ batchState' = [batchState EXCEPT ![liveBatch] = "STOPPED_ERR"]
  /\ UNCHANGED <<closed, liveBatch, opened, releasingSet, nextBatchId,
                 wPc, wBatch>>

\* Caller enters BatchClaims.release_all(): snapshots opened into releasingSet.
\* Under ClearOpenedUpfrontBug (batch_claims.py:186), self._opened clears upfront!
StartReleaseAll ==
  /\ releasingSet = {}
  /\ closed' = TRUE
  /\ liveBatch' = 0
  /\ releasingSet' = opened
  /\ opened' = IF ClearOpenedUpfrontBug THEN {} ELSE opened
  /\ UNCHANGED <<batchState, nextBatchId, wPc, wBatch>>

\* One iteration of release_all() succeeds on batch b.
ReleaseBatchStepOk(b) ==
  /\ b \in releasingSet
  /\ batchState' = [batchState EXCEPT ![b] = "RELEASED"]
  /\ releasingSet' = releasingSet \ {b}
  /\ opened' = opened \ {b}
  /\ UNCHANGED <<closed, liveBatch, nextBatchId, wPc, wBatch>>

\* One iteration of release_all() hits a transient 503 on batch b and logs a warning.
ReleaseBatchStepTransientErr(b) ==
  /\ b \in releasingSet
  /\ releasingSet' = releasingSet \ {b}
  /\ UNCHANGED <<closed, liveBatch, opened, batchState, nextBatchId,
                 wPc, wBatch>>

\* Fleet setup() calls reopen() once prior workers and release_all() finished.
Reopen ==
  /\ closed
  /\ releasingSet = {}
  /\ \A w \in Workers : wPc[w] \in {"IDLE", "DONE"}
  /\ closed' = FALSE
  /\ UNCHANGED <<liveBatch, opened, releasingSet, batchState, nextBatchId,
                 wPc, wBatch>>

Next ==
  \/ \E w \in Workers : WorkerStartBatchFor(w)
  \/ \E w \in Workers : WorkerCommitBatchFor(w)
  \/ \E w \in Workers : WorkerFinishRacedReleaseOk(w)
  \/ \E w \in Workers : WorkerFinishRacedReleaseTransientErr(w)
  \/ \E w \in Workers : WorkerFinishAcquire(w)
  \/ LiveBatchErrors
  \/ StartReleaseAll
  \/ \E b \in Batches : ReleaseBatchStepOk(b)
  \/ \E b \in Batches : ReleaseBatchStepTransientErr(b)
  \/ Reopen

Spec == Init /\ [][Next]_vars

\* ---------------------------------------------------------------------------
\* Invariants
\* ---------------------------------------------------------------------------

\* Closed BatchClaims never exposes a live batch in _live.
ClosedPreventsLiveInstall ==
  closed => liveBatch = 0

\* INV-LEAK-1: Every RENEWING batch that is not currently held by an in-flight
\* worker or an active release_all() iteration remains tracked in _opened so a
\* subsequent release_all() retry can release it. Violated by both
\* UntrackedClosedBug and ClearOpenedUpfrontBug!
NoOrphanedRenewingBatch ==
  \A b \in Batches :
    (/\ batchState[b] = "RENEWING"
     /\ b \notin releasingSet
     /\ ~\E w \in Workers : wPc[w] \in {"CLAIMING_BATCH", "CLOSING_RACED_BATCH"} /\ wBatch[w] = b)
      => b \in opened

=============================================================================
