--------------------------- MODULE BatchConsumer ---------------------------
\* Formal TLA+ specification of BatchState consumer modes, settlement,
\* group_outcomes(), wait_for_quorum(), events() (including post-quorum and
\* post-group composition), popped pacing-slot release_group() cancellation,
\* dynamic ordinal allocation across get_batch() reattachment, and fill timeout
\* accounting (batch_state.py, sandbox_batch.py).
\*
\* Checks invariants:
\*   - ResolvedPartitionAccounting (INV-ACCT-1 / F-12)
\*   - NoFailedGroupMemberDispatched (INV-ACCT-2)
\*   - MonotonicUniqueOrdinals (INV-ACCT-3 / get_batch ordinal recovery)
\*   - NoStrandedWaitingAfterOutcome (INV-ACCT-3)
\*   - ReleasedPoolImmediatelySettled (INV-ACCT-2 / F-25 popped pacing slots)

EXTENDS Integers, FiniteSets

CONSTANTS
  Pools,          \* e.g. {"p1", "p2"}
  MembersPerPool, \* e.g. 3
  MinReady,       \* e.g. 2
  MaxAcquires,    \* e.g. 1
  Bugs            \* Subset of {"omitResolvedOnTimeout", "poppedSlotMissesRelease",
                  \*            "staleReattachOrdinal"}

FillClaims == {<<p, i>> : p \in Pools, i \in 0..(MembersPerPool - 1)}
InitialSize == Cardinality(FillClaims)

VARIABLES
  mode,             \* "UNSET" | "QUORUM" | "GROUP" | "STREAM"
  quorumCalled,     \* BOOLEAN: wait_for_quorum() single-call enforcement
  cState,           \* [FillClaims -> {"STARTING", "READY", "FAILED"}]
  planPopped,       \* SUBSET FillClaims: popped by _create_worker into pacing sleep
  releasedPools,    \* SUBSET Pools: pools released via release_group()
  waiting,          \* [Pools -> SUBSET FillClaims]
  dispatched,       \* SUBSET FillClaims
  unable,           \* SUBSET FillClaims
  resolved,         \* SUBSET FillClaims
  groupOutcome,     \* [Pools -> {"UNDECIDED", "OK", "ERROR"}]
  quorumState,      \* "UNDECIDED" | "OK" | "ERROR"
  fillExpired,      \* BOOLEAN
  reattached,       \* BOOLEAN: detach() + get_batch() occurred
  ordinalCollision, \* BOOLEAN: duplicate ordinal allocated
  nextOrdinal,      \* Integer >= InitialSize
  usedOrdinals      \* Set of allocated ordinals

vars == <<mode, quorumCalled, cState, planPopped, releasedPools, waiting,
          dispatched, unable, resolved, groupOutcome, quorumState,
          fillExpired, reattached, ordinalCollision, nextOrdinal, usedOrdinals>>

OmitResolvedBug == "omitResolvedOnTimeout" \in Bugs
PoppedSlotBug == "poppedSlotMissesRelease" \in Bugs
StaleReattachBug == "staleReattachOrdinal" \in Bugs

PoolClaims(p) == {c \in FillClaims : c[1] = p}

CanStillReach(p) ==
  MembersPerPool - Cardinality({c \in PoolClaims(p) : c \in unable}) >= MinReady

Init ==
  /\ mode = "UNSET"
  /\ quorumCalled = FALSE
  /\ cState = [c \in FillClaims |-> "STARTING"]
  /\ planPopped = {}
  /\ releasedPools = {}
  /\ waiting = [p \in Pools |-> {}]
  /\ dispatched = {}
  /\ unable = {}
  /\ resolved = {}
  /\ groupOutcome = [p \in Pools |-> "UNDECIDED"]
  /\ quorumState = "UNDECIDED"
  /\ fillExpired = FALSE
  /\ reattached = FALSE
  /\ ordinalCollision = FALSE
  /\ nextOrdinal = InitialSize
  /\ usedOrdinals = 0..(InitialSize - 1)

