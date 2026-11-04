----------------------------- MODULE BatchLease -----------------------------
(***************************************************************************)
(* The cross-process Lease protocol of the agent-sandbox batch claim       *)
(* feature (Python SDK at 23/N): two handles (processes) that claim,       *)
(* attach, renew, detach, release, crash, or go flaky, one reaper, the     *)
(* apiserver's single Lease object, and the garbage collector (GC).        *)
(*                                                                         *)
(* Abstracted away: HTTP payloads, threads inside one handle, individual   *)
(* claims and their states, groups, quorum, the watch, member deletes.     *)
(* Claims are a set of Lease UIDs ("some claims owned by UID u exist").    *)
(* One time unit is roughly D/6 seconds (D=6 here stands for 60 s).        *)
(***************************************************************************)
EXTENDS Integers, FiniteSets, TLC

CONSTANTS
  D,          \* lease duration (spec.leaseDurationSeconds of a held Lease)
  Grace,      \* detach(grace): leaseDurationSeconds written by detach
  G,          \* get_batch margin, batch_utils.CLOCK_SKEW_MARGIN (subtracted)
  M,          \* reaper margin, REAPER_DEFAULT_SKEW_MARGIN_SECONDS (added)
  SkewMax,    \* the reaper's wall clock runs ahead of the handles' by SkewMax
  Lat,        \* a request lands within Lat ticks of being sent; a reply within Lat of landing
  MaxTime,    \* bound on the global clock, keeps the state space finite
  MaxInc,     \* at most this many Lease incarnations (UIDs) are ever created
  MaxRounds,  \* release() gives up after this many delete rounds
  Bugs,       \* negative-control switches, {} for the shipped protocol
  Fixes,      \* candidate fixes, {} for the 23/N code: "uidScoped", "skipOnNone", "reaperWaitLonger"
  Features,   \* which optional moves are on: subsets of {"attach","detach","release","crash","flaky"}
  Procs,      \* the two handles (model values, symmetric)
  NoOne       \* an empty holderIdentity

Symm == Permutations(Procs)   \* the two handles are interchangeable

Interval(d) == IF d \div 3 > 1 THEN d \div 3 ELSE 1   \* max(1, d // 3)
I == Interval(D)
Min(a, b) == IF a < b THEN a ELSE b

NoLease == [exists |-> FALSE, uid |-> 0, rv |-> 0, holder |-> NoOne,
            renew |-> 0, dur |-> 0, trans |-> 0]
NoMsg == [from |-> NoOne, sent |-> 0, kind |-> "none", prec |-> 0, holder |-> NoOne,
          renew |-> 0, dur |-> 0, trans |-> 0, landBy |-> 0]
NoReply == [code |-> "none", rv |-> 0, uid |-> 0, trans |-> 0, at |-> 0]

VARIABLES
  now,        \* true time; handles read it directly (monotonic and wall clock agree)
  rskew,      \* reaper wall clock = now + rskew
  lease,      \* the apiserver's batch-<id> Lease
  nextUid, nextRv,
  claims,     \* UIDs of Leases that still own live claims (label batch-id=<id> on all)
  net,        \* requests in flight to the apiserver
  pc, alive, flaky,
  myUid, myTrans, myRv,   \* last_lease: the Lease from this handle's last successful write
  expAt,      \* _lease_expires_at (monotonic)
  err,        \* renewal stopped (err() set)
  nextRenew,  \* when the renew loop next sends
  wKind, wMsg, wBy, sentAt,  \* the one request this handle is blocked on
  reply,      \* reply waiting for the handle (or NoReply)
  retried,    \* the single 409 retry of detach() and _take_batch_lease
  took,       \* release round: rv written by take, 0 = take found no Lease, -1 = take failed
  rounds,
  rpc, rRv, rAt, rInt,    \* reaper: idle/waiting, listed rv, list time, its renew interval
  reaperKilled, reaperKilledConnected, badRelease  \* ghost flags for violations

hvars == <<pc, alive, flaky, myUid, myTrans, myRv, expAt, err, nextRenew,
           wKind, wMsg, wBy, sentAt, reply, retried, took, rounds>>
rvars == <<rpc, rRv, rAt, rInt>>
gvars == <<reaperKilled, reaperKilledConnected, badRelease>>
vars == <<now, rskew, lease, nextUid, nextRv, claims, net, hvars, rvars, gvars>>

\* A handle believes it holds the batch: it may connect to and delete members.
Believes(p) == alive[p] /\ pc[p] = "hold" /\ ~err[p] /\ now < expAt[p]

Init ==
  /\ now = 0 /\ rskew = SkewMax   \* worst case: a reaper further ahead may act at more times
  /\ lease = NoLease /\ nextUid = 1 /\ nextRv = 1
  /\ claims = {} /\ net = {}
  /\ pc = [p \in Procs |-> "idle"] /\ alive = [p \in Procs |-> TRUE]
  /\ flaky = [p \in Procs |-> FALSE]
  /\ myUid = [p \in Procs |-> 0] /\ myTrans = [p \in Procs |-> 0]
  /\ myRv = [p \in Procs |-> 0] /\ expAt = [p \in Procs |-> 0]
  /\ err = [p \in Procs |-> FALSE] /\ nextRenew = [p \in Procs |-> 0]
  /\ wKind = [p \in Procs |-> "none"] /\ wMsg = [p \in Procs |-> NoMsg]
  /\ wBy = [p \in Procs |-> 0] /\ sentAt = [p \in Procs |-> 0]
  /\ reply = [p \in Procs |-> NoReply] /\ retried = [p \in Procs |-> FALSE]
  /\ took = [p \in Procs |-> 0] /\ rounds = [p \in Procs |-> 0]
  /\ rpc = "idle" /\ rRv = 0 /\ rAt = 0 /\ rInt = 0
  /\ reaperKilled = FALSE /\ reaperKilledConnected = FALSE /\ badRelease = FALSE

Msg(p, kind, prec, holder, renew, dur, trans) ==
  [from |-> p, sent |-> now, kind |-> kind, prec |-> prec, holder |-> holder,
   renew |-> renew, dur |-> dur, trans |-> trans, landBy |-> now + Lat]

\* Send a write and block on it until a reply, a timeout at `by`, or an error.
SendWait(p, kind, prec, holder, renew, dur, trans, by) ==
  /\ net' = net \cup {Msg(p, kind, prec, holder, renew, dur, trans)}
  /\ wKind' = [wKind EXCEPT ![p] = kind]
  /\ wMsg' = [wMsg EXCEPT ![p] = Msg(p, kind, prec, holder, renew, dur, trans)]
  /\ wBy' = [wBy EXCEPT ![p] = by] /\ sentAt' = [sentAt EXCEPT ![p] = now]
  /\ reply' = [reply EXCEPT ![p] = NoReply]

\* Send without waiting for the reply (release's final Lease delete, whose outcome never matters:
\* delete_batch_lease counts 404 and a precondition 409 as done, K8S-1).
SendNoWait(p, kind, prec) ==
  /\ net' = net \cup {Msg(p, kind, prec, NoOne, 0, 0, 0)}

Unblock(p) == /\ wKind' = [wKind EXCEPT ![p] = "none"]
              /\ reply' = [reply EXCEPT ![p] = NoReply]

-----------------------------------------------------------------------------
(* Handle actions *)

\* SandboxBatch._claim: create-if-absent the Lease (409 => BatchExistsError), then list claims by
\* label and refuse if any exist. renewed_at is read before renewTime is stamped (LEASE-5).
\* Abstraction: create, list and the fill's creates are one atomic step with an instant reply.
Claim(p) ==
  /\ pc[p] = "idle" /\ alive[p] /\ ~lease.exists /\ claims = {} /\ nextUid <= MaxInc
  /\ lease' = [exists |-> TRUE, uid |-> nextUid, rv |-> nextRv, holder |-> p,
               renew |-> now, dur |-> D, trans |-> 0]
  /\ claims' = claims \cup {nextUid}
  /\ nextUid' = nextUid + 1 /\ nextRv' = nextRv + 1
  /\ pc' = [pc EXCEPT ![p] = "hold"]
  /\ myUid' = [myUid EXCEPT ![p] = nextUid] /\ myTrans' = [myTrans EXCEPT ![p] = 0]
  /\ myRv' = [myRv EXCEPT ![p] = nextRv]
  /\ expAt' = [expAt EXCEPT ![p] = now + D] /\ nextRenew' = [nextRenew EXCEPT ![p] = now + I]
  /\ UNCHANGED <<now, rskew, net, alive, flaky, err, wKind, wMsg, wBy, sentAt,
                 reply, retried, took, rounds, rvars, gvars>>

\* SandboxBatch._attach: read, staleness check `now > renew + dur - G` (is_lease_stale), empty
\* holder counts as free (K8S-16), then one replace at the read's resourceVersion with
\* leaseTransitions + 1 (LEASE-14). Bug "getBatchAdds" adds the margin instead.
NotStaleForAttach == IF "getBatchAdds" \in Bugs THEN now <= lease.renew + lease.dur + G
                     ELSE now <= lease.renew + lease.dur - G
Attach(p) ==
  /\ "attach" \in Features /\ pc[p] = "idle" /\ alive[p] /\ lease.exists /\ lease.holder = NoOne /\ NotStaleForAttach
  /\ pc' = [pc EXCEPT ![p] = "attaching"]
  /\ SendWait(p, "takeover", lease.rv, p, now, D, lease.trans + 1, now + Lat + Lat)
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, claims, alive, flaky, myUid, myTrans,
                 myRv, expAt, err, nextRenew, retried, took, rounds, rvars, gvars>>

\* _renew_loop / _renew_once / _write_renewal: sent_at is read before renewTime is stamped; the
\* PUT carries the last written resourceVersion; timeout min(interval, time left).
Renew(p) ==
  /\ pc[p] = "hold" /\ alive[p] /\ ~err[p] /\ wKind[p] = "none"
  /\ now >= nextRenew[p] /\ now < expAt[p]
  /\ SendWait(p, "renew", myRv[p], p, now, D, myTrans[p], Min(now + I, expAt[p]))
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, claims, pc, alive, flaky, myUid, myTrans,
                 myRv, expAt, err, nextRenew, retried, took, rounds, rvars, gvars>>

\* SandboxBatch.detach: renewal is stopped and joined first (no request outstanding), then read;
\* a missing Lease or another holder counts as released (LEASE-15); else write holder=None.
Detach(p) ==
  /\ "detach" \in Features /\ pc[p] = "hold" /\ alive[p] /\ wKind[p] = "none"
  /\ retried' = [retried EXCEPT ![p] = FALSE]
  /\ IF ~lease.exists \/ lease.holder # p
     THEN /\ pc' = [pc EXCEPT ![p] = "detached"]
          /\ UNCHANGED <<net, wKind, wMsg, wBy, sentAt, reply>>
     ELSE /\ pc' = [pc EXCEPT ![p] = "detaching"]
          /\ SendWait(p, "detach", lease.rv, NoOne, now, Grace, lease.trans, now + Lat + Lat)
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, claims, alive, flaky, myUid, myTrans,
                 myRv, expAt, err, nextRenew, took, rounds, rvars, gvars>>

\* SandboxBatch.release: stop renewal, then _delete_batch_objects. Allowed on a holding handle
\* (also after its Lease expired or renewal stopped) and after a failed detach (LEASE-22).
StartRelease(p) ==
  /\ "release" \in Features /\ pc[p] \in {"hold", "detfail"} /\ alive[p] /\ wKind[p] = "none"
  /\ pc' = [pc EXCEPT ![p] = "rel_take"]
  /\ rounds' = [rounds EXCEPT ![p] = 0] /\ retried' = [retried EXCEPT ![p] = FALSE]
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, claims, net, alive, flaky,
                 myUid, myTrans, myRv, expAt, err, nextRenew, wKind, wMsg, wBy, sentAt, reply,
                 took, rvars, gvars>>

\* _take_batch_lease: read; None if missing; fence on UID + leaseTransitions (LEASE-17) and on
\* holder; then a conditional replace. Bug "holderOnlyFence" drops the UID/transitions check.
\* With no Lease, the round goes straight on to deletecollection by label (LEASE-18).
Fenced(p) == /\ "holderOnlyFence" \notin Bugs
             /\ (lease.uid # myUid[p] \/ lease.trans # myTrans[p])
TakeRead(p) ==
  /\ pc[p] = "rel_take" /\ alive[p]
  /\ IF ~lease.exists
     THEN /\ took' = [took EXCEPT ![p] = 0]
          /\ IF "skipOnNone" \in Fixes
             THEN /\ pc' = [pc EXCEPT ![p] = "rel_count"]
                  /\ UNCHANGED <<net, wKind, wMsg, wBy, sentAt, reply>>
             ELSE /\ pc' = [pc EXCEPT ![p] = "rel_dcoll"]
                  /\ SendWait(p, "dcoll", 0, NoOne, 0, 0, 0, now + Lat + Lat)
     ELSE IF Fenced(p) \/ lease.holder \notin {NoOne, p}
     THEN /\ pc' = [pc EXCEPT ![p] = "done"]   \* BatchInUseError, latched (LEASE-20)
          /\ UNCHANGED <<took, net, wKind, wMsg, wBy, sentAt, reply>>
     ELSE /\ pc' = [pc EXCEPT ![p] = "rel_wait"]
          /\ SendWait(p, "take", lease.rv, p, now, D, lease.trans, now + Lat + Lat)
          /\ UNCHANGED took
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, claims, alive, flaky, myUid, myTrans,
                 myRv, expAt, err, nextRenew, retried, rounds, rvars, gvars>>

\* _delete_claims_in_rounds: a consistent count of live claims by label; finish only when
\* live == 0 and this round's take succeeded; finish deletes the Lease at the rv written.
Live(p) == IF "uidScoped" \in Fixes THEN claims \cap {myUid[p]} ELSE claims
Count(p) ==
  /\ pc[p] = "rel_count" /\ alive[p]
  /\ IF Live(p) = {} /\ took[p] >= 0
     THEN /\ pc' = [pc EXCEPT ![p] = "done"]
          /\ IF took[p] > 0 THEN SendNoWait(p, "ldel", took[p]) ELSE UNCHANGED <<net>>
          /\ UNCHANGED <<rounds, retried>>
     ELSE /\ IF rounds[p] + 1 < MaxRounds
             THEN pc' = [pc EXCEPT ![p] = "rel_take"] ELSE pc' = [pc EXCEPT ![p] = "done"]
          /\ rounds' = [rounds EXCEPT ![p] = rounds[p] + 1]
          /\ retried' = [retried EXCEPT ![p] = FALSE]
          /\ UNCHANGED <<net>>
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, claims, alive, flaky, myUid, myTrans,
                 myRv, expAt, err, nextRenew, wKind, wMsg, wBy, sentAt, reply, took, rvars, gvars>>

\* A reply arrives for the request the handle is blocked on.
Recv(p) ==
  LET r == reply[p] IN
  /\ alive[p] /\ wKind[p] # "none" /\ r # NoReply /\ now <= wBy[p]
  /\ \/ /\ wKind[p] = "renew" /\ now < expAt[p]
        /\ \/ /\ r.code = "ok"   \* _renew_once success: expiry = sent_at + D (LEASE-5)
              /\ myRv' = [myRv EXCEPT ![p] = r.rv]
              /\ expAt' = [expAt EXCEPT ![p] =
                            (IF "expiryFromResponse" \in Bugs THEN now ELSE sentAt[p]) + D]
              /\ nextRenew' = [nextRenew EXCEPT ![p] = now + I]
              /\ Unblock(p) /\ UNCHANGED <<err, net, wMsg, wBy, sentAt>>
           \/ /\ r.code = "409"  \* read; continue only on proof we still hold it (LEASE-28)
              /\ IF lease.exists /\ lease.holder = p
                 THEN /\ SendWait(p, "renew", lease.rv, p, now, D, lease.trans,
                                  Min(now + I, expAt[p]))
                      /\ UNCHANGED err
                 ELSE /\ err' = [err EXCEPT ![p] = TRUE] /\ Unblock(p)
                      /\ UNCHANGED <<net, wMsg, wBy, sentAt>>
              /\ UNCHANGED <<myRv, expAt, nextRenew>>
           \/ /\ r.code = "404"  \* Lease gone: BatchLeaseExpiredError, never recreate (LEASE-7)
              /\ err' = [err EXCEPT ![p] = TRUE] /\ Unblock(p)
              /\ UNCHANGED <<myRv, expAt, nextRenew, net, wMsg, wBy, sentAt>>
        /\ UNCHANGED <<pc, myUid, myTrans, retried, took>>
     \/ /\ wKind[p] = "takeover"
        /\ IF r.code = "ok"
           THEN /\ pc' = [pc EXCEPT ![p] = "hold"]
                /\ myUid' = [myUid EXCEPT ![p] = r.uid]
                /\ myTrans' = [myTrans EXCEPT ![p] = r.trans]
                /\ myRv' = [myRv EXCEPT ![p] = r.rv]
                /\ expAt' = [expAt EXCEPT ![p] =
                              (IF "expiryFromResponse" \in Bugs THEN now ELSE sentAt[p]) + D]
                /\ nextRenew' = [nextRenew EXCEPT ![p] = now + I]
                /\ err' = [err EXCEPT ![p] = FALSE]
           ELSE /\ pc' = [pc EXCEPT ![p] = "done"]   \* 409 => BatchInUseError
                /\ UNCHANGED <<myUid, myTrans, myRv, expAt, nextRenew, err>>
        /\ Unblock(p) /\ UNCHANGED <<net, wMsg, wBy, sentAt, retried, took>>
     \/ /\ wKind[p] = "detach"
        /\ \/ /\ r.code = "ok" /\ pc' = [pc EXCEPT ![p] = "detached"]
              /\ Unblock(p) /\ UNCHANGED <<net, wMsg, wBy, sentAt, retried>>
           \/ /\ r.code = "409"  \* re-read: gone, no holder, or other holder => done (LEASE-15)
              /\ IF ~lease.exists \/ lease.holder # p
                 THEN /\ pc' = [pc EXCEPT ![p] = "detached"] /\ Unblock(p)
                      /\ UNCHANGED <<net, wMsg, wBy, sentAt, retried>>
                 ELSE IF ~retried[p]   \* own late renewal landed: retry once (LEASE-16)
                 THEN /\ retried' = [retried EXCEPT ![p] = TRUE] /\ UNCHANGED pc
                      /\ SendWait(p, "detach", lease.rv, NoOne, now, Grace, lease.trans,
                                  now + Lat + Lat)
                 ELSE /\ pc' = [pc EXCEPT ![p] = "detfail"] /\ Unblock(p)
                      /\ UNCHANGED <<net, wMsg, wBy, sentAt, retried>>
           \/ /\ r.code = "404" /\ pc' = [pc EXCEPT ![p] = "detfail"]
              /\ Unblock(p) /\ UNCHANGED <<net, wMsg, wBy, sentAt, retried>>
        /\ UNCHANGED <<myUid, myTrans, myRv, expAt, nextRenew, err, took>>
     \/ /\ wKind[p] = "take"
        /\ \/ /\ r.code = "ok"   \* took it: deletecollection by label, then count
              /\ myRv' = [myRv EXCEPT ![p] = r.rv] /\ took' = [took EXCEPT ![p] = r.rv]
              /\ pc' = [pc EXCEPT ![p] = "rel_dcoll"]
              /\ SendWait(p, "dcoll", 0, NoOne, 0, 0, 0, now + Lat + Lat)
              /\ UNCHANGED retried
           \/ /\ r.code = "409"  \* re-read and re-fence once, then BatchInUseError (LEASE-16)
              /\ IF retried[p] THEN pc' = [pc EXCEPT ![p] = "done"]
                 ELSE pc' = [pc EXCEPT ![p] = "rel_take"]
              /\ retried' = [retried EXCEPT ![p] = TRUE]
              /\ UNCHANGED <<myRv, took, net>>
           \/ /\ r.code = "404"  \* not retryable: release() raises
              /\ pc' = [pc EXCEPT ![p] = "done"]
              /\ UNCHANGED <<myRv, took, net, retried>>
        /\ (r.code # "ok" => Unblock(p) /\ UNCHANGED <<wMsg, wBy, sentAt>>)
        /\ UNCHANGED <<myUid, myTrans, expAt, nextRenew, err>>
     \/ /\ wKind[p] = "dcoll"   \* deletecollection returned; count next
        /\ pc' = [pc EXCEPT ![p] = "rel_count"]
        /\ Unblock(p)
        /\ UNCHANGED <<net, wMsg, wBy, sentAt, myUid, myTrans, myRv, expAt, nextRenew,
                       err, retried, took>>
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, claims, alive, flaky, rounds, rvars, gvars>>

\* The client gives up on its request: a timeout, or (flaky) a transport error at any time.
\* The request itself may still land later (LEASE-16).
GiveUp(p) ==
  /\ alive[p] /\ wKind[p] # "none" /\ (now >= wBy[p] \/ flaky[p])
  /\ Unblock(p)
  /\ CASE wKind[p] = "renew"    -> /\ nextRenew' = [nextRenew EXCEPT ![p] = now + 1]  \* backoff
                                   /\ UNCHANGED <<pc, took>>
       [] wKind[p] = "takeover" -> /\ pc' = [pc EXCEPT ![p] = "done"]
                                   /\ UNCHANGED <<nextRenew, took>>
       [] wKind[p] = "detach"   -> /\ pc' = [pc EXCEPT ![p] = "detfail"]
                                   /\ UNCHANGED <<nextRenew, took>>
       [] wKind[p] = "take"     -> /\ pc' = [pc EXCEPT ![p] = "rel_count"]  \* held = False
                                   /\ took' = [took EXCEPT ![p] = -1]
                                   /\ UNCHANGED nextRenew
       [] wKind[p] = "dcoll"    -> /\ pc' = [pc EXCEPT ![p] = "rel_count"]  \* transient
                                   /\ UNCHANGED <<nextRenew, took>>
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, claims, net, alive, flaky,
                 myUid, myTrans, myRv, expAt, err, wMsg, wBy, sentAt, retried, rounds,
                 rvars, gvars>>

\* A crash only matters while the handle holds the batch; a crash at any other point looks the
\* same to everyone else as a handle that stops taking steps, which the model already allows.
Crash(p) ==
  /\ "crash" \in Features /\ alive[p] /\ pc[p] = "hold"
  /\ alive' = [alive EXCEPT ![p] = FALSE]
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, claims, net, pc, flaky, myUid,
                 myTrans, myRv, expAt, err, nextRenew, wKind, wMsg, wBy, sentAt, reply,
                 retried, took, rounds, rvars, gvars>>

\* From now on p's requests and replies may be lost, it may see transport errors, and it may
\* pause (it is exempt from the timeliness rules in Tick).
GoFlaky(p) ==
  /\ "flaky" \in Features /\ alive[p] /\ \A q \in Procs : ~flaky[q]   \* at most one flaky handle
  /\ flaky' = [flaky EXCEPT ![p] = TRUE]
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, claims, net, pc, alive, myUid,
                 myTrans, myRv, expAt, err, nextRenew, wKind, wMsg, wBy, sentAt, reply,
                 retried, took, rounds, rvars, gvars>>

-----------------------------------------------------------------------------
(* The apiserver and the garbage collector *)

Victims(m) == IF "uidScoped" \in Fixes THEN claims \cap {myUid[m.from]} ELSE claims

\* Writes are replace-with-resourceVersion (409 on mismatch, 404 if gone). Bug
\* "unconditionalRenew" makes renewals an unconditional PATCH (LEASE-2).
Deliver(m) ==
  /\ m \in net /\ now <= m.landBy
  /\ net' = net \ {m}
  /\ CASE m.kind = "dcoll" ->
            /\ claims' = claims \ Victims(m)
            /\ badRelease' = (badRelease \/ \E u \in Victims(m) :
                   lease.exists /\ lease.uid = u
                   /\ (u # myUid[m.from] \/ lease.trans # myTrans[m.from]))
            /\ reply' = IF wMsg[m.from] = m /\ wKind[m.from] # "none"
                        THEN [reply EXCEPT ![m.from] = [NoReply EXCEPT !.code = "ok", !.at = now]]
                        ELSE reply
            /\ UNCHANGED <<lease, nextRv, reaperKilled, reaperKilledConnected>>
       [] m.kind = "ldel" ->   \* delete_batch_lease(resource_version=...)
            /\ lease' = IF lease.exists /\ lease.rv = m.prec THEN NoLease ELSE lease
            /\ UNCHANGED <<claims, nextRv, reply, gvars>>
       [] OTHER ->
            LET ok == lease.exists /\ (lease.rv = m.prec
                        \/ ("unconditionalRenew" \in Bugs /\ m.kind = "renew"))
                new == [lease EXCEPT !.rv = nextRv, !.holder = m.holder, !.renew = m.renew,
                          !.dur = m.dur,
                          !.trans = IF m.kind = "takeover" THEN m.trans ELSE lease.trans]
                code == IF ok THEN "ok" ELSE IF lease.exists THEN "409" ELSE "404"
            IN /\ lease' = IF ok THEN new ELSE lease
               /\ nextRv' = IF ok THEN nextRv + 1 ELSE nextRv
               /\ reply' = IF wMsg[m.from] = m /\ wKind[m.from] # "none"
                           THEN [reply EXCEPT ![m.from] = [code |-> code,
                                   rv |-> nextRv, uid |-> lease.uid, trans |-> new.trans,
                                   at |-> now]]
                           ELSE reply
               /\ UNCHANGED <<claims, gvars>>
  /\ UNCHANGED <<now, rskew, nextUid, pc, alive, flaky, myUid, myTrans, myRv, expAt,
                 err, nextRenew, wKind, wMsg, wBy, sentAt, retried, took, rounds, rvars>>

\* A flaky handle's requests may be lost: Tick discards any request that missed its landBy.
\* A lost reply is the same as GiveUp, which a flaky handle may do at any time.

\* GC deletes claims whose owner UID no longer exists (K8S-2).
GC(u) ==
  /\ u \in claims /\ ~(lease.exists /\ lease.uid = u)
  /\ claims' = claims \ {u}
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, net, hvars, rvars, gvars>>

-----------------------------------------------------------------------------
(* The reaper: batch_reaper.find_stale_leases and reap *)

\* is_lease_reapable: now > renewTime + D + margin, on the reaper's own clock.
\* Bug "reaperSubtracts" subtracts the margin.
Reapable == IF "reaperSubtracts" \in Bugs THEN now + rskew > lease.renew + lease.dur - M
            ELSE now + rskew > lease.renew + lease.dur + M
ReapList ==
  /\ rpc = "idle" /\ lease.exists /\ Reapable
  /\ rpc' = "waiting" /\ rRv' = lease.rv /\ rAt' = now /\ rInt' = Interval(lease.dur)
  /\ UNCHANGED <<now, rskew, lease, nextUid, nextRv, claims, net, hvars, gvars>>

\* Wait one renew interval from listed_at, then _delete_lease_if_unchanged (precondition rv).
\* Fix "reaperWaitLonger" also waits out the longest gap between two renewal writes of a timely
\* holder beyond the interval (a reply leg plus a request leg, plus one tick to break ties).
\* Bugs: "reaperNoWait" skips the wait, "reaperNoPrecondition" deletes by name only.
ReapDelete ==
  /\ rpc = "waiting"
  /\ \/ "reaperNoWait" \in Bugs
     \/ now >= rAt + rInt + (IF "reaperWaitLonger" \in Fixes THEN 2 * Lat + 1 ELSE 0)
  /\ LET hit == lease.exists /\ (lease.rv = rRv \/ "reaperNoPrecondition" \in Bugs)
         victims == {p \in Procs : Believes(p) /\ myUid[p] = lease.uid}
     IN /\ lease' = IF hit THEN NoLease ELSE lease
        /\ reaperKilled' = (reaperKilled \/ (hit /\ victims # {}))
        /\ reaperKilledConnected' = (reaperKilledConnected
                                     \/ (hit /\ \E p \in victims : ~flaky[p]))
  /\ rpc' = "idle"
  /\ UNCHANGED <<now, rskew, nextUid, nextRv, claims, net, hvars, rRv, rAt, rInt,
                 badRelease>>

-----------------------------------------------------------------------------
(* Time. A connected (non-flaky) live handle is timely: its renewals go out on schedule, its   *)
(* requests land within Lat, its replies arrive within Lat, and it times out at its deadline. *)

Timely(p) ==
  alive[p] /\ ~flaky[p] =>
    /\ ~(pc[p] = "hold" /\ ~err[p] /\ wKind[p] = "none" /\ now >= nextRenew[p]
         /\ now < expAt[p])
    /\ ~(wKind[p] # "none" /\ now >= wBy[p])
    /\ (reply[p] # NoReply => reply[p].at + Lat > now)
Tick ==
  /\ now < MaxTime
  /\ \A p \in Procs : Timely(p)
  /\ \A m \in net : ~flaky[m.from] => m.landBy > now
  /\ now' = now + 1
  /\ net' = {m \in net : m.landBy > now}                \* late flaky requests are lost
  /\ reply' = [p \in Procs |-> IF reply[p].at + Lat > now THEN reply[p] ELSE NoReply]
  /\ UNCHANGED <<rskew, lease, nextUid, nextRv, claims, pc, alive, flaky, myUid,
                 myTrans, myRv, expAt, err, nextRenew, wKind, wMsg, wBy, sentAt, retried, took,
                 rounds, rvars, gvars>>

Next ==
  \/ Tick \/ ReapList \/ ReapDelete
  \/ \E p \in Procs : Claim(p) \/ Attach(p) \/ Renew(p) \/ Detach(p) \/ StartRelease(p)
                      \/ TakeRead(p) \/ Count(p) \/ Recv(p) \/ GiveUp(p) \/ Crash(p)
                      \/ GoFlaky(p)
  \/ \E m \in net : Deliver(m)
  \/ \E u \in claims : GC(u)

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* Properties *)

\* The reaper never deletes the Lease of a live handle that still believes it holds it.
ReaperSafety == ~reaperKilled
\* The same, but only for believers that are connected and timely.
ReaperSafetyConnected == ~reaperKilledConnected
\* At most one live handle believes it holds the batch.
AtMostOneBeliever == \A p, q \in Procs : Believes(p) /\ Believes(q) => p = q
\* A believer's belief is backed by the server: its Lease incarnation exists and names it.
BelieverIsHolder == \A p \in Procs : Believes(p) =>
                      lease.exists /\ lease.uid = myUid[p] /\ lease.holder = p
\* A believer's claims are still there.
BelieverKeepsClaims == \A p \in Procs : Believes(p) => myUid[p] \in claims
\* release() never deletes claims of a live Lease incarnation it doesn't own, nor of a batch
\* another handle attached to since this handle last wrote the Lease.
ReleaseFencing == ~badRelease
=============================================================================
