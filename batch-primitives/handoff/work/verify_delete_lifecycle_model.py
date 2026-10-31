#!/usr/bin/env python3
"""Exhaustive BFS finite-state model checker for SandboxBatch / AsyncSandboxBatch
delete concurrency vs detach()/release()/cleanup lifecycle synchronization.

Models and compares:
  1. SYNC Ordering 2 (wait for active deletes BEFORE _lifecycle_lock; release() commits
     _detach_grace = None INSIDE _lifecycle_lock) at cap=1 and cap=2, with concurrent
     D1, D2, DETACH, RELEASE, failed-detach retry, and same-thread POSIX SIGNAL reentry
     on both D1 and DETACH threads.
  2. SYNC Ordering 1 (wait for active deletes INSIDE _lifecycle_lock):
     - Passes when signal handler does NOT re-enter from a delete thread while another
       thread holds _lifecycle_lock;
     - FAILS Inv 5 (5-step ABBA deadlock!) when T_REL/T_DET holds _lifecycle_lock waiting
       for T_D1's active delete and a POSIX signal interrupts T_D1 and calls release()/detach().
  3. SYNC Negative Controls:
     - Flaw A: release() sets _detach_grace = None BEFORE acquiring _lifecycle_lock
       (breaks 12/N first-call-wins when detach() holds _lifecycle_lock).
     - Flaw B: _check_lease_held() checks only _lease_released instead of _detached
       (breaks Inv 6 post-detach refusal after failed detach() or retry).
     - Flaw C: _wait_active_deletes() does NOT subtract same-thread count
       (self-deadlocks on signal handler interrupting release_member on single thread).
  4. ASYNC Ordering 1 & Ordering 2 at cap=1 and cap=2 (with D1, D2, DETACH, RELEASE,
     AsyncSandboxClient._stop_batches(), self._lock contention, and CancelledError at
     every await point), plus Async Negative Controls:
     - Flaw B (async): _check_lease_held() checks only _lease_released instead of _detached
       (breaks Inv 6 after failed detach() or _stop_batches()).
     - Flaw D (async): finally block awaits self._lock before decrementing _active_deletes
       (leaks _active_deletes and deadlocks detach/release under double cancellation).
     - Variant E (async): _stop_batches() does not await _no_active_deletes before
       k8s_helper.close() (closes aiohttp session while HTTP DELETE is in flight).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable


# ============================================================================
# 1. SYNC MODEL (RLock + per-thread active-delete tracker + signal reentry)
# ============================================================================
#
# Thread IDs:
#   T_D1  = 1 (runs D1; e.g. main thread calling release_member — can receive POSIX signal)
#   T_D2  = 2 (runs D2; e.g. worker thread)
#   T_DET = 3 (runs DETACH; can also receive POSIX signal when detach runs on main thread)
#   T_REL = 4 (runs RELEASE)


@dataclass(frozen=True)
class SyncConfig:
    cap: int = 2
    # Ordering 1 = wait_inside_lc_lock=True; Ordering 2 = wait_inside_lc_lock=False
    wait_inside_lc_lock: bool = False
    release_clears_grace_before_lc_lock: bool = False
    check_only_lease_released: bool = False
    subtract_own_thread: bool = True
    enable_signal_on_delete_thread: bool = True
    enable_signal_on_detach_thread: bool = True
    enable_failed_detach_followup: bool = True


@dataclass(frozen=True)
class SyncState:
    detached: bool = False
    detach_grace_set: bool = False
    lease_released: bool = False
    released: bool = False
    lc_owner: int | None = None
    lc_depth: int = 0
    # Per-thread active delete slot count: (count_T1, count_T2)
    del_slots: tuple[int, int] = (0, 0)

    # Delete actor PCs: IDLE -> WAIT_SLOT | PRE_HTTP -> IN_HTTP -> (RETRY_CHECK -> IN_HTTP) -> DONE_OK | DONE_ERR
    d1_pc: str = "IDLE"
    d1_started_after_detach: bool = False
    d2_pc: str = "IDLE"
    d2_started_after_detach: bool = False

    # Lifecycle actor PCs
    det_pc: str = "IDLE"
    rel_pc: str = "IDLE"

    # Optional POSIX signal handler frame (at most 1 signal in trace):
    # sig_host: 0 = none, 1 = interrupting T_D1, 3 = interrupting T_DET
    # sig_kind: "" | "detach" | "release"
    sig_host: int = 0
    sig_kind: str = ""
    sig_pc: str = "IDLE"

    followup_used: bool = False
    # Ghost history: which cross-thread actor ("detach" or "release") first entered _lifecycle_lock
    first_lc_winner: str = ""


def _release_started(s: SyncState) -> bool:
    return s.detached and not s.detach_grace_set


def _entry_blocks_delete(s: SyncState) -> bool:
    return s.detached


def _lease_check_blocks_delete(s: SyncState, cfg: SyncConfig) -> bool:
    return s.lease_released if cfg.check_only_lease_released else s.detached


def _other_active_deletes(s: SyncState, tid: int, cfg: SyncConfig) -> int:
    total = s.del_slots[0] + s.del_slots[1]
    if not cfg.subtract_own_thread:
        return total
    own = s.del_slots[0] if tid == 1 else (s.del_slots[1] if tid == 2 else 0)
    return total - own


def _can_acq_lc(s: SyncState, tid: int) -> bool:
    return s.lc_owner is None or s.lc_owner == tid


def _acq_lc(s: SyncState, tid: int) -> tuple[int, int]:
    return (tid, s.lc_depth + 1)


def _rel_lc(s: SyncState) -> tuple[int | None, int]:
    if s.lc_depth <= 1:
        return (None, 0)
    return (s.lc_owner, s.lc_depth - 1)


def _is_paused_by_signal(s: SyncState, tid: int) -> bool:
    return s.sig_host == tid and s.sig_pc not in ("DONE_OK", "DONE_BATCH_ERR", "DONE_API_ERR")


def _wake_slot_waiters(s: SyncState, cfg: SyncConfig) -> SyncState:
    d1_pc, d2_pc = s.d1_pc, s.d2_pc
    if d1_pc == "WAIT_SLOT" and not _is_paused_by_signal(s, 1) and _entry_blocks_delete(s):
        d1_pc = "DONE_ERR"
    if d2_pc == "WAIT_SLOT" and _entry_blocks_delete(s):
        d2_pc = "DONE_ERR"
    return SyncState(**{**s.__dict__, "d1_pc": d1_pc, "d2_pc": d2_pc})


def sync_transitions(s: SyncState, cfg: SyncConfig) -> Iterable[tuple[str, SyncState]]:
    # --- Delete actors D1 (tid=1) and D2 (tid=2) ---
    for idx, tid, pc, name in ((0, 1, s.d1_pc, "D1"), (1, 2, s.d2_pc, "D2")):
        if _is_paused_by_signal(s, tid):
            continue
        pc_field = "d1_pc" if idx == 0 else "d2_pc"
        flag_field = "d1_started_after_detach" if idx == 0 else "d2_started_after_detach"

        if pc == "IDLE":
            if _entry_blocks_delete(s):
                yield (f"{name}:refuse_detached", SyncState(**{**s.__dict__, pc_field: "DONE_ERR"}))
            elif s.del_slots[0] + s.del_slots[1] < cfg.cap:
                slots = (s.del_slots[0] + 1, s.del_slots[1]) if idx == 0 else (s.del_slots[0], s.del_slots[1] + 1)
                yield (f"{name}:take_slot", SyncState(**{**s.__dict__, pc_field: "PRE_HTTP", "del_slots": slots}))
            else:
                yield (f"{name}:wait_slot", SyncState(**{**s.__dict__, pc_field: "WAIT_SLOT"}))

        elif pc == "WAIT_SLOT":
            if _entry_blocks_delete(s):
                yield (f"{name}:wake_detached", SyncState(**{**s.__dict__, pc_field: "DONE_ERR"}))
            elif s.del_slots[0] + s.del_slots[1] < cfg.cap:
                slots = (s.del_slots[0] + 1, s.del_slots[1]) if idx == 0 else (s.del_slots[0], s.del_slots[1] + 1)
                yield (f"{name}:wake_take_slot", SyncState(**{**s.__dict__, pc_field: "PRE_HTTP", "del_slots": slots}))

        elif pc in ("PRE_HTTP", "RETRY_CHECK"):
            if _lease_check_blocks_delete(s, cfg):
                slots = (s.del_slots[0] - 1, s.del_slots[1]) if idx == 0 else (s.del_slots[0], s.del_slots[1] - 1)
                yield (f"{name}:abort_before_http", SyncState(**{**s.__dict__, pc_field: "DONE_ERR", "del_slots": slots}))
            else:
                yield (
                    f"{name}:start_http",
                    SyncState(**{**s.__dict__, pc_field: "IN_HTTP", flag_field: s.detached}),
                )

        elif pc == "IN_HTTP":
            slots = (s.del_slots[0] - 1, s.del_slots[1]) if idx == 0 else (s.del_slots[0], s.del_slots[1] - 1)
            yield (f"{name}:http_ok", SyncState(**{**s.__dict__, pc_field: "DONE_OK", "del_slots": slots}))
            yield (f"{name}:http_503_retry", SyncState(**{**s.__dict__, pc_field: "RETRY_CHECK"}))

    # --- DETACH state machine ---
    def detach_steps(label: str, tid: int, pc: str, set_pc: Callable[[SyncState, str], SyncState]):
        if pc == "IDLE":
            if _release_started(s):
                yield (f"{label}:fail_fast_release_started", set_pc(s, "DONE_BATCH_ERR"))
            elif s.lease_released:
                yield (f"{label}:noop_already_detached", set_pc(s, "DONE_OK"))
            else:
                ns = SyncState(**{**s.__dict__, "detached": True, "detach_grace_set": True})
                ns = _wake_slot_waiters(ns, cfg)
                next_pc = "WAIT_LC_LOCK" if cfg.wait_inside_lc_lock else "WAIT_DELETES_OUT"
                yield (f"{label}:begin_detach", set_pc(ns, next_pc))

        elif pc == "WAIT_DELETES_OUT":
            if _release_started(s):
                yield (f"{label}:abort_release_started_out", set_pc(s, "DONE_BATCH_ERR"))
            elif s.lease_released:
                yield (f"{label}:done_already_detached_out", set_pc(s, "DONE_OK"))
            elif _other_active_deletes(s, tid, cfg) == 0:
                yield (f"{label}:drained_stop_bg", set_pc(s, "STOP_BG"))

        elif pc == "STOP_BG":
            yield (f"{label}:stopped_bg", set_pc(s, "WAIT_LC_LOCK"))

        elif pc == "WAIT_LC_LOCK":
            if _can_acq_lc(s, tid):
                owner, depth = _acq_lc(s, tid)
                winner = s.first_lc_winner or ("detach" if label == "DET" else "")
                ns = SyncState(**{**s.__dict__, "lc_owner": owner, "lc_depth": depth, "first_lc_winner": winner})
                if cfg.wait_inside_lc_lock:
                    yield (f"{label}:acq_lc", set_pc(ns, "WAIT_DELETES_IN"))
                else:
                    if _release_started(ns):
                        o2, d2 = _rel_lc(ns)
                        yield (f"{label}:abort_in_lc", set_pc(SyncState(**{**ns.__dict__, "lc_owner": o2, "lc_depth": d2}), "DONE_BATCH_ERR"))
                    elif ns.lease_released:
                        o2, d2 = _rel_lc(ns)
                        yield (f"{label}:done_in_lc", set_pc(SyncState(**{**ns.__dict__, "lc_owner": o2, "lc_depth": d2}), "DONE_OK"))
                    else:
                        yield (f"{label}:acq_lc_enter_write", set_pc(ns, "LEASE_WRITE"))

        elif pc == "WAIT_DELETES_IN":
            if _release_started(s):
                owner, depth = _rel_lc(s)
                yield (f"{label}:abort_release_started", set_pc(SyncState(**{**s.__dict__, "lc_owner": owner, "lc_depth": depth}), "DONE_BATCH_ERR"))
            elif s.lease_released:
                owner, depth = _rel_lc(s)
                yield (f"{label}:done_already_released", set_pc(SyncState(**{**s.__dict__, "lc_owner": owner, "lc_depth": depth}), "DONE_OK"))
            elif _other_active_deletes(s, tid, cfg) == 0:
                yield (f"{label}:drained_enter_write", set_pc(s, "LEASE_WRITE"))

        elif pc == "LEASE_WRITE":
            owner, depth = _rel_lc(s)
            if _release_started(s):
                yield (f"{label}:abort_after_reentrant_release", set_pc(SyncState(**{**s.__dict__, "lc_owner": owner, "lc_depth": depth}), "DONE_BATCH_ERR"))
            elif s.lease_released:
                yield (f"{label}:done_after_reentrant_detach", set_pc(SyncState(**{**s.__dict__, "lc_owner": owner, "lc_depth": depth}), "DONE_OK"))
            else:
                ns_ok = SyncState(**{**s.__dict__, "lease_released": True, "lc_owner": owner, "lc_depth": depth})
                yield (f"{label}:lease_write_ok", set_pc(ns_ok, "DONE_OK"))
                if label == "DET" and cfg.enable_failed_detach_followup:
                    ns_err = SyncState(**{**s.__dict__, "lc_owner": owner, "lc_depth": depth})
                    yield (f"{label}:lease_write_503", set_pc(ns_err, "DONE_API_ERR"))

    # --- RELEASE state machine ---
    def release_steps(label: str, tid: int, pc: str, set_pc: Callable[[SyncState, str], SyncState]):
        if pc == "IDLE":
            if cfg.wait_inside_lc_lock:
                yield (f"{label}:want_lc", set_pc(s, "WAIT_LC_LOCK"))
            else:
                if s.released:
                    yield (f"{label}:noop_released", set_pc(s, "DONE_OK"))
                elif s.lease_released:
                    yield (f"{label}:err_lease_released", set_pc(s, "DONE_BATCH_ERR"))
                else:
                    # Ordering 2: set _detached=True under _lock (stopping new deletes),
                    # WITHOUT clearing _detach_grace until inside _lifecycle_lock!
                    grace_flag = False if cfg.release_clears_grace_before_lc_lock else s.detach_grace_set
                    ns = SyncState(**{**s.__dict__, "detached": True, "detach_grace_set": grace_flag})
                    ns = _wake_slot_waiters(ns, cfg)
                    yield (f"{label}:mark_detached_wait_deletes", set_pc(ns, "WAIT_DELETES_OUT"))

        elif pc == "WAIT_DELETES_OUT":
            if s.released:
                yield (f"{label}:noop_released_out", set_pc(s, "DONE_OK"))
            elif s.lease_released:
                yield (f"{label}:err_lease_released_out", set_pc(s, "DONE_BATCH_ERR"))
            elif _other_active_deletes(s, tid, cfg) == 0:
                yield (f"{label}:drained_want_lc", set_pc(s, "WAIT_LC_LOCK"))

        elif pc == "WAIT_LC_LOCK":
            if _can_acq_lc(s, tid):
                owner, depth = _acq_lc(s, tid)
                winner = s.first_lc_winner or ("release" if label == "REL" else "")
                if s.released:
                    ns = SyncState(**{**s.__dict__, "lc_owner": owner, "lc_depth": depth})
                    o2, d2 = _rel_lc(ns)
                    yield (f"{label}:noop_released", set_pc(SyncState(**{**ns.__dict__, "lc_owner": o2, "lc_depth": d2}), "DONE_OK"))
                elif s.lease_released:
                    ns = SyncState(**{**s.__dict__, "lc_owner": owner, "lc_depth": depth})
                    o2, d2 = _rel_lc(ns)
                    yield (f"{label}:err_lease_released", set_pc(SyncState(**{**ns.__dict__, "lc_owner": o2, "lc_depth": d2}), "DONE_BATCH_ERR"))
                else:
                    ns = SyncState(
                        **{
                            **s.__dict__,
                            "detached": True,
                            "detach_grace_set": False,
                            "lc_owner": owner,
                            "lc_depth": depth,
                            "first_lc_winner": winner,
                        }
                    )
                    ns = _wake_slot_waiters(ns, cfg)
                    next_pc = "WAIT_DELETES_IN" if cfg.wait_inside_lc_lock else "DELETE_ROUND"
                    yield (f"{label}:acq_lc_and_commit_release", set_pc(ns, next_pc))

        elif pc == "WAIT_DELETES_IN":
            if _other_active_deletes(s, tid, cfg) == 0:
                yield (f"{label}:drained_enter_delete", set_pc(s, "DELETE_ROUND"))

        elif pc == "DELETE_ROUND":
            owner, depth = _rel_lc(s)
            ns_ok = SyncState(
                **{
                    **s.__dict__,
                    "lease_released": True,
                    "released": True,
                    "lc_owner": owner,
                    "lc_depth": depth,
                }
            )
            yield (f"{label}:delete_ok", set_pc(ns_ok, "DONE_OK"))

    if not _is_paused_by_signal(s, 3):
        yield from detach_steps("DET", 3, s.det_pc, lambda st, p: SyncState(**{**st.__dict__, "det_pc": p}))
        if cfg.enable_failed_detach_followup and s.det_pc == "DONE_API_ERR" and not s.followup_used:
            yield (
                "DET:retry_after_fail",
                SyncState(**{**s.__dict__, "det_pc": "IDLE", "followup_used": True}),
            )

    yield from release_steps("REL", 4, s.rel_pc, lambda st, p: SyncState(**{**st.__dict__, "rel_pc": p}))

    # --- Signal-handler same-thread reentry ---
    if s.sig_host == 0:
        if cfg.enable_signal_on_delete_thread and s.d1_pc in ("WAIT_SLOT", "IN_HTTP"):
            for kind in ("detach", "release"):
                yield (
                    f"SIG_on_D1:start_{kind}",
                    SyncState(**{**s.__dict__, "sig_host": 1, "sig_kind": kind, "sig_pc": "IDLE"}),
                )
        if cfg.enable_signal_on_detach_thread and s.det_pc in ("WAIT_DELETES_OUT", "STOP_BG", "WAIT_DELETES_IN", "LEASE_WRITE"):
            for kind in ("detach", "release"):
                yield (
                    f"SIG_on_DET:start_{kind}",
                    SyncState(**{**s.__dict__, "sig_host": 3, "sig_kind": kind, "sig_pc": "IDLE"}),
                )
    elif s.sig_pc not in ("DONE_OK", "DONE_BATCH_ERR", "DONE_API_ERR"):
        set_sig = lambda st, p: SyncState(**{**st.__dict__, "sig_pc": p})
        if s.sig_kind == "detach":
            yield from detach_steps(f"SIG({s.sig_host},det)", s.sig_host, s.sig_pc, set_sig)
        else:
            yield from release_steps(f"SIG({s.sig_host},rel)", s.sig_host, s.sig_pc, set_sig)


def check_sync_model(name: str, cfg: SyncConfig) -> dict[str, object]:
    init = SyncState()
    visited: dict[SyncState, SyncState | None] = {init: None}
    edge_label: dict[SyncState, str] = {}
    queue: deque[SyncState] = deque([init])

    saw_parallel_deletes = False
    violations: list[tuple[str, str, SyncState]] = []
    terminal_states = 0
    deadlock_states = 0

    def reconstruct_trace(st: SyncState) -> list[str]:
        steps: list[str] = []
        cur: SyncState | None = st
        while cur is not None and visited[cur] is not None:
            steps.append(edge_label[cur])
            cur = visited[cur]
        steps.reverse()
        return steps

    def record_violation(inv: str, detail: str, st: SyncState) -> None:
        if not any(v[0] == inv for v in violations):
            violations.append((inv, detail, st))

    def is_clean_terminal(st: SyncState) -> bool:
        return (
            st.d1_pc in ("IDLE", "DONE_OK", "DONE_ERR")
            and st.d2_pc in ("IDLE", "DONE_OK", "DONE_ERR")
            and st.det_pc in ("IDLE", "DONE_OK", "DONE_BATCH_ERR", "DONE_API_ERR")
            and st.rel_pc in ("IDLE", "DONE_OK", "DONE_BATCH_ERR", "DONE_API_ERR")
            and (st.sig_host == 0 or st.sig_pc in ("DONE_OK", "DONE_BATCH_ERR", "DONE_API_ERR"))
            and st.del_slots == (0, 0)
            and st.lc_owner is None
        )

    while queue:
        s = queue.popleft()

        in_http_count = int(s.d1_pc == "IN_HTTP") + int(s.d2_pc == "IN_HTTP")
        if in_http_count >= 2:
            saw_parallel_deletes = True

        # Inv 2 (Cap)
        if s.del_slots[0] + s.del_slots[1] > cfg.cap or in_http_count > cfg.cap:
            record_violation("Inv2_Cap", f"slots={s.del_slots}, in_http={in_http_count}, cap={cfg.cap}", s)

        # Inv 3 (Lease safety): neither detach's Lease write nor release's deletecollection/Lease delete
        # executes while any OTHER thread has an active claim DELETE in flight.
        for actor, tid, pc in (
            ("DET", 3, s.det_pc),
            ("REL", 4, s.rel_pc),
            ("SIG", s.sig_host, s.sig_pc),
        ):
            if pc in ("LEASE_WRITE", "DELETE_ROUND"):
                other_d1_http = s.d1_pc in ("PRE_HTTP", "IN_HTTP", "RETRY_CHECK") and tid != 1
                other_d2_http = s.d2_pc in ("PRE_HTTP", "IN_HTTP", "RETRY_CHECK") and tid != 2
                if other_d1_http or other_d2_http:
                    record_violation(
                        "Inv3_LeaseSafety",
                        f"{actor}(tid={tid}) in {pc} while other delete active (d1={s.d1_pc}, d2={s.d2_pc})",
                        s,
                    )

        # Inv 4a (Mutual exclusion of LEASE_WRITE / DELETE_ROUND)
        active_ops = []
        if s.det_pc == "LEASE_WRITE" and not _is_paused_by_signal(s, 3):
            active_ops.append("DET:LEASE_WRITE")
        if s.rel_pc == "DELETE_ROUND":
            active_ops.append("REL:DELETE_ROUND")
        if s.sig_pc in ("LEASE_WRITE", "DELETE_ROUND"):
            active_ops.append(f"SIG:{s.sig_pc}")
        if len(active_ops) > 1:
            record_violation("Inv4_Mutex", f"simultaneous lifecycle ops: {active_ops}", s)

        # Inv 4b (12/N rule: same-thread release() inside detach() makes outer detach() raise BatchError)
        if s.sig_host == 3 and s.sig_kind == "release" and s.sig_pc in ("DONE_OK", "DONE_API_ERR"):
            if s.det_pc == "DONE_OK":
                record_violation("Inv4_SameThreadReleaseInDetach", "outer detach succeeded after reentrant release", s)

        # Inv 4c (12/N rule: first cross-thread actor to enter _lifecycle_lock wins)
        if s.sig_host == 0 and not s.followup_used:
            if s.first_lc_winner == "detach" and s.det_pc == "DONE_BATCH_ERR" and s.rel_pc == "DONE_OK":
                record_violation("Inv4_FirstCallWins", "detach entered _lifecycle_lock first but release won", s)
            if s.first_lc_winner == "release" and s.rel_pc == "DONE_BATCH_ERR" and s.det_pc == "DONE_OK":
                record_violation("Inv4_FirstCallWins", "release entered _lifecycle_lock first but detach won", s)

        # Inv 6 (Post-detach refusal)
        if s.d1_started_after_detach or s.d2_started_after_detach:
            record_violation(
                "Inv6_PostDetachRefusal",
                f"HTTP DELETE started after detached=True (d1={s.d1_started_after_detach}, d2={s.d2_started_after_detach})",
                s,
            )

        next_edges = list(sync_transitions(s, cfg))
        if not next_edges:
            terminal_states += 1
            if not is_clean_terminal(s):
                deadlock_states += 1
                record_violation("Inv5_NoDeadlockOrLeak", f"stuck terminal state: {s}", s)
            continue

        for lbl, ns in next_edges:
            if ns not in visited:
                visited[ns] = s
                edge_label[ns] = lbl
                queue.append(ns)

    # Backwards reachability for Inv 5 liveness across ALL reachable states
    rev_adj: dict[SyncState, list[SyncState]] = {st: [] for st in visited}
    clean_terminals: list[SyncState] = []
    for st in visited:
        outs = list(sync_transitions(st, cfg))
        if not outs and is_clean_terminal(st):
            clean_terminals.append(st)
        for _, nxt in outs:
            rev_adj[nxt].append(st)

    can_terminate: set[SyncState] = set(clean_terminals)
    bqueue: deque[SyncState] = deque(clean_terminals)
    while bqueue:
        cur = bqueue.popleft()
        for prev in rev_adj[cur]:
            if prev not in can_terminate:
                can_terminate.add(prev)
                bqueue.append(prev)

    livestuck = len(visited) - len(can_terminate)
    if livestuck > 0:
        sample = next(st for st in visited if st not in can_terminate)
        record_violation("Inv5_Liveness", f"{livestuck} reachable states cannot reach clean termination", sample)

    if cfg.cap >= 2 and not saw_parallel_deletes:
        record_violation("Inv1_Parallelism", "never observed 2 concurrent HTTP DELETEs with cap>=2", init)

    return {
        "name": name,
        "states": len(visited),
        "terminals": terminal_states,
        "deadlocks": deadlock_states,
        "livestuck": livestuck,
        "saw_parallel_deletes": saw_parallel_deletes,
        "violations": [(inv, detail, reconstruct_trace(st)) for inv, detail, st in violations],
    }


# ============================================================================
# 2. ASYNC MODEL (asyncio.Lock + Semaphore(cap) + _active_deletes + CancelledError
#                 + _stop_batches client.close() + detach/release)
# ============================================================================


@dataclass(frozen=True)
class AsyncConfig:
    cap: int = 2
    wait_inside_lc_lock: bool = False
    await_lock_in_finally: bool = False
    check_only_lease_released: bool = False
    stop_batches_waits_active_deletes: bool = True


@dataclass(frozen=True)
class AsyncState:
    detached: bool = False
    detach_grace_set: bool = False
    lease_released: bool = False
    released: bool = False

    lock_busy: bool = False        # another coroutine transiently holding self._lock
    lc_locked: bool = False        # self._lifecycle_lock (asyncio.Lock)
    sem_used: int = 0              # permits taken from self._delete_slots (0..cap)
    active_deletes: int = 0        # self._active_deletes
    no_active_event: bool = True   # self._no_active_deletes.is_set()

    d1_pc: str = "IDLE"
    d1_started_after_detach: bool = False
    d2_pc: str = "IDLE"
    d2_started_after_detach: bool = False

    det_pc: str = "IDLE"
    rel_pc: str = "IDLE"
    stop_pc: str = "IDLE"
    first_lc_winner: str = ""


def _async_entry_blocks(s: AsyncState) -> bool:
    return s.detached


def _async_lease_check_blocks(s: AsyncState, cfg: AsyncConfig) -> bool:
    return s.lease_released if cfg.check_only_lease_released else s.detached


def async_transitions(s: AsyncState, cfg: AsyncConfig) -> Iterable[tuple[str, AsyncState]]:
    if not s.lock_busy:
        if any(p in ("WAIT_LOCK_ENTER", "FINALLY_WAIT_LOCK") for p in (s.d1_pc, s.d2_pc)):
            yield ("LOCK:contend", AsyncState(**{**s.__dict__, "lock_busy": True}))
    else:
        yield ("LOCK:release", AsyncState(**{**s.__dict__, "lock_busy": False}))

    # --- Delete tasks D1, D2 ---
    for idx, pc, name in ((0, s.d1_pc, "D1"), (1, s.d2_pc, "D2")):
        pc_field = "d1_pc" if idx == 0 else "d2_pc"
        flag_field = "d1_started_after_detach" if idx == 0 else "d2_started_after_detach"

        if pc == "IDLE":
            if _async_entry_blocks(s):
                yield (f"{name}:entry_refuse", AsyncState(**{**s.__dict__, pc_field: "DONE_ERR"}))
            elif s.sem_used < cfg.cap:
                yield (
                    f"{name}:acq_sem",
                    AsyncState(**{**s.__dict__, pc_field: "WAIT_LOCK_ENTER", "sem_used": s.sem_used + 1}),
                )
            else:
                yield (f"{name}:wait_sem", AsyncState(**{**s.__dict__, pc_field: "WAIT_SEM"}))

        elif pc == "WAIT_SEM":
            yield (f"{name}:cancel_at_sem", AsyncState(**{**s.__dict__, pc_field: "DONE_CANCELLED"}))
            if s.sem_used < cfg.cap:
                yield (
                    f"{name}:wake_sem",
                    AsyncState(**{**s.__dict__, pc_field: "WAIT_LOCK_ENTER", "sem_used": s.sem_used + 1}),
                )

        elif pc == "WAIT_LOCK_ENTER":
            yield (
                f"{name}:cancel_at_lock_enter",
                AsyncState(**{**s.__dict__, pc_field: "DONE_CANCELLED", "sem_used": s.sem_used - 1}),
            )
            if not s.lock_busy:
                if _async_entry_blocks(s):
                    yield (
                        f"{name}:refuse_under_lock",
                        AsyncState(**{**s.__dict__, pc_field: "DONE_ERR", "sem_used": s.sem_used - 1}),
                    )
                else:
                    yield (
                        f"{name}:enter_active",
                        AsyncState(
                            **{
                                **s.__dict__,
                                pc_field: "PRE_HTTP",
                                "active_deletes": s.active_deletes + 1,
                                "no_active_event": False,
                            }
                        ),
                    )

        elif pc == "PRE_HTTP":
            if _async_lease_check_blocks(s, cfg):
                if cfg.await_lock_in_finally:
                    yield (f"{name}:abort_to_finally", AsyncState(**{**s.__dict__, pc_field: "FINALLY_WAIT_LOCK"}))
                else:
                    na = s.active_deletes - 1
                    yield (
                        f"{name}:abort_sync_finally",
                        AsyncState(
                            **{
                                **s.__dict__,
                                pc_field: "DONE_ERR",
                                "active_deletes": na,
                                "no_active_event": (na == 0),
                                "sem_used": s.sem_used - 1,
                            }
                        ),
                    )
            else:
                yield (
                    f"{name}:start_http",
                    AsyncState(**{**s.__dict__, pc_field: "IN_HTTP", flag_field: s.detached}),
                )

        elif pc == "IN_HTTP":
            yield (f"{name}:http_503_retry", AsyncState(**{**s.__dict__, pc_field: "PRE_HTTP"}))
            if cfg.await_lock_in_finally:
                yield (f"{name}:http_done_to_finally", AsyncState(**{**s.__dict__, pc_field: "FINALLY_WAIT_LOCK"}))
                yield (f"{name}:cancel_mid_http_to_finally", AsyncState(**{**s.__dict__, pc_field: "FINALLY_WAIT_LOCK"}))
            else:
                na = s.active_deletes - 1
                yield (
                    f"{name}:http_ok",
                    AsyncState(
                        **{
                            **s.__dict__,
                            pc_field: "DONE_OK",
                            "active_deletes": na,
                            "no_active_event": (na == 0),
                            "sem_used": s.sem_used - 1,
                        }
                    ),
                )
                yield (
                    f"{name}:cancel_mid_http",
                    AsyncState(
                        **{
                            **s.__dict__,
                            pc_field: "DONE_CANCELLED",
                            "active_deletes": na,
                            "no_active_event": (na == 0),
                            "sem_used": s.sem_used - 1,
                        }
                    ),
                )

        elif pc == "FINALLY_WAIT_LOCK":
            if s.lock_busy:
                yield (
                    f"{name}:second_cancel_in_finally_LEAK",
                    AsyncState(**{**s.__dict__, pc_field: "DONE_CANCELLED", "sem_used": s.sem_used - 1}),
                )
            else:
                na = s.active_deletes - 1
                yield (
                    f"{name}:finally_lock_ok",
                    AsyncState(
                        **{
                            **s.__dict__,
                            pc_field: "DONE_OK",
                            "active_deletes": na,
                            "no_active_event": (na == 0),
                            "sem_used": s.sem_used - 1,
                        }
                    ),
                )

    # --- DETACH task ---
    if s.det_pc == "IDLE" and not s.lock_busy:
        if s.detached and not s.detach_grace_set:
            yield ("DET:fail_fast", AsyncState(**{**s.__dict__, "det_pc": "DONE_BATCH_ERR"}))
        elif s.lease_released:
            yield ("DET:noop", AsyncState(**{**s.__dict__, "det_pc": "DONE_OK"}))
        else:
            next_pc = "WAIT_LC_LOCK" if cfg.wait_inside_lc_lock else "WAIT_NO_ACTIVE_OUT"
            yield (
                "DET:begin",
                AsyncState(**{**s.__dict__, "detached": True, "detach_grace_set": True, "det_pc": next_pc}),
            )
    elif s.det_pc == "WAIT_NO_ACTIVE_OUT" and s.no_active_event:
        yield ("DET:drained_out", AsyncState(**{**s.__dict__, "det_pc": "WAIT_LC_LOCK"}))
    elif s.det_pc == "WAIT_LC_LOCK" and not s.lc_locked and not s.lock_busy:
        winner = s.first_lc_winner or "detach"
        if cfg.wait_inside_lc_lock:
            yield (
                "DET:acq_lc",
                AsyncState(**{**s.__dict__, "lc_locked": True, "first_lc_winner": winner, "det_pc": "WAIT_NO_ACTIVE_IN"}),
            )
        else:
            if s.detached and not s.detach_grace_set:
                yield ("DET:abort_released", AsyncState(**{**s.__dict__, "det_pc": "DONE_BATCH_ERR"}))
            elif s.lease_released:
                yield ("DET:already_done", AsyncState(**{**s.__dict__, "det_pc": "DONE_OK"}))
            else:
                yield (
                    "DET:acq_lc_enter_write",
                    AsyncState(**{**s.__dict__, "lc_locked": True, "first_lc_winner": winner, "det_pc": "LEASE_WRITE"}),
                )
    elif s.det_pc == "WAIT_NO_ACTIVE_IN" and not s.lock_busy:
        if s.detached and not s.detach_grace_set:
            yield ("DET:abort_released", AsyncState(**{**s.__dict__, "lc_locked": False, "det_pc": "DONE_BATCH_ERR"}))
        elif s.lease_released:
            yield ("DET:already_done", AsyncState(**{**s.__dict__, "lc_locked": False, "det_pc": "DONE_OK"}))
        elif s.no_active_event:
            yield ("DET:drained", AsyncState(**{**s.__dict__, "det_pc": "LEASE_WRITE"}))
    elif s.det_pc == "LEASE_WRITE" and not s.lock_busy:
        yield (
            "DET:write_ok",
            AsyncState(**{**s.__dict__, "lease_released": True, "lc_locked": False, "det_pc": "DONE_OK"}),
        )
        yield (
            "DET:write_503",
            AsyncState(**{**s.__dict__, "lc_locked": False, "det_pc": "DONE_API_ERR"}),
        )

    # --- RELEASE task ---
    if s.rel_pc == "IDLE":
        if cfg.wait_inside_lc_lock:
            yield ("REL:want_lc", AsyncState(**{**s.__dict__, "rel_pc": "WAIT_LC_LOCK"}))
        elif not s.lock_busy:
            if s.released:
                yield ("REL:noop", AsyncState(**{**s.__dict__, "rel_pc": "DONE_OK"}))
            elif s.lease_released:
                yield ("REL:err_detached", AsyncState(**{**s.__dict__, "rel_pc": "DONE_BATCH_ERR"}))
            else:
                yield (
                    "REL:mark_detached_wait_active",
                    AsyncState(**{**s.__dict__, "detached": True, "rel_pc": "WAIT_NO_ACTIVE_OUT"}),
                )
    elif s.rel_pc == "WAIT_NO_ACTIVE_OUT" and s.no_active_event:
        yield ("REL:drained_out", AsyncState(**{**s.__dict__, "rel_pc": "WAIT_LC_LOCK"}))
    elif s.rel_pc == "WAIT_LC_LOCK" and not s.lc_locked and not s.lock_busy:
        winner = s.first_lc_winner or "release"
        if s.released:
            yield ("REL:noop", AsyncState(**{**s.__dict__, "rel_pc": "DONE_OK"}))
        elif s.lease_released:
            yield ("REL:err_detached", AsyncState(**{**s.__dict__, "rel_pc": "DONE_BATCH_ERR"}))
        else:
            next_pc = "WAIT_NO_ACTIVE_IN" if cfg.wait_inside_lc_lock else "DELETE_ROUND"
            yield (
                "REL:acq_lc",
                AsyncState(
                    **{
                        **s.__dict__,
                        "lc_locked": True,
                        "detached": True,
                        "detach_grace_set": False,
                        "first_lc_winner": winner,
                        "rel_pc": next_pc,
                    }
                ),
            )
    elif s.rel_pc == "WAIT_NO_ACTIVE_IN" and s.no_active_event:
        yield ("REL:drained", AsyncState(**{**s.__dict__, "rel_pc": "DELETE_ROUND"}))
    elif s.rel_pc == "DELETE_ROUND" and not s.lock_busy:
        yield (
            "REL:delete_ok",
            AsyncState(
                **{
                    **s.__dict__,
                    "lease_released": True,
                    "released": True,
                    "lc_locked": False,
                    "rel_pc": "DONE_OK",
                }
            ),
        )

    # --- STOP_BATCHES (AsyncSandboxClient.close() -> _stop_batches) ---
    if s.stop_pc == "IDLE" and not s.lock_busy:
        if not (s.detach_grace_set and not s.lease_released):
            next_stop = "WAIT_ACTIVE" if cfg.stop_batches_waits_active_deletes else "CLIENT_CLOSED"
            yield (
                "CLOSE:stop_batches",
                AsyncState(**{**s.__dict__, "detached": True, "stop_pc": next_stop}),
            )
    elif s.stop_pc == "WAIT_ACTIVE" and s.no_active_event:
        yield ("CLOSE:drained_close_http", AsyncState(**{**s.__dict__, "stop_pc": "CLIENT_CLOSED"}))


def check_async_model(name: str, cfg: AsyncConfig) -> dict[str, object]:
    init = AsyncState()
    visited: dict[AsyncState, AsyncState | None] = {init: None}
    edge_label: dict[AsyncState, str] = {}
    queue: deque[AsyncState] = deque([init])

    saw_parallel_deletes = False
    violations: list[tuple[str, str, AsyncState]] = []
    terminal_states = 0
    deadlock_states = 0

    def reconstruct_trace(st: AsyncState) -> list[str]:
        steps: list[str] = []
        cur: AsyncState | None = st
        while cur is not None and visited[cur] is not None:
            steps.append(edge_label[cur])
            cur = visited[cur]
        steps.reverse()
        return steps

    def record_violation(inv: str, detail: str, st: AsyncState) -> None:
        if not any(v[0] == inv for v in violations):
            violations.append((inv, detail, st))

    def is_clean_terminal(st: AsyncState) -> bool:
        return (
            st.d1_pc in ("IDLE", "DONE_OK", "DONE_ERR", "DONE_CANCELLED")
            and st.d2_pc in ("IDLE", "DONE_OK", "DONE_ERR", "DONE_CANCELLED")
            and st.det_pc in ("IDLE", "DONE_OK", "DONE_BATCH_ERR", "DONE_API_ERR")
            and st.rel_pc in ("IDLE", "DONE_OK", "DONE_BATCH_ERR")
            and st.stop_pc in ("IDLE", "CLIENT_CLOSED")
            and st.sem_used == 0
            and st.active_deletes == 0
            and st.no_active_event is True
            and not st.lc_locked
            and not st.lock_busy
        )

    while queue:
        s = queue.popleft()

        in_http = int(s.d1_pc == "IN_HTTP") + int(s.d2_pc == "IN_HTTP")
        if in_http >= 2:
            saw_parallel_deletes = True

        # Inv 2 (Cap)
        if s.sem_used > cfg.cap or s.active_deletes > cfg.cap or in_http > cfg.cap:
            record_violation("Inv2_Cap", f"sem={s.sem_used}, active={s.active_deletes}, http={in_http}", s)

        # Inv 3 (Lease & session safety)
        if s.det_pc == "LEASE_WRITE" or s.rel_pc == "DELETE_ROUND":
            if s.active_deletes > 0 or in_http > 0:
                record_violation(
                    "Inv3_LeaseSafety",
                    f"det={s.det_pc}, rel={s.rel_pc} while active_deletes={s.active_deletes}, in_http={in_http}",
                    s,
                )
        if s.stop_pc == "CLIENT_CLOSED" and in_http > 0 and cfg.stop_batches_waits_active_deletes:
            record_violation("Inv3_CloseSafety", f"k8s_helper closed while in_http={in_http}", s)

        # Inv 4 (Mutex & First-call-wins on _lifecycle_lock)
        if s.det_pc == "LEASE_WRITE" and s.rel_pc == "DELETE_ROUND":
            record_violation("Inv4_Mutex", "DET and REL simultaneously writing/deleting", s)
        if s.first_lc_winner == "detach" and s.det_pc == "DONE_BATCH_ERR" and s.rel_pc == "DONE_OK":
            record_violation("Inv4_FirstCallWins", "detach entered _lifecycle_lock first but release won", s)
        if s.first_lc_winner == "release" and s.rel_pc == "DONE_BATCH_ERR" and s.det_pc == "DONE_OK":
            record_violation("Inv4_FirstCallWins", "release entered _lifecycle_lock first but detach won", s)

        # Inv 6 (Post-detach refusal)
        if s.d1_started_after_detach or s.d2_started_after_detach:
            record_violation(
                "Inv6_PostDetachRefusal",
                f"HTTP DELETE started after detached=True (d1={s.d1_started_after_detach}, d2={s.d2_started_after_detach})",
                s,
            )

        next_edges = list(async_transitions(s, cfg))
        if not next_edges:
            terminal_states += 1
            if not is_clean_terminal(s):
                deadlock_states += 1
                record_violation("Inv5_NoDeadlockOrLeak", f"stuck/leaked terminal state: {s}", s)
            continue

        for lbl, ns in next_edges:
            if ns not in visited:
                visited[ns] = s
                edge_label[ns] = lbl
                queue.append(ns)

    rev_adj: dict[AsyncState, list[AsyncState]] = {st: [] for st in visited}
    clean_terminals: list[AsyncState] = []
    for st in visited:
        outs = list(async_transitions(st, cfg))
        if not outs and is_clean_terminal(st):
            clean_terminals.append(st)
        for _, nxt in outs:
            rev_adj[nxt].append(st)

    can_terminate: set[AsyncState] = set(clean_terminals)
    bqueue: deque[AsyncState] = deque(clean_terminals)
    while bqueue:
        cur = bqueue.popleft()
        for prev in rev_adj[cur]:
            if prev not in can_terminate:
                can_terminate.add(prev)
                bqueue.append(prev)

    livestuck = len(visited) - len(can_terminate)
    if livestuck > 0:
        sample = next(st for st in visited if st not in can_terminate)
        record_violation("Inv5_Liveness", f"{livestuck} reachable states cannot terminate cleanly", sample)

    if cfg.cap >= 2 and not saw_parallel_deletes:
        record_violation("Inv1_Parallelism", "never observed 2 concurrent HTTP DELETEs with cap>=2", init)

    close_during_http = sum(
        1 for st in visited if st.stop_pc == "CLIENT_CLOSED" and (st.d1_pc == "IN_HTTP" or st.d2_pc == "IN_HTTP")
    )

    return {
        "name": name,
        "states": len(visited),
        "terminals": terminal_states,
        "deadlocks": deadlock_states,
        "livestuck": livestuck,
        "saw_parallel_deletes": saw_parallel_deletes,
        "close_during_http_states": close_during_http,
        "violations": [(inv, detail, reconstruct_trace(st)) for inv, detail, st in violations],
    }


def main() -> None:
    print("=== EXHAUSTIVE FINITE-STATE MODEL CHECK RESULTS ===\n")

    sync_runs = [
        (
            "SYNC Ordering 2 [RECOMMENDED]: wait active deletes BEFORE _lifecycle_lock (cap=1, full signal + retry)",
            SyncConfig(cap=1, wait_inside_lc_lock=False),
            True,
        ),
        (
            "SYNC Ordering 2 [RECOMMENDED]: wait active deletes BEFORE _lifecycle_lock (cap=2, full signal + retry)",
            SyncConfig(cap=2, wait_inside_lc_lock=False),
            True,
        ),
        (
            "SYNC Ordering 1 (wait INSIDE _lifecycle_lock, signal ONLY on detach thread, cap=2)",
            SyncConfig(cap=2, wait_inside_lc_lock=True, enable_signal_on_delete_thread=False),
            True,
        ),
        (
            "SYNC Ordering 1 [DEADLOCK BUG]: wait INSIDE _lifecycle_lock + signal on delete thread (cap=1)",
            SyncConfig(cap=1, wait_inside_lc_lock=True, enable_signal_on_delete_thread=True),
            False,
        ),
        (
            "SYNC Ordering 1 [DEADLOCK BUG]: wait INSIDE _lifecycle_lock + signal on delete thread (cap=2)",
            SyncConfig(cap=2, wait_inside_lc_lock=True, enable_signal_on_delete_thread=True),
            False,
        ),
        (
            "SYNC Flaw A: release() clears _detach_grace=None BEFORE _lifecycle_lock (cap=2)",
            SyncConfig(cap=2, wait_inside_lc_lock=False, release_clears_grace_before_lc_lock=True),
            False,
        ),
        (
            "SYNC Flaw B: _check_lease_held checks only _lease_released, not _detached (cap=2)",
            SyncConfig(cap=2, wait_inside_lc_lock=False, check_only_lease_released=True),
            False,
        ),
        (
            "SYNC Flaw C: _wait_active_deletes does NOT subtract own-thread count (cap=1)",
            SyncConfig(cap=1, wait_inside_lc_lock=False, subtract_own_thread=False),
            False,
        ),
    ]

    for label, cfg, expect_pass in sync_runs:
        res = check_sync_model(label, cfg)
        passed = len(res["violations"]) == 0
        status = "PASS" if passed else "FAIL (expected negative control)" if not expect_pass else "UNEXPECTED FAIL"
        print(
            f"[{status}] {res['name']}\n"
            f"  reachable_states={res['states']}, terminals={res['terminals']}, "
            f"deadlocks={res['deadlocks']}, livestuck={res['livestuck']}, "
            f"parallel_deletes={res['saw_parallel_deletes']}"
        )
        for inv, detail, trace in res["violations"]:
            print(f"  VIOLATION {inv}: {detail}")
            print(f"    counterexample ({len(trace)} steps): {' -> '.join(trace)}")
        assert passed == expect_pass, f"Model {label} expectation mismatch: passed={passed}, expected={expect_pass}"
        print()

    async_runs = [
        (
            "ASYNC Ordering 2: wait active deletes BEFORE _lifecycle_lock (cap=1, cancel + stop_batches drain)",
            AsyncConfig(cap=1, wait_inside_lc_lock=False),
            True,
        ),
        (
            "ASYNC Ordering 2: wait active deletes BEFORE _lifecycle_lock (cap=2, cancel + stop_batches drain)",
            AsyncConfig(cap=2, wait_inside_lc_lock=False),
            True,
        ),
        (
            "ASYNC Ordering 1: wait active deletes INSIDE _lifecycle_lock (cap=1, cancel + stop_batches drain)",
            AsyncConfig(cap=1, wait_inside_lc_lock=True),
            True,
        ),
        (
            "ASYNC Ordering 1: wait active deletes INSIDE _lifecycle_lock (cap=2, cancel + stop_batches drain)",
            AsyncConfig(cap=2, wait_inside_lc_lock=True),
            True,
        ),
        (
            "ASYNC Variant E: _stop_batches() does NOT await _no_active_deletes (cap=2)",
            AsyncConfig(cap=2, wait_inside_lc_lock=False, stop_batches_waits_active_deletes=False),
            True,
        ),
        (
            "ASYNC Flaw B: _check_lease_held checks only _lease_released, not _detached (cap=2)",
            AsyncConfig(cap=2, wait_inside_lc_lock=False, check_only_lease_released=True),
            False,
        ),
        (
            "ASYNC Flaw D: finally awaits self._lock before decrementing _active_deletes (cap=2)",
            AsyncConfig(cap=2, wait_inside_lc_lock=False, await_lock_in_finally=True),
            False,
        ),
    ]

    for label, cfg, expect_pass in async_runs:
        res = check_async_model(label, cfg)
        passed = len(res["violations"]) == 0
        status = "PASS" if passed else "FAIL (expected negative control)" if not expect_pass else "UNEXPECTED FAIL"
        print(
            f"[{status}] {res['name']}\n"
            f"  reachable_states={res['states']}, terminals={res['terminals']}, "
            f"deadlocks={res['deadlocks']}, livestuck={res['livestuck']}, "
            f"parallel_deletes={res['saw_parallel_deletes']}, "
            f"close_during_http_states={res['close_during_http_states']}"
        )
        for inv, detail, trace in res["violations"]:
            print(f"  VIOLATION {inv}: {detail}")
            print(f"    counterexample ({len(trace)} steps): {' -> '.join(trace)}")
        assert passed == expect_pass, f"Model {label} expectation mismatch: passed={passed}, expected={expect_pass}"
        print()

    print("ALL MODEL CHECKS COMPLETED AND VERIFIED SUCCESSFULLY.")


if __name__ == "__main__":
    main()