SetModeQuorum ==
  /\ mode = "UNSET"
  /\ ~quorumCalled
  /\ mode' = "QUORUM"
  /\ quorumCalled' = TRUE
  /\ IF \A p \in Pools : Cardinality(waiting[p]) >= MinReady
     THEN /\ dispatched' = dispatched \cup UNION {waiting[p] : p \in Pools}
          /\ resolved' = resolved \cup UNION {waiting[p] : p \in Pools}
          /\ waiting' = [p \in Pools |-> {}]
          /\ quorumState' = "OK"
     ELSE IF (\E p \in Pools : ~CanStillReach(p)) \/ fillExpired
     THEN /\ resolved' = resolved \cup UNION {waiting[p] : p \in Pools}
          /\ waiting' = [p \in Pools |-> {}]
          /\ quorumState' = "ERROR"
          /\ UNCHANGED dispatched
     ELSE UNCHANGED <<waiting, dispatched, resolved, quorumState>>
  /\ UNCHANGED <<cState, planPopped, releasedPools, unable, groupOutcome,
                 fillExpired, reattached, ordinalCollision, nextOrdinal,
                 usedOrdinals>>

SetModeGroup ==
  /\ mode \in {"UNSET", "GROUP"}
  /\ mode' = "GROUP"
  /\ LET w1 == [p \in Pools |->
                 IF groupOutcome[p] /= "UNDECIDED" THEN waiting[p]
                 ELSE IF Cardinality(waiting[p]) >= MinReady THEN {}
                 ELSE IF ~CanStillReach(p) \/ fillExpired THEN {}
                 ELSE waiting[p]]
         d1 == dispatched \cup UNION {
                 IF groupOutcome[p] = "UNDECIDED" /\ Cardinality(waiting[p]) >= MinReady
                 THEN waiting[p] ELSE {} : p \in Pools}
         r1 == resolved \cup UNION {
                 IF groupOutcome[p] /= "UNDECIDED" THEN {}
                 ELSE IF Cardinality(waiting[p]) >= MinReady THEN waiting[p]
                 ELSE IF ~CanStillReach(p) THEN waiting[p]
                 ELSE IF fillExpired /\ ~OmitResolvedBug THEN waiting[p]
                 ELSE {} : p \in Pools}
         g1 == [p \in Pools |->
                 IF groupOutcome[p] /= "UNDECIDED" THEN groupOutcome[p]
                 ELSE IF Cardinality(waiting[p]) >= MinReady THEN "OK"
                 ELSE IF ~CanStillReach(p) \/ fillExpired THEN "ERROR"
                 ELSE "UNDECIDED"]
     IN /\ waiting' = w1
        /\ dispatched' = d1
        /\ resolved' = r1
        /\ groupOutcome' = g1
  /\ UNCHANGED <<quorumCalled, cState, planPopped, releasedPools, unable,
                 quorumState, fillExpired, reattached, ordinalCollision,
                 nextOrdinal, usedOrdinals>>

\* events() may be entered from UNSET or composed after wait_for_quorum() /
\* group_outcomes() have finished deciding their cohorts.
SetModeStream ==
  /\ \/ mode = "UNSET"
     \/ (mode = "QUORUM" /\ quorumState /= "UNDECIDED")
     \/ (mode = "GROUP" /\ \A p \in Pools : groupOutcome[p] /= "UNDECIDED")
  /\ mode /= "STREAM"
  /\ mode' = "STREAM"
  /\ dispatched' = dispatched \cup UNION {waiting[p] : p \in Pools}
  /\ resolved' = resolved \cup UNION {waiting[p] : p \in Pools}
  /\ waiting' = [p \in Pools |-> {}]
  /\ UNCHANGED <<quorumCalled, cState, planPopped, releasedPools, unable,
                 groupOutcome, quorumState, fillExpired, reattached,
                 ordinalCollision, nextOrdinal, usedOrdinals>>

\* _create_worker pops a representative planned fill slot into pacing sleep.
PopPacingSlot(c) ==
  /\ c[2] = 0
  /\ cState[c] = "STARTING"
  /\ c \notin planPopped
  /\ c \notin unable
  /\ c[1] \notin releasedPools
  /\ planPopped' = planPopped \cup {c}
  /\ UNCHANGED <<mode, quorumCalled, cState, releasedPools, waiting,
                 dispatched, unable, resolved, groupOutcome, quorumState,
                 fillExpired, reattached, ordinalCollision, nextOrdinal,
                 usedOrdinals>>

