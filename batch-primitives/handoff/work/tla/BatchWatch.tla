--------------------------- MODULE BatchWatch ---------------------------
\* Formal TLA+ specification of the SandboxBatch / AsyncSandboxBatch watch,
\* multi-page 410 Gone relist, readiness flap, release_not_ready() late-watch
\* tombstoning, and connection-cache eviction protocol (sandbox_batch.py:
\* _watch_loop, _resync_watch, _sync_connections, connect; batch_state.py:
\* upsert_claim, mark_lost, resync_from_list, release_not_ready).
\*
\* Checks invariants:
\*   - NoStaleEndpointCached (INV-CONN-1)
\*   - ReleasedOrLostEvicted (INV-CONN-2)
\*   - ReleasedNeverResurrected (INV-OWN-4 tombstone monotonicity across 410 relist)
\*   - NoFalseLostOnRelist (INV-ACCT-4 multi-page relist completeness)
\*   - NoOrphanedLiveReleasedClaim (INV-LEAK-1 / F-03 late watch ADDED delete queueing)

EXTENDS Integers, FiniteSets

CONSTANTS
  Claims,       \* Set of claim names, e.g. {"c1", "c2"}
  Pods,         \* Set of pod identities (sandbox_name/ip), e.g. {"p1", "p2"}
  NoPod,        \* Sentinel for unready/missing pod, e.g. "none"
  Bugs          \* Subset of {"skipEvictOnPodSwap", "dropPage1OnRelist",
                \*            "resurrectReleasedOnRelist", "lateAddedNoDelete"}

VARIABLES
  clusterState,    \* [Claims -> {"ABSENT", "STARTING", "READY"}]
  clusterPod,      \* [Claims -> Pods \cup {NoPod}]
  everClusterGone, \* Subset of Claims ever deleted on cluster (for false-LOST detection)
  everReleased,    \* Monotonic ghost set of caller-released claims
  fillStopped,     \* BOOLEAN: release_not_ready() called
  pendingDeletes,  \* Subset of Claims queued in _pending_deletes for K8s DELETE
  watchMode,       \* "STREAMING" | "RELIST_PAGE1" | "RELIST_PAGE2"
  page1Seen,       \* Subset of Claims accumulated on page 1 of 410 relist
  relistItems,     \* [Claims -> {"ABSENT", "STARTING", "READY"}] snapshot buffer
  relistPods,      \* [Claims -> Pods \cup {NoPod}] snapshot buffer
  memberState,     \* [Claims -> {"UNKNOWN", "STARTING", "READY", "LOST", "RELEASED"}]
  memberPod,       \* [Claims -> Pods \cup {NoPod}]
  connectedPod,    \* [Claims -> Pods \cup {NoPod}] cached Sandbox connection
  staleQueue       \* Set of Pods queued for close_connection() outside _lock

vars == <<clusterState, clusterPod, everClusterGone, everReleased, fillStopped,
          pendingDeletes, watchMode, page1Seen, relistItems, relistPods,
          memberState, memberPod, connectedPod, staleQueue>>

SkipEvictBug == "skipEvictOnPodSwap" \in Bugs
DropPage1Bug == "dropPage1OnRelist" \in Bugs
ResurrectReleasedBug == "resurrectReleasedOnRelist" \in Bugs
LateAddedNoDeleteBug == "lateAddedNoDelete" \in Bugs

Init ==
  /\ clusterState = [c \in Claims |-> "STARTING"]
  /\ clusterPod = [c \in Claims |-> NoPod]
  /\ everClusterGone = {}
  /\ everReleased = {}
  /\ fillStopped = FALSE
  /\ pendingDeletes = {}
  /\ watchMode = "STREAMING"
  /\ page1Seen = {}
  /\ relistItems = [c \in Claims |-> "ABSENT"]
  /\ relistPods = [c \in Claims |-> NoPod]
  /\ memberState = [c \in Claims |-> "UNKNOWN"]
  /\ memberPod = [c \in Claims |-> NoPod]
  /\ connectedPod = [c \in Claims |-> NoPod]
  /\ staleQueue = {}

\* Helper: apply connection eviction rules from _sync_connections(claim, member).
EvictForUpdate(c, newState, newPod) ==
  IF connectedPod[c] = NoPod
  THEN /\ connectedPod' = connectedPod
       /\ staleQueue' = staleQueue
  ELSE IF newState /= "READY"
  THEN /\ connectedPod' = [connectedPod EXCEPT ![c] = NoPod]
       /\ staleQueue' = staleQueue \cup {connectedPod[c]}
  ELSE IF newPod /= connectedPod[c] /\ ~SkipEvictBug
  THEN /\ connectedPod' = [connectedPod EXCEPT ![c] = NoPod]
       /\ staleQueue' = staleQueue \cup {connectedPod[c]}
  ELSE /\ connectedPod' = connectedPod
       /\ staleQueue' = staleQueue

