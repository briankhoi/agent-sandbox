--------------------------- MODULE BatchHandle ---------------------------
\* Subsystem composition and authority separation model for SandboxBatch and
\* AsyncSandboxBatch (sandbox_batch.py, async_sandbox_batch.py).
\*
\* Models the interaction between:
\*   - renew_loop (_renew_loop / _renew_once / _expire_lease_if_due)
\*   - watch_loop (_watch_loop / _resync_watch)
\*   - create_loop / dynamic acquire()
\*   - connected active Sandbox workloads and release_member()
\*   - detach() / release() lifecycle termination
\*
\* Negative control: Bugs = {"sharedWatchLeaseError"} reproduces the exact
\* baseline behavior at e84e964ce2d87987ec2443a5b5edccc84a374289 where
\* _watch_loop writes fatal watch errors (such as HTTP 403 Forbidden) into
\* shared self._state.note_error(e), causing _renew_once() to return False
\* (halting Lease renewal while sandboxes remain active), discarding the
\* newly written Lease resourceVersion, leaving lease_degraded() False, and
\* blocking release_member() via _check_healthy().

EXTENDS Integers, FiniteSets

CONSTANTS
  LeaseDur,       \* Lease duration in ticks (e.g. 6)
  RenewInt,       \* Renewal interval in ticks (e.g. 2)
  MaxTime,        \* Time bound for finite model checking (e.g. 10)
  MaxSandboxes,   \* Max concurrently connected active sandboxes (e.g. 2)
  AllowExternalLeaseLoss, \* TRUE to inject 404/409 Lease loss on server
  Bugs            \* Subset of {"sharedWatchLeaseError"}

VARIABLES
  now,              \* Monotonic clock: 0..MaxTime
  leaseExpiresAt,   \* Handle's _lease_expires_at deadline
  nextRenewAt,      \* Next scheduled _renew_once wakeup
  sentAt,           \* Monotonic timestamp of in-flight _write_renewal
  serverRv,         \* Apiserver Lease resourceVersion
  myRv,             \* Handle's cached self._lease.metadata.resource_version
  renewState,       \* "ACTIVE" | "IN_FLIGHT" | "STOPPED"
  renewDegraded,    \* Handle's _renewal_degraded flag (lease_degraded())
  watchState,       \* "RUNNING" | "STOPPED"
  watchErr,         \* "NONE" | "WATCH_FAILED"
  leaseErr,         \* "NONE" | "LEASE_GONE" | "LEASE_IN_USE" | "LEASE_EXPIRED"
  stateError,       \* Shared _state._error (first-write-wins when bug enabled)
  createState,      \* "RUNNING" | "STOPPED"
  activeSandboxes,  \* Count of live connected Sandbox handles running work
  detached,         \* Handle's _detached flag (set by detach() or release())
  blockedRelease    \* TRUE if release_member() raised BatchError under valid Lease

vars == <<now, leaseExpiresAt, nextRenewAt, sentAt, serverRv, myRv,
          renewState, renewDegraded, watchState, watchErr, leaseErr,
          stateError, createState, activeSandboxes, detached, blockedRelease>>

SharedErrorBug == "sharedWatchLeaseError" \in Bugs

\* Effective health check for Lease-fenced operations (release_member, renewal).
\* Under SharedErrorBug (e84e964c), _check_healthy() and _renew_once() treat any
\* stateError /= "NONE" as Lease loss. Under the decoupled model, only detached
\* or actual Lease loss stops Lease renewal and member cleanup.
LeaseAuthorityLost ==
  IF SharedErrorBug
  THEN detached \/ stateError /= "NONE"
  ELSE detached \/ leaseErr /= "NONE"

\* Watch-dependent operations (wait_for_quorum, acquire readiness wait, events)
\* stop on detached, Lease loss, or fatal watch failure in both models.
WatchDependentStopped ==
  detached \/ leaseErr /= "NONE" \/ watchErr /= "NONE"

Init ==
  /\ now = 0
  /\ leaseExpiresAt = LeaseDur
  /\ nextRenewAt = RenewInt
  /\ sentAt = 0
  /\ serverRv = 1
  /\ myRv = 1
  /\ renewState = "ACTIVE"
  /\ renewDegraded = FALSE
  /\ watchState = "RUNNING"
  /\ watchErr = "NONE"
  /\ leaseErr = "NONE"
  /\ stateError = "NONE"
  /\ createState = "RUNNING"
  /\ activeSandboxes = 1
  /\ detached = FALSE
  /\ blockedRelease = FALSE

\* Connect an additional ready member before watch/Lease failure.
ConnectSandbox ==
  /\ ~WatchDependentStopped
  /\ now < leaseExpiresAt
  /\ activeSandboxes < MaxSandboxes
  /\ activeSandboxes' = activeSandboxes + 1
  /\ UNCHANGED <<now, leaseExpiresAt, nextRenewAt, sentAt, serverRv, myRv,
                 renewState, renewDegraded, watchState, watchErr, leaseErr,
                 stateError, createState, detached, blockedRelease>>

