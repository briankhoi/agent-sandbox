------------------------ MODULE BatchAcquireRelease ------------------------
\* Formal TLA+ specification of concurrent initial fill, dynamic acquire()
\* (including split HTTP POST vs BatchRelease barrier), release_member(),
\* release_not_ready(), release_group() (_pool_release_seq), and monotonic
\* _pending_deletes sequence numbers (batch_state.py, sandbox_batch.py,
\* async_sandbox_batch.py).
\*
\* Checks invariants:
\*   - NoForeignGroupSurvival: an acquire() that began before or during
\*     release_group(pool) never returns a live member after release_group
\*     completes for that pool.
\*   - DispatchedProtectedFromReleaseNotReady: release_not_ready() never deletes
\*     a fill member that was already dispatched to a caller, even if its
\*     cluster readiness later flaps to False.
\*   - NoPendingDeleteLost (INV-OWN-4): an older in-flight delete completing
\*     after a retry re-created a claim never clears a newer _pending_deletes
\*     sequence number.
\*   - NoOrphanedClaimAfterBatchRelease (INV-LEAK-1 / F-24): an in-flight
\*     acquire() HTTP POST never escapes batch.release() to leave an orphaned
\*     SandboxClaim live on the cluster after release() completes.

EXTENDS Integers, FiniteSets

MaxSeq == 2

CONSTANTS
  Pools,     \* e.g. {"p1", "p2"}
  Bugs       \* Subset of {"unsequencedPendingDelete", "ignorePoolReleaseSeq",
             \*            "untrackedAcquireInRelease"}

Claims == {"f1", "a1"}  \* f1 = initial fill in p1; a1 = dynamic acquire in p1

VARIABLES
  batchDetached,         \* BOOLEAN: batch.release() completed
  poolRelSeq,            \* [Pools -> Nat]
  fillState,             \* "STARTING" | "DISPATCHED" | "RELEASED"
  fillClusterReady,      \* BOOLEAN: cluster readiness of f1 (can flap after dispatch)
  acqPhase,              \* "IDLE" | "CHECKING" | "POST_IN_FLIGHT" | "CREATING"
                         \* | "READY_RETURNED" | "ABORTED"
  acqPool,               \* Pool targeted by dynamic acquire
  acqStartSeq,           \* poolRelSeq[acqPool] observed when acquire() entered
  clusterExists,         \* [Claims -> BOOLEAN]
  pendingDelSeq,         \* [Claims -> Nat] (0 = none pending)
  nextDelSeq,            \* Nat monotonic counter
  inFlightDelSeq,        \* [Claims -> Nat] (0 = no delete in flight)
  groupReleasedAfterAcq  \* Ghost flag: TRUE if release_group ran after acquire started

vars == <<batchDetached, poolRelSeq, fillState, fillClusterReady, acqPhase,
          acqPool, acqStartSeq, clusterExists, pendingDelSeq, nextDelSeq,
          inFlightDelSeq, groupReleasedAfterAcq>>

UnsequencedBug == "unsequencedPendingDelete" \in Bugs
IgnorePoolSeqBug == "ignorePoolReleaseSeq" \in Bugs
UntrackedAcquireBug == "untrackedAcquireInRelease" \in Bugs

Init ==
  /\ batchDetached = FALSE
  /\ poolRelSeq = [p \in Pools |-> 0]
  /\ fillState = "STARTING"
  /\ fillClusterReady = FALSE
  /\ acqPhase = "IDLE"
  /\ acqPool = CHOOSE p \in Pools : TRUE
  /\ acqStartSeq = 0
  /\ clusterExists = [c \in Claims |-> c = "f1"]
  /\ pendingDelSeq = [c \in Claims |-> 0]
  /\ nextDelSeq = 0
  /\ inFlightDelSeq = [c \in Claims |-> 0]
  /\ groupReleasedAfterAcq = FALSE