\* Cluster controller transitions a claim to READY with a chosen pod identity
\* (modeling initial readiness or pod restart / IP change).
ClusterBecomeReady(c, p) ==
  /\ clusterState[c] /= "ABSENT"
  /\ clusterState' = [clusterState EXCEPT ![c] = "READY"]
  /\ clusterPod' = [clusterPod EXCEPT ![c] = p]
  /\ UNCHANGED <<everClusterGone, everReleased, fillStopped, pendingDeletes,
                 watchMode, page1Seen, relistItems, relistPods, memberState,
                 memberPod, connectedPod, staleQueue>>

\* Cluster controller marks a READY claim temporarily not Ready (readiness flap).
ClusterFlapNotReady(c) ==
  /\ clusterState[c] = "READY"
  /\ clusterState' = [clusterState EXCEPT ![c] = "STARTING"]
  /\ clusterPod' = [clusterPod EXCEPT ![c] = NoPod]
  /\ UNCHANGED <<everClusterGone, everReleased, fillStopped, pendingDeletes,
                 watchMode, page1Seen, relistItems, relistPods, memberState,
                 memberPod, connectedPod, staleQueue>>

\* External deletion of claim on cluster (e.g. shutdownTime or eviction).
ClusterDeleteClaim(c) ==
  /\ clusterState[c] /= "ABSENT"
  /\ clusterState' = [clusterState EXCEPT ![c] = "ABSENT"]
  /\ clusterPod' = [clusterPod EXCEPT ![c] = NoPod]
  /\ everClusterGone' = everClusterGone \cup {c}
  /\ UNCHANGED <<everReleased, fillStopped, pendingDeletes, watchMode,
                 page1Seen, relistItems, relistPods, memberState, memberPod,
                 connectedPod, staleQueue>>

\* Asynchronous apiserver completion of caller-queued HTTP DELETE (404 also clears).
ServerCompleteDelete(c) ==
  /\ c \in pendingDeletes
  /\ everClusterGone' = IF clusterState[c] /= "ABSENT"
                        THEN everClusterGone \cup {c}
                        ELSE everClusterGone
  /\ clusterState' = [clusterState EXCEPT ![c] = "ABSENT"]
  /\ clusterPod' = [clusterPod EXCEPT ![c] = NoPod]
  /\ pendingDeletes' = pendingDeletes \ {c}
  /\ UNCHANGED <<everReleased, fillStopped, watchMode, page1Seen, relistItems,
                 relistPods, memberState, memberPod, connectedPod, staleQueue>>

\* Watch delivers ADDED/MODIFIED event for a live claim on the cluster.
WatchDeliverUpsert(c) ==
  /\ watchMode = "STREAMING"
  /\ clusterState[c] /= "ABSENT"
  /\ memberState[c] /= "RELEASED"
  /\ IF fillStopped /\ memberState[c] = "UNKNOWN"
     THEN /\ memberState' = [memberState EXCEPT ![c] = "RELEASED"]
          /\ memberPod' = memberPod
          /\ everReleased' = everReleased \cup {c}
          /\ pendingDeletes' = IF LateAddedNoDeleteBug
                               THEN pendingDeletes
                               ELSE pendingDeletes \cup {c}
          /\ UNCHANGED <<connectedPod, staleQueue>>
     ELSE LET ns == clusterState[c]
              np == clusterPod[c]
          IN /\ memberState' = [memberState EXCEPT ![c] = ns]
             /\ memberPod' = [memberPod EXCEPT ![c] = np]
             /\ EvictForUpdate(c, ns, np)
             /\ UNCHANGED <<everReleased, pendingDeletes>>
  /\ UNCHANGED <<clusterState, clusterPod, everClusterGone, fillStopped,
                 watchMode, page1Seen, relistItems, relistPods>>

\* Watch delivers DELETED event for a deleted claim.
WatchDeliverDeleted(c) ==
  /\ watchMode = "STREAMING"
  /\ clusterState[c] = "ABSENT"
  /\ memberState[c] \in {"STARTING", "READY"}
  /\ memberState' = [memberState EXCEPT ![c] = "LOST"]
  /\ memberPod' = [memberPod EXCEPT ![c] = NoPod]
  /\ EvictForUpdate(c, "LOST", NoPod)
  /\ UNCHANGED <<clusterState, clusterPod, everClusterGone, everReleased,
                 fillStopped, pendingDeletes, watchMode, page1Seen,
                 relistItems, relistPods>>