\* Caller finishes work on an active sandbox and calls batch.release_member().
\* Under SharedErrorBug, _check_healthy() raises BatchError when stateError is
\* set by a watch 403 even though the handle still holds a valid Lease.
ReleaseActiveSandbox ==
  /\ activeSandboxes > 0
  /\ ~detached
  /\ now < leaseExpiresAt
  /\ leaseErr = "NONE"
  /\ IF LeaseAuthorityLost
     THEN /\ blockedRelease' = TRUE
          /\ UNCHANGED activeSandboxes
     ELSE /\ activeSandboxes' = activeSandboxes - 1
          /\ UNCHANGED blockedRelease
  /\ UNCHANGED <<now, leaseExpiresAt, nextRenewAt, sentAt, serverRv, myRv,
                 renewState, renewDegraded, watchState, watchErr, leaseErr,
                 stateError, createState, detached>>

\* _watch_loop encounters a non-transient error (HTTP 403 Forbidden, 404, or
\* TLS certificate verification error). Watch terminates and records watchErr.
\* Under SharedErrorBug, it writes directly into shared stateError.
WatchFatalError ==
  /\ watchState = "RUNNING"
  /\ ~detached
  /\ watchState' = "STOPPED"
  /\ watchErr' = "WATCH_FAILED"
  /\ stateError' = IF SharedErrorBug /\ stateError = "NONE"
                   THEN "WATCH_FAILED"
                   ELSE stateError
  /\ createState' = "STOPPED"
  /\ UNCHANGED <<now, leaseExpiresAt, nextRenewAt, sentAt, serverRv, myRv,
                 renewState, renewDegraded, leaseErr, activeSandboxes,
                 detached, blockedRelease>>

\* _renew_loop wakes at nextRenewAt (before leaseExpiresAt) and sends PUT Lease.
\* Note: _renew_once() sends _write_renewal BEFORE acquiring self._lock to check
\* self._detached or self._state.error() (sandbox_batch.py:1711, 1738).
RenewStart ==
  /\ renewState = "ACTIVE"
  /\ ~detached
  /\ now >= nextRenewAt
  /\ now < leaseExpiresAt
  /\ renewState' = "IN_FLIGHT"
  /\ sentAt' = now
  /\ serverRv' = serverRv + 1
  /\ UNCHANGED <<now, leaseExpiresAt, nextRenewAt, myRv, renewDegraded,
                 watchState, watchErr, leaseErr, stateError, createState,
                 activeSandboxes, detached, blockedRelease>>

\* _write_renewal HTTP PUT succeeds and _renew_once acquires self._lock
\* (sandbox_batch.py:1737-1743).
\* Under SharedErrorBug, `if self._detached or self._state.error() is not None:
\* return False` discards `written` (leaving myRv < serverRv), does not advance
\* leaseExpiresAt, leaves renewDegraded = FALSE, and stops _renew_loop!
RenewFinishSuccess ==
  /\ renewState = "IN_FLIGHT"
  /\ IF LeaseAuthorityLost
     THEN /\ renewState' = "STOPPED"
          /\ UNCHANGED <<leaseExpiresAt, nextRenewAt, myRv, renewDegraded>>
     ELSE /\ renewState' = "ACTIVE"
          /\ myRv' = serverRv
          /\ leaseExpiresAt' = sentAt + LeaseDur
          /\ nextRenewAt' = now + RenewInt
          /\ renewDegraded' = FALSE
  /\ UNCHANGED <<now, sentAt, serverRv, watchState, watchErr, leaseErr,
                 stateError, createState, activeSandboxes, detached,
                 blockedRelease>>

\* Transient renewal error (HTTP 500/503 or socket timeout): marks degraded and
\* schedules retry backoff without stopping _renew_loop.
RenewTransientFailure ==
  /\ renewState = "ACTIVE"
  /\ ~detached
  /\ now >= nextRenewAt
  /\ now < leaseExpiresAt
  /\ renewDegraded' = TRUE
  /\ nextRenewAt' = now + 1
  /\ UNCHANGED <<now, leaseExpiresAt, sentAt, serverRv, myRv, renewState,
                 watchState, watchErr, leaseErr, stateError, createState,
                 activeSandboxes, detached, blockedRelease>>

\* Optional external Lease loss on apiserver (404 Lease deleted or 409 stolen).
RenewExternalLoss ==
  /\ AllowExternalLeaseLoss
  /\ renewState = "ACTIVE"
  /\ ~detached
  /\ now >= nextRenewAt
  /\ now < leaseExpiresAt
  /\ renewState' = "STOPPED"
  /\ renewDegraded' = TRUE
  /\ leaseErr' = "LEASE_GONE"
  /\ stateError' = IF stateError = "NONE" THEN "LEASE_GONE" ELSE stateError
  /\ createState' = "STOPPED"
  /\ watchState' = "STOPPED"
  /\ UNCHANGED <<now, leaseExpiresAt, nextRenewAt, sentAt, serverRv, myRv,
                 watchErr, activeSandboxes, detached, blockedRelease>>