\* Fill claim f1 turns Ready on cluster and is dispatched to caller.
FillTurnReadyAndDispatch ==
  /\ ~batchDetached
  /\ fillState = "STARTING"
  /\ fillState' = "DISPATCHED"
  /\ fillClusterReady' = TRUE
  /\ UNCHANGED <<batchDetached, poolRelSeq, acqPhase, acqPool, acqStartSeq,
                 clusterExists, pendingDelSeq, nextDelSeq, inFlightDelSeq,
                 groupReleasedAfterAcq>>

\* Dispatched fill claim f1 flaps back to not-Ready on cluster, but stays DISPATCHED.
DispatchedFillFlaps ==
  /\ ~batchDetached
  /\ fillState = "DISPATCHED"
  /\ fillClusterReady
  /\ fillClusterReady' = FALSE
  /\ UNCHANGED <<batchDetached, poolRelSeq, fillState, acqPhase, acqPool,
                 acqStartSeq, clusterExists, pendingDelSeq, nextDelSeq,
                 inFlightDelSeq, groupReleasedAfterAcq>>

\* release_not_ready() releases fill claims that are not READY and never dispatched.
ReleaseNotReady ==
  /\ ~batchDetached
  /\ nextDelSeq < MaxSeq
  /\ fillState = "STARTING"
  /\ fillState' = "RELEASED"
  /\ fillClusterReady' = FALSE
  /\ nextDelSeq' = nextDelSeq + 1
  /\ pendingDelSeq' = [pendingDelSeq EXCEPT !["f1"] = nextDelSeq + 1]
  /\ UNCHANGED <<batchDetached, poolRelSeq, acqPhase, acqPool, acqStartSeq,
                 clusterExists, inFlightDelSeq, groupReleasedAfterAcq>>

\* Dynamic acquire(p) enters _checking_acquires and snapshots poolRelSeq[p].
AcquireEnter(p) ==
  /\ ~batchDetached
  /\ acqPhase = "IDLE"
  /\ acqPhase' = "CHECKING"
  /\ acqPool' = p
  /\ acqStartSeq' = poolRelSeq[p]
  /\ groupReleasedAfterAcq' = FALSE
  /\ UNCHANGED <<batchDetached, poolRelSeq, fillState, fillClusterReady,
                 clusterExists, pendingDelSeq, nextDelSeq, inFlightDelSeq>>

\* Dynamic acquire(p) reserves ordinal a1 and starts HTTP POST create_sandbox_claim.
AcquireStartCreate ==
  /\ ~batchDetached
  /\ acqPhase = "CHECKING"
  /\ IF ~IgnorePoolSeqBug /\ poolRelSeq[acqPool] /= acqStartSeq
     THEN acqPhase' = "ABORTED"
     ELSE acqPhase' = "POST_IN_FLIGHT"
  /\ UNCHANGED <<batchDetached, poolRelSeq, fillState, fillClusterReady,
                 acqPool, acqStartSeq, clusterExists, pendingDelSeq,
                 nextDelSeq, inFlightDelSeq, groupReleasedAfterAcq>>

\* In-flight HTTP POST create_sandbox_claim lands on the apiserver.
AcquireCommitCreate ==
  /\ acqPhase = "POST_IN_FLIGHT"
  /\ clusterExists' = [clusterExists EXCEPT !["a1"] = TRUE]
  /\ IF batchDetached
     THEN /\ acqPhase' = "ABORTED"
          /\ UNCHANGED <<nextDelSeq, pendingDelSeq>>
     ELSE IF ~IgnorePoolSeqBug /\ poolRelSeq[acqPool] /= acqStartSeq
     THEN /\ acqPhase' = "ABORTED"
          /\ IF nextDelSeq < MaxSeq
             THEN /\ nextDelSeq' = nextDelSeq + 1
                  /\ pendingDelSeq' = [pendingDelSeq EXCEPT !["a1"] = nextDelSeq + 1]
             ELSE UNCHANGED <<nextDelSeq, pendingDelSeq>>
     ELSE /\ acqPhase' = "CREATING"
          /\ UNCHANGED <<nextDelSeq, pendingDelSeq>>
  /\ UNCHANGED <<batchDetached, poolRelSeq, fillState, fillClusterReady,
                 acqPool, acqStartSeq, inFlightDelSeq, groupReleasedAfterAcq>>