\* release_group(p) stops fill for pool p and counts un-dispatched claims as unable.
\* Under PoppedSlotBug (F-25), claims already popped into worker pacing sleep are missed!
ReleasePoolGroup(p) ==
  /\ p \notin releasedPools
  /\ releasedPools' = releasedPools \cup {p}
  /\ LET relC == {c \in PoolClaims(p) :
                   c \notin dispatched /\ c \notin unable /\
                   ~(PoppedSlotBug /\ c \in planPopped /\ cState[c] = "STARTING")}
         uNext == unable \cup relC
         wBase == [waiting EXCEPT ![p] = waiting[p] \ relC]
         rBase == resolved \cup relC
         stillReach == MembersPerPool - Cardinality({x \in PoolClaims(p) : x \in uNext}) >= MinReady
     IN /\ cState' = [c \in FillClaims |-> IF c \in relC THEN "FAILED" ELSE cState[c]]
        /\ unable' = uNext
        /\ IF mode = "QUORUM" /\ quorumState = "UNDECIDED" /\ ~stillReach
           THEN /\ resolved' = rBase \cup UNION {wBase[q] : q \in Pools}
                /\ waiting' = [q \in Pools |-> {}]
                /\ quorumState' = "ERROR"
                /\ UNCHANGED groupOutcome
           ELSE IF mode = "GROUP" /\ groupOutcome[p] = "UNDECIDED" /\ ~stillReach
           THEN /\ resolved' = rBase \cup wBase[p]
                /\ waiting' = [wBase EXCEPT ![p] = {}]
                /\ groupOutcome' = [groupOutcome EXCEPT ![p] = "ERROR"]
                /\ UNCHANGED quorumState
           ELSE /\ resolved' = rBase
                /\ waiting' = wBase
                /\ UNCHANGED <<groupOutcome, quorumState>>
  /\ UNCHANGED <<mode, quorumCalled, planPopped, dispatched, fillExpired,
                 reattached, ordinalCollision, nextOrdinal, usedOrdinals>>

\* Fill claim c becomes READY.
MemberTurnReady(c) ==
  /\ cState[c] = "STARTING"
  /\ c \notin unable
  /\ c[1] \notin releasedPools
  /\ LET p == c[1]
     IN /\ cState' = [cState EXCEPT ![c] = "READY"]
        /\ IF mode = "STREAM"
           THEN /\ dispatched' = dispatched \cup {c}
                /\ resolved' = resolved \cup {c}
                /\ UNCHANGED <<waiting, groupOutcome, quorumState>>
           ELSE IF mode = "QUORUM"
           THEN IF quorumState = "OK"
                THEN /\ dispatched' = dispatched \cup {c}
                     /\ resolved' = resolved \cup {c}
                     /\ UNCHANGED <<waiting, groupOutcome, quorumState>>
                ELSE IF quorumState = "ERROR"
                THEN /\ resolved' = resolved \cup {c}
                     /\ UNCHANGED <<waiting, dispatched, groupOutcome, quorumState>>
                ELSE LET wNext == [waiting EXCEPT ![p] = waiting[p] \cup {c}]
                     IN IF \A q \in Pools : Cardinality(wNext[q]) >= MinReady
                        THEN /\ waiting' = [q \in Pools |-> {}]
                             /\ dispatched' = dispatched \cup UNION {wNext[q] : q \in Pools}
                             /\ resolved' = resolved \cup UNION {wNext[q] : q \in Pools}
                             /\ quorumState' = "OK"
                             /\ UNCHANGED groupOutcome
                        ELSE /\ waiting' = wNext
                             /\ UNCHANGED <<dispatched, resolved, groupOutcome, quorumState>>
           ELSE IF mode = "GROUP"
           THEN IF groupOutcome[p] = "OK"
                THEN /\ dispatched' = dispatched \cup {c}
                     /\ resolved' = resolved \cup {c}
                     /\ UNCHANGED <<waiting, groupOutcome, quorumState>>
                ELSE IF groupOutcome[p] = "ERROR"
                THEN /\ resolved' = resolved \cup {c}
                     /\ UNCHANGED <<waiting, dispatched, groupOutcome, quorumState>>
                ELSE LET wNext == [waiting EXCEPT ![p] = waiting[p] \cup {c}]
                     IN IF Cardinality(wNext[p]) >= MinReady
                        THEN /\ waiting' = [wNext EXCEPT ![p] = {}]
                             /\ dispatched' = dispatched \cup wNext[p]
                             /\ resolved' = resolved \cup wNext[p]
                             /\ groupOutcome' = [groupOutcome EXCEPT ![p] = "OK"]
                             /\ UNCHANGED quorumState
                        ELSE /\ waiting' = wNext
                             /\ UNCHANGED <<dispatched, resolved, groupOutcome, quorumState>>
           ELSE \* mode = "UNSET"
                /\ waiting' = [waiting EXCEPT ![p] = waiting[p] \cup {c}]
                /\ UNCHANGED <<dispatched, resolved, groupOutcome, quorumState>>
  /\ UNCHANGED <<mode, quorumCalled, planPopped, releasedPools, unable,
                 fillExpired, reattached, ordinalCollision, nextOrdinal,
                 usedOrdinals>>