\* _renew_loop or _expire_lease_if_due observes now >= leaseExpiresAt.
\* Notice sandbox_batch.py:491 checks `self._state.error() is None` before
\* marking renewal degraded when expiring on caller path!
ExpireLeaseDue ==
  /\ ~detached
  /\ now >= leaseExpiresAt
  /\ leaseErr = "NONE"
  /\ renewState /= "IN_FLIGHT"
  /\ renewState' = "STOPPED"
  /\ leaseErr' = "LEASE_EXPIRED"
  /\ renewDegraded' = IF SharedErrorBug /\ stateError /= "NONE"
                      THEN renewDegraded
                      ELSE TRUE
  /\ stateError' = IF stateError = "NONE" THEN "LEASE_EXPIRED" ELSE stateError
  /\ createState' = "STOPPED"
  /\ watchState' = "STOPPED"
  /\ UNCHANGED <<now, leaseExpiresAt, nextRenewAt, sentAt, serverRv, myRv,
                 watchErr, activeSandboxes, detached, blockedRelease>>

\* Caller explicitly detaches or releases the batch handle.
DetachOrRelease ==
  /\ ~detached
  /\ detached' = TRUE
  /\ renewState' = "STOPPED"
  /\ watchState' = "STOPPED"
  /\ createState' = "STOPPED"
  /\ activeSandboxes' = 0
  /\ UNCHANGED <<now, leaseExpiresAt, nextRenewAt, sentAt, serverRv, myRv,
                 renewDegraded, watchErr, leaseErr, stateError, blockedRelease>>

\* Monotonic clock tick. If _renew_loop is ACTIVE and not degraded, renewal fires
\* at nextRenewAt before the Lease expires (timely local scheduler).
Tick ==
  /\ now < MaxTime
  /\ ~(renewState = "ACTIVE" /\ ~detached /\ now + 1 >= leaseExpiresAt /\ ~renewDegraded /\ now >= nextRenewAt)
  /\ renewState /= "IN_FLIGHT"
  /\ now' = now + 1
  /\ UNCHANGED <<leaseExpiresAt, nextRenewAt, sentAt, serverRv, myRv,
                 renewState, renewDegraded, watchState, watchErr, leaseErr,
                 stateError, createState, activeSandboxes, detached,
                 blockedRelease>>

IdleAtHorizon ==
  /\ now = MaxTime
  /\ renewState /= "IN_FLIGHT"
  /\ UNCHANGED vars

Next ==
  \/ ConnectSandbox
  \/ ReleaseActiveSandbox
  \/ WatchFatalError
  \/ RenewStart
  \/ RenewFinishSuccess
  \/ RenewTransientFailure
  \/ RenewExternalLoss
  \/ ExpireLeaseDue
  \/ DetachOrRelease
  \/ Tick
  \/ IdleAtHorizon

Spec == Init /\ [][Next]_vars

\* ---------------------------------------------------------------------------
\* Invariants (Master Invariant Oracle Section 1 & Section 5.1)
\* ---------------------------------------------------------------------------

\* Step 1.1 Negative-Control Invariant (INV-AUTH-1, exact form for watch-fault runs):
\* While sandboxes are active, Lease has not expired, and handle is not detached,
\* renew_loop must not stop (unless external 404/409 Lease loss is enabled).
ActiveWorkloadProtected ==
  (activeSandboxes > 0 /\ now < leaseExpiresAt /\ ~detached)
    => renewState /= "STOPPED"

\* General authority separation invariant holding even with AllowExternalLeaseLoss = TRUE:
AuthoritySeparation ==
  (activeSandboxes > 0 /\ now < leaseExpiresAt /\ ~detached /\ leaseErr = "NONE")
    => renewState /= "STOPPED"

\* INV-AUTH-2: Fatal watch error must not block release_member() while Lease is valid.
CleanMemberReleaseAllowed ==
  ~blockedRelease

\* Cached self._lease resourceVersion must match the last server write whenever
\* the handle is not detached and has not lost Lease authority.
CachedLeaseRvConsistent ==
  (~detached /\ leaseErr = "NONE" /\ renewState /= "IN_FLIGHT")
    => (myRv = serverRv)

\* INV-AUTH-3: Once the Lease expires on an non-detached handle, lease_degraded()
\* must be True (never masked by an earlier watch error).
ExpiredImpliesDegraded ==
  (~detached /\ leaseErr = "LEASE_EXPIRED") => renewDegraded

=============================================================================