\* Late HTTP 201 retry recreates claim a1 while a previous delete was in flight,
\* and caller cleanup marks a newer pending delete sequence.
CreateRetryLandsAfterRelease ==
  /\ ~batchDetached
  /\ acqPhase = "ABORTED"
  /\ inFlightDelSeq["a1"] > 0
  /\ nextDelSeq < MaxSeq
  /\ clusterExists' = [clusterExists EXCEPT !["a1"] = TRUE]
  /\ nextDelSeq' = nextDelSeq + 1
  /\ pendingDelSeq' = [pendingDelSeq EXCEPT !["a1"] = nextDelSeq + 1]
  /\ UNCHANGED <<batchDetached, poolRelSeq, fillState, fillClusterReady,
                 acqPhase, acqPool, acqStartSeq, inFlightDelSeq,
                 groupReleasedAfterAcq>>

\* Dynamic acquire finishes waiting when claim a1 becomes Ready.
AcquireReturnReady ==
  /\ ~batchDetached
  /\ acqPhase = "CREATING"
  /\ acqPhase' = "READY_RETURNED"
  /\ UNCHANGED <<batchDetached, poolRelSeq, fillState, fillClusterReady,
                 acqPool, acqStartSeq, clusterExists, pendingDelSeq,
                 nextDelSeq, inFlightDelSeq, groupReleasedAfterAcq>>

\* release_group(p) increments poolRelSeq[p] and marks any acquiring claim released.
ReleaseGroup(p) ==
  /\ ~batchDetached
  /\ poolRelSeq[p] < MaxSeq
  /\ nextDelSeq < MaxSeq
  /\ poolRelSeq' = [poolRelSeq EXCEPT ![p] = poolRelSeq[p] + 1]
  /\ groupReleasedAfterAcq' = (groupReleasedAfterAcq \/
                               (acqPhase \in {"CHECKING", "POST_IN_FLIGHT", "CREATING"} /\ acqPool = p))
  /\ IF acqPhase = "CREATING" /\ acqPool = p
     THEN /\ nextDelSeq' = nextDelSeq + 1
          /\ pendingDelSeq' = [pendingDelSeq EXCEPT !["a1"] = nextDelSeq + 1]
          /\ acqPhase' = IF IgnorePoolSeqBug THEN acqPhase ELSE "ABORTED"
     ELSE /\ UNCHANGED <<nextDelSeq, pendingDelSeq, acqPhase>>
  /\ UNCHANGED <<batchDetached, fillState, fillClusterReady, acqPool,
                 acqStartSeq, clusterExists, inFlightDelSeq>>

\* batch.release() drains active deletes (and in-flight acquire POSTs unless
\* UntrackedAcquireBug [F-24]), deletes all live batch claims by label, and
\* sets batchDetached = TRUE.
BatchRelease ==
  /\ ~batchDetached
  /\ \A c \in Claims : inFlightDelSeq[c] = 0
  /\ (UntrackedAcquireBug \/ acqPhase /= "POST_IN_FLIGHT")
  /\ batchDetached' = TRUE
  /\ clusterExists' = [c \in Claims |-> FALSE]
  /\ pendingDelSeq' = [c \in Claims |-> 0]
  /\ fillState' = IF fillState = "STARTING" THEN "RELEASED" ELSE fillState
  /\ acqPhase' = IF acqPhase \in {"CHECKING", "CREATING"} THEN "ABORTED" ELSE acqPhase
  /\ UNCHANGED <<poolRelSeq, fillClusterReady, acqPool, acqStartSeq,
                 nextDelSeq, inFlightDelSeq, groupReleasedAfterAcq>>