\* Watch receives HTTP 410 Gone (compact resourceVersion) and begins paged relist.
WatchHit410Gone ==
  /\ watchMode = "STREAMING"
  /\ watchMode' = "RELIST_PAGE1"
  /\ page1Seen' = {}
  /\ relistItems' = [c \in Claims |-> "ABSENT"]
  /\ relistPods' = [c \in Claims |-> NoPod]
  /\ UNCHANGED <<clusterState, clusterPod, everClusterGone, everReleased,
                 fillStopped, pendingDeletes, memberState, memberPod,
                 connectedPod, staleQueue>>

\* Page 1 of list_sandbox_claim_objects reads a subset S of Claims.
RelistReadPage1(S) ==
  /\ watchMode = "RELIST_PAGE1"
  /\ S \subseteq Claims
  /\ page1Seen' = S
  /\ relistItems' = [c \in Claims |-> IF c \in S THEN clusterState[c] ELSE "ABSENT"]
  /\ relistPods' = [c \in Claims |-> IF c \in S THEN clusterPod[c] ELSE NoPod]
  /\ watchMode' = "RELIST_PAGE2"
  /\ UNCHANGED <<clusterState, clusterPod, everClusterGone, everReleased,
                 fillStopped, pendingDeletes, memberState, memberPod,
                 connectedPod, staleQueue>>

\* Page 2 reads remaining claims (Claims \ page1Seen) and commits resync_from_list
\* + _sync_all_connections atomically under _changed.
RelistCommitPage2 ==
  /\ watchMode = "RELIST_PAGE2"
  /\ LET fullState   == [c \in Claims |->
                          IF c \in page1Seen
                          THEN (IF DropPage1Bug THEN "ABSENT" ELSE relistItems[c])
                          ELSE clusterState[c]]
         fullPod     == [c \in Claims |->
                          IF c \in page1Seen
                          THEN (IF DropPage1Bug THEN NoPod ELSE relistPods[c])
                          ELSE clusterPod[c]]
         lateUnknown == {c \in Claims :
                          fillStopped /\ memberState[c] = "UNKNOWN" /\
                          fullState[c] /= "ABSENT"}
         nextMState  == [c \in Claims |->
                          IF memberState[c] = "RELEASED" /\ ~ResurrectReleasedBug THEN "RELEASED"
                          ELSE IF c \in lateUnknown THEN "RELEASED"
                          ELSE IF fullState[c] /= "ABSENT" THEN fullState[c]
                          ELSE IF memberState[c] \in {"STARTING", "READY", "RELEASED"} THEN "LOST"
                          ELSE memberState[c]]
         nextMPod    == [c \in Claims |->
                          IF nextMState[c] = "READY" THEN fullPod[c] ELSE NoPod]
         evicted     == {c \in Claims :
                          connectedPod[c] /= NoPod /\
                          (nextMState[c] /= "READY" \/
                           (~SkipEvictBug /\ nextMPod[c] /= connectedPod[c]))}
     IN /\ memberState' = nextMState
        /\ memberPod' = nextMPod
        /\ everReleased' = everReleased \cup lateUnknown
        /\ pendingDeletes' = IF LateAddedNoDeleteBug
                             THEN pendingDeletes
                             ELSE pendingDeletes \cup lateUnknown
        /\ connectedPod' = [c \in Claims |->
                            IF c \in evicted THEN NoPod ELSE connectedPod[c]]
        /\ staleQueue' = staleQueue \cup {connectedPod[c] : c \in evicted}
        /\ watchMode' = "STREAMING"
  /\ UNCHANGED <<clusterState, clusterPod, everClusterGone, fillStopped,
                 page1Seen, relistItems, relistPods>>

\* Caller connects to a READY member and caches its pod identity under _lock.
CallerConnect(c) ==
  /\ memberState[c] = "READY"
  /\ memberPod[c] /= NoPod
  /\ connectedPod[c] = NoPod
  /\ connectedPod' = [connectedPod EXCEPT ![c] = memberPod[c]]
  /\ UNCHANGED <<clusterState, clusterPod, everClusterGone, everReleased,
                 fillStopped, pendingDeletes, watchMode, page1Seen,
                 relistItems, relistPods, memberState, memberPod, staleQueue>>

\* Caller calls release_member(c), recording tombstone and queueing K8s DELETE.
CallerReleaseMember(c) ==
  /\ memberState[c] \in {"STARTING", "READY"}
  /\ memberState' = [memberState EXCEPT ![c] = "RELEASED"]
  /\ memberPod' = [memberPod EXCEPT ![c] = NoPod]
  /\ everReleased' = everReleased \cup {c}
  /\ pendingDeletes' = pendingDeletes \cup {c}
  /\ EvictForUpdate(c, "RELEASED", NoPod)
  /\ UNCHANGED <<clusterState, clusterPod, everClusterGone, fillStopped,
                 watchMode, page1Seen, relistItems, relistPods>>