\* Fill claim c fails or is lost (terminal unable state).
MemberFailOrLost(c) ==
  /\ cState[c] \in {"STARTING", "READY"}
  /\ c \notin unable
  /\ LET p == c[1]
         uNext == unable \cup {c}
         wBase == [waiting EXCEPT ![p] = waiting[p] \ {c}]
         rBase == resolved \cup {c}
         stillReach == MembersPerPool - Cardinality({x \in PoolClaims(p) : x \in uNext}) >= MinReady
     IN /\ cState' = [cState EXCEPT ![c] = "FAILED"]
        /\ unable' = uNext
        /\ IF mode = "QUORUM" /\ quorumState = "UNDECIDED" /\ ~stillReach
           THEN /\ resolved' = rBase \cup UNION {wBase[q] : q \in Pools}
                /\ waiting' = [q \in Pools |-> {}]
                /\ quorumState' = "ERROR"
                /\ UNCHANGED groupOutcome
           ELSE IF mode = "GROUP" /\ groupOutcome[p] = "UNDECIDED" /\ ~stillReach
           THEN /\ resolved' = rBase \cup wBase[p]
                /\ waiting' = [wBase EXCEPT ![p] = {}]
                /\ groupOutcome' = [groupOutcome EXCEPT ![p] = "ERROR"]
                /\ UNCHANGED quorumState
           ELSE /\ resolved' = rBase
                /\ waiting' = wBase
                /\ UNCHANGED <<groupOutcome, quorumState>>
  /\ UNCHANGED <<mode, quorumCalled, planPopped, releasedPools, dispatched,
                 fillExpired, reattached, ordinalCollision, nextOrdinal,
                 usedOrdinals>>

\* Fill deadline expires (expire_fill -> _time_out_undecided_groups / fail_quorum).
ExpireFill ==
  /\ ~fillExpired
  /\ fillExpired' = TRUE
  /\ IF mode = "GROUP"
     THEN /\ groupOutcome' = [p \in Pools |->
                               IF groupOutcome[p] = "UNDECIDED" THEN "ERROR"
                               ELSE groupOutcome[p]]
          /\ resolved' = IF OmitResolvedBug
                         THEN resolved
                         ELSE resolved \cup UNION {
                                IF groupOutcome[p] = "UNDECIDED" THEN waiting[p] ELSE {} : p \in Pools}
          /\ waiting' = [p \in Pools |->
                          IF groupOutcome[p] = "UNDECIDED" THEN {} ELSE waiting[p]]
          /\ UNCHANGED quorumState
     ELSE IF mode = "QUORUM" /\ quorumState = "UNDECIDED"
     THEN /\ quorumState' = "ERROR"
          /\ resolved' = resolved \cup UNION {waiting[p] : p \in Pools}
          /\ waiting' = [p \in Pools |-> {}]
          /\ UNCHANGED groupOutcome
     ELSE UNCHANGED <<waiting, resolved, groupOutcome, quorumState>>
  /\ UNCHANGED <<mode, quorumCalled, cState, planPopped, releasedPools,
                 dispatched, unable, reattached, ordinalCollision, nextOrdinal,
                 usedOrdinals>>

\* Dynamic acquire() allocates a fresh monotonic ordinal (before and after reattach).
DynamicAcquire ==
  /\ MaxAcquires > 0
  /\ ~ordinalCollision
  /\ (\/ (~reattached /\ nextOrdinal < InitialSize + MaxAcquires)
      \/ (reattached /\ Cardinality(usedOrdinals) < InitialSize + MaxAcquires + 1))
  /\ ordinalCollision' = (ordinalCollision \/ (nextOrdinal \in usedOrdinals))
  /\ usedOrdinals' = usedOrdinals \cup {nextOrdinal}
  /\ nextOrdinal' = nextOrdinal + 1
  /\ UNCHANGED <<mode, quorumCalled, cState, planPopped, releasedPools,
                 waiting, dispatched, unable, resolved, groupOutcome,
                 quorumState, fillExpired, reattached>>