\* Start an HTTP DELETE for claim c capturing current pendingDelSeq[c].
StartDeleteHttp(c) ==
  /\ ~batchDetached
  /\ pendingDelSeq[c] > 0
  /\ inFlightDelSeq[c] = 0
  /\ inFlightDelSeq' = [inFlightDelSeq EXCEPT ![c] = pendingDelSeq[c]]
  /\ clusterExists' = [clusterExists EXCEPT ![c] = FALSE]
  /\ UNCHANGED <<batchDetached, poolRelSeq, fillState, fillClusterReady,
                 acqPhase, acqPool, acqStartSeq, pendingDelSeq, nextDelSeq,
                 groupReleasedAfterAcq>>

\* Complete an in-flight HTTP DELETE: note_deleted(c, seq) clears pendingDelSeq[c]
\* ONLY when pendingDelSeq[c] == seq (unless UnsequencedBug clears unconditionally).
FinishDeleteHttp(c) ==
  /\ inFlightDelSeq[c] > 0
  /\ LET seq == inFlightDelSeq[c]
     IN pendingDelSeq' = [pendingDelSeq EXCEPT ![c] =
                           IF UnsequencedBug \/ pendingDelSeq[c] = seq
                           THEN 0
                           ELSE pendingDelSeq[c]]
  /\ inFlightDelSeq' = [inFlightDelSeq EXCEPT ![c] = 0]
  /\ UNCHANGED <<batchDetached, poolRelSeq, fillState, fillClusterReady,
                 acqPhase, acqPool, acqStartSeq, clusterExists, nextDelSeq,
                 groupReleasedAfterAcq>>

\* Terminal horizon stuttering once batch is released (or settled) and no HTTP
\* POST or DELETE remains in flight.
IdleAtHorizon ==
  /\ acqPhase /= "POST_IN_FLIGHT"
  /\ \A c \in Claims : inFlightDelSeq[c] = 0
  /\ (\/ batchDetached
      \/ (fillState \in {"DISPATCHED", "RELEASED"}
          /\ acqPhase \in {"IDLE", "READY_RETURNED", "ABORTED"}
          /\ \A c \in Claims : pendingDelSeq[c] = 0))
  /\ UNCHANGED vars

Next ==
  \/ FillTurnReadyAndDispatch
  \/ DispatchedFillFlaps
  \/ ReleaseNotReady
  \/ \E p \in Pools : AcquireEnter(p)
  \/ AcquireStartCreate
  \/ AcquireCommitCreate
  \/ CreateRetryLandsAfterRelease
  \/ AcquireReturnReady
  \/ \E p \in Pools : ReleaseGroup(p)
  \/ BatchRelease
  \/ \E c \in Claims : StartDeleteHttp(c)
  \/ \E c \in Claims : FinishDeleteHttp(c)
  \/ IdleAtHorizon

Spec == Init /\ [][Next]_vars

\* ---------------------------------------------------------------------------
\* Invariants
\* ---------------------------------------------------------------------------

\* An acquire() whose target pool was released by release_group() while in flight
\* must never return READY_RETURNED. Violated by IgnorePoolSeqBug!
NoForeignGroupSurvival ==
  groupReleasedAfterAcq => acqPhase /= "READY_RETURNED"

\* INV-OWN-4: If a recreated claim exists on the cluster after release/abort and
\* no delete is currently in flight, pendingDelSeq[c] must remain > 0 so cleanup
\* deletes it. Violated by UnsequencedBug!
NoPendingDeleteLost ==
  (~batchDetached /\ acqPhase = "ABORTED" /\ clusterExists["a1"] /\ inFlightDelSeq["a1"] = 0 /\ nextDelSeq > 0)
    => pendingDelSeq["a1"] > 0

\* Dispatched fill member is never released by release_not_ready(), even after
\* DispatchedFillFlaps sets fillClusterReady = FALSE.
DispatchedProtectedFromReleaseNotReady ==
  fillState = "DISPATCHED" => pendingDelSeq["f1"] = 0

\* INV-LEAK-1 (F-24): Once batch.release() completes and any in-flight HTTP POST
\* settles, no dynamic claim remains orphaned on the cluster. Violated by
\* UntrackedAcquireBug!
NoOrphanedClaimAfterBatchRelease ==
  (batchDetached /\ acqPhase /= "POST_IN_FLIGHT") => ~clusterExists["a1"]

=============================================================================