\* Caller calls release_not_ready(), stopping fill and releasing known STARTING claims.
CallerReleaseNotReady ==
  /\ ~fillStopped
  /\ fillStopped' = TRUE
  /\ LET rel == {c \in Claims : memberState[c] = "STARTING"}
     IN /\ memberState' = [c \in Claims |-> IF c \in rel THEN "RELEASED" ELSE memberState[c]]
        /\ memberPod' = [c \in Claims |-> IF c \in rel THEN NoPod ELSE memberPod[c]]
        /\ everReleased' = everReleased \cup rel
        /\ pendingDeletes' = pendingDeletes \cup rel
  /\ UNCHANGED <<clusterState, clusterPod, everClusterGone, watchMode,
                 page1Seen, relistItems, relistPods, connectedPod, staleQueue>>

\* Background/caller drains one evicted connection from staleQueue via close_connection().
DrainStaleConnection(p) ==
  /\ p \in staleQueue
  /\ staleQueue' = staleQueue \ {p}
  /\ UNCHANGED <<clusterState, clusterPod, everClusterGone, everReleased,
                 fillStopped, pendingDeletes, watchMode, page1Seen,
                 relistItems, relistPods, memberState, memberPod, connectedPod>>

IdleWhenAllGone ==
  /\ \A c \in Claims : clusterState[c] = "ABSENT" /\ memberState[c] \in {"LOST", "RELEASED", "UNKNOWN"}
  /\ watchMode = "STREAMING"
  /\ staleQueue = {}
  /\ pendingDeletes = {}
  /\ UNCHANGED vars

Next ==
  \/ \E c \in Claims, p \in Pods : ClusterBecomeReady(c, p)
  \/ \E c \in Claims : ClusterFlapNotReady(c)
  \/ \E c \in Claims : ClusterDeleteClaim(c)
  \/ \E c \in Claims : ServerCompleteDelete(c)
  \/ \E c \in Claims : WatchDeliverUpsert(c)
  \/ \E c \in Claims : WatchDeliverDeleted(c)
  \/ WatchHit410Gone
  \/ \E S \in SUBSET Claims : RelistReadPage1(S)
  \/ RelistCommitPage2
  \/ \E c \in Claims : CallerConnect(c)
  \/ \E c \in Claims : CallerReleaseMember(c)
  \/ CallerReleaseNotReady
  \/ \E p \in Pods : DrainStaleConnection(p)
  \/ IdleWhenAllGone

Spec == Init /\ [][Next]_vars

\* ---------------------------------------------------------------------------
\* Invariants
\* ---------------------------------------------------------------------------

\* INV-CONN-1: Every cached connection in _connected belongs to a currently
\* READY member and points to that member's current sandbox_name / pod_ip.
NoStaleEndpointCached ==
  \A c \in Claims :
    connectedPod[c] /= NoPod =>
      /\ memberState[c] = "READY"
      /\ connectedPod[c] = memberPod[c]

\* INV-CONN-2: Released or lost members never retain a cached connection.
ReleasedOrLostEvicted ==
  \A c \in Claims :
    memberState[c] \in {"LOST", "RELEASED", "STARTING", "UNKNOWN"} =>
      connectedPod[c] = NoPod

\* INV-OWN-4: Released tombstones are strictly monotonic; even if a 410 relist
\* reads a lingering cluster object before HTTP DELETE completes, memberState
\* stays RELEASED. Violated by ResurrectReleasedBug!
ReleasedNeverResurrected ==
  \A c \in everReleased : memberState[c] = "RELEASED"

\* INV-ACCT-4: A claim that was never deleted on the cluster and never released
\* by the caller is never falsely marked LOST by paged 410 relist. Violated by
\* DropPage1Bug!
NoFalseLostOnRelist ==
  \A c \in Claims :
    (c \notin everClusterGone /\ c \notin everReleased) => memberState[c] /= "LOST"

\* INV-LEAK-1 (F-03): Any claim tombstoned in RELEASED while still live on the
\* cluster MUST be queued in pendingDeletes so K8s DELETE runs. Violated by
\* LateAddedNoDeleteBug!
NoOrphanedLiveReleasedClaim ==
  \A c \in Claims :
    (memberState[c] = "RELEASED" /\ clusterState[c] /= "ABSENT") => c \in pendingDeletes

=============================================================================