\* detach() persists BATCH_NEXT_ORDINAL_ANNOTATION and get_batch() restores it.
ReattachBatch ==
  /\ ~reattached
  /\ MaxAcquires > 0
  /\ nextOrdinal = InitialSize + MaxAcquires
  /\ reattached' = TRUE
  /\ nextOrdinal' = IF StaleReattachBug THEN InitialSize ELSE nextOrdinal
  /\ UNCHANGED <<mode, quorumCalled, cState, planPopped, releasedPools,
                 waiting, dispatched, unable, resolved, groupOutcome,
                 quorumState, fillExpired, ordinalCollision, usedOrdinals>>

\* Terminal horizon stuttering once all fill claims failed, deadline expired in
\* STREAM mode, pools released, and bounded acquires completed.
IdleAtHorizon ==
  /\ fillExpired
  /\ mode = "STREAM"
  /\ releasedPools = Pools
  /\ \A c \in FillClaims : c \in unable
  /\ (MaxAcquires = 0 \/ (reattached /\ Cardinality(usedOrdinals) = InitialSize + MaxAcquires + 1))
  /\ UNCHANGED vars

Next ==
  \/ SetModeQuorum
  \/ SetModeGroup
  \/ SetModeStream
  \/ \E c \in FillClaims : PopPacingSlot(c)
  \/ \E p \in Pools : ReleasePoolGroup(p)
  \/ \E c \in FillClaims : MemberTurnReady(c)
  \/ \E c \in FillClaims : MemberFailOrLost(c)
  \/ ExpireFill
  \/ DynamicAcquire
  \/ ReattachBatch
  \/ IdleAtHorizon

Spec == Init /\ [][Next]_vars

\* ---------------------------------------------------------------------------
\* Invariants (Section 5.3 & Master Invariant Oracle INV-ACCT-1..3)
\* ---------------------------------------------------------------------------

\* INV-ACCT-1: Every Ready fill member that is no longer held in _waiting must
\* be accounted for in _resolved. Violated by OmitResolvedBug (F-12)!
ResolvedPartitionAccounting ==
  \A c \in FillClaims :
    (cState[c] = "READY" /\ c \notin waiting[c[1]])
      => c \in resolved

\* INV-ACCT-2: Failed or timed-out groups in GROUP mode that never switched to
\* STREAM never dispatch members, and failed quorums before STREAM never dispatch.
NoFailedGroupMemberDispatched ==
  /\ (mode = "GROUP" =>
        \A p \in Pools :
          groupOutcome[p] = "ERROR" => (PoolClaims(p) \cap dispatched = {}))
  /\ (mode = "QUORUM" /\ quorumState = "ERROR" => dispatched = {})

\* INV-ACCT-3: Dynamic ordinals never collide with fill or pre-detach ordinals
\* and stay bounded by nextOrdinal. Violated by StaleReattachBug!
MonotonicUniqueOrdinals ==
  /\ ~ordinalCollision
  /\ (~StaleReattachBug => Cardinality(usedOrdinals) = nextOrdinal)
  /\ \A o \in usedOrdinals : o >= 0

\* Once a QUORUM or GROUP outcome is decided, no members stay stranded in _waiting.
NoStrandedWaitingAfterOutcome ==
  /\ (mode = "QUORUM" /\ quorumState /= "UNDECIDED" =>
        \A p \in Pools : waiting[p] = {})
  /\ (mode = "GROUP" =>
        \A p \in Pools : groupOutcome[p] /= "UNDECIDED" => waiting[p] = {})

\* INV-ACCT-2 (F-25): Releasing a pool via release_group(p) immediately accounts
\* every un-dispatched fill claim of p in unable (even if popped into pacing sleep).
\* Violated by PoppedSlotBug!
ReleasedPoolImmediatelySettled ==
  \A p \in releasedPools :
    \A c \in PoolClaims(p) : c \in dispatched \/ c \in unable

=============================================================================
