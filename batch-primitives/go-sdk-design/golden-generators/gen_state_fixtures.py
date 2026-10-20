"""Generates golden STATE-MACHINE fixtures for the Go batchState port from Python TOP (8a72827f).

Each scenario is (groups, next_ordinal, ops). Every op is applied to the real
k8s_agent_sandbox.batch_state.BatchState, and after every op the generator records the op's return
value plus a snapshot of the state (members, counters, quorum, settle flags, pending deletes,
group_failed). Nothing is hand-written except inputs. Run from clients/python/agentic-sandbox-client:
    PYTHONHASHSEED=0 python gen_state_fixtures.py > state_golden.json
"""
import json
import sys

import pydantic

from k8s_agent_sandbox import batch_state
from k8s_agent_sandbox.exceptions import (BatchError, BatchTimeoutError, QuorumUnreachableError,
                                          TerminalMemberError)
from k8s_agent_sandbox.models import BatchGroup

SIZE = "agents.x-k8s.io/batch-group-size"
MR = "agents.x-k8s.io/batch-group-min-ready"


def claim(name, pool, st="pending", fill=None, deleting=False):
    md = {"name": name}
    if fill is not None:
        md["annotations"] = {SIZE: str(fill[0]), MR: str(fill[1])}
    if deleting:
        md["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    c = {"metadata": md, "spec": {"warmPoolRef": {"name": pool}}}
    sb = {"name": f"sb-{name}", "podIPs": ["10.0.0.1"], "serviceFQDN": f"sb-{name}.ns.svc"}
    if st == "ready":
        c["status"] = {"conditions": [{"type": "Ready", "status": "True", "reason": "Ready", "message": ""}], "sandbox": sb}
    elif st == "notready":
        c["status"] = {"conditions": [{"type": "Ready", "status": "False", "reason": "SandboxNotReady", "message": "x"}], "sandbox": sb}
    elif st == "failed":
        c["status"] = {"conditions": [{"type": "Ready", "status": "False", "reason": "PodFailed", "message": "boom"}]}
    return c


def err_json(e):
    if e is None:
        return None
    if isinstance(e, QuorumUnreachableError):
        return {"type": "QuorumUnreachable", "pool": e.warmpool, "size": e.size, "min_ready": e.min_ready,
                "failed": e.failed, "lost": e.lost, "released": e.released}
    if isinstance(e, BatchTimeoutError):
        return {"type": "Timeout"}
    if isinstance(e, TerminalMemberError):
        return {"type": "TerminalMember", "claim": e.member.claim_name, "state": e.member.state.value}
    if isinstance(e, BatchError):
        return {"type": "BatchError"}
    if isinstance(e, ValueError):
        return {"type": "ValueError"}
    return {"type": type(e).__name__}


def member_json(m):
    return {"claim_name": m.claim_name, "sandbox_name": m.sandbox_name or "", "warmpool": m.warmpool,
            "pod_ips": list(m.pod_ips), "service_fqdn": m.service_fqdn or "", "state": m.state.value,
            "reason": m.reason or "", "message": m.message or ""}


MODES = {"stream": batch_state.ConsumerMode.STREAM, "group": batch_state.ConsumerMode.GROUP,
         "quorum": batch_state.ConsumerMode.QUORUM}


def apply(s, op):
    k = op[0]
    if k == "upsert":
        m = s.upsert_claim(op[1])
        return None if m is None else m.state.value
    if k == "seed":
        s.seed_from_claims(op[1])
        return None
    if k == "lost":
        m = s.mark_lost(op[1])
        return None if m is None else m.state.value
    if k == "resync":
        s.resync_from_list(op[1])
        return None
    if k == "set_mode":
        s.set_mode(MODES[op[1]])
        return None
    if k == "release_member":
        return s.release_member(op[1])
    if k == "release_not_ready":
        return sorted(s.release_not_ready())
    if k == "release_group":
        return s.release_group(op[1])
    if k == "start_acquire":
        return s.start_acquire(op[1])
    if k == "acquired_member":
        m = s.acquired_member(op[1])
        return None if m is None else m.claim_name
    if k == "drop_acquired":
        return s.drop_acquired(op[1], op[2])
    if k == "note_deleted":
        s.note_deleted(op[1])
        return None
    if k == "record_create_failure":
        s.record_create_failure(op[1], op[2], op[3])
        return None
    if k == "record_skipped_create":
        s.record_skipped_create()
        return None
    if k == "expire_fill":
        return s.expire_fill()
    if k == "count_missing_fill":
        s.count_missing_fill_as_unable()
        return None
    if k == "fail_quorum_timeout":
        ms, e = s.fail_quorum(BatchTimeoutError("x"))
        return {"members": [m.claim_name for m in ms], "error": err_json(e)}
    if k == "fill_creates_released":
        return s.fill_creates_released(op[1])
    if k == "has_pool":
        return s.has_pool(op[1])
    if k == "pop_events":
        out = []
        while (ev := s.pop_event()) is not None:
            out.append({"type": ev.type.value, "claim": ev.member.claim_name, "state": ev.member.state.value})
        return out
    if k == "pop_groups":
        out = []
        while (g := s.pop_group_outcome()) is not None:
            out.append({"pool": g.warmpool, "members": [m.claim_name for m in g.members], "error": err_json(g.error)})
        return out
    raise KeyError(k)


def snapshot(s, pools):
    q = s.quorum_result()
    return {
        "members": [member_json(m) for m in s.members()],
        "next_ordinal": s.next_ordinal,
        "fill_settled": s.fill_settled(),
        "events_done": s.events_done(),
        "groups_done": s.groups_done(),
        "quorum": None if q is None else {"members": [m.claim_name for m in q[0]], "error": err_json(q[1])},
        "ready_count": s._ready_count,
        "unable": len(s._unable),
        "never_created": s._never_created,
        "failed": dict(s._failed),
        "lost": dict(s._lost),
        "released": dict(s._released_count),
        "pending_deletes": sorted(s._pending_deletes),
        "group_failed": {p: s.group_failed(p) for p in pools},
        "error": err_json(s.error()),
    }


def run(name, groups, ops, next_ordinal=0):
    gs = [BatchGroup(warmpool=p, size=n, min_ready=m) for p, n, m in groups]
    s = batch_state.BatchState("b", gs, next_ordinal)
    pools = [g.warmpool for g in gs]
    steps = []
    for op in ops:
        try:
            ret, err = apply(s, op), None
        except Exception as e:  # noqa: BLE001
            ret, err = None, err_json(e)
        steps.append({"op": op, "ret": ret, "raises": err, "state": snapshot(s, pools)})
    return {"name": name, "groups": [{"warmpool": g.warmpool, "size": g.size, "min_ready": g.min_ready} for g in gs],
            "next_ordinal": next_ordinal, "steps": steps}


A = ("a", 3, 2)
B = ("b", 2, 1)
fa = (3, 2)
fb = (2, 1)
sc = []

# --- upsert / ready accounting / STREAM ------------------------------------------------------------
sc.append(run("stream basic", [A, B], [
    ["upsert", claim("b-0", "a", "pending", fa)], ["upsert", claim("b-0", "a", "ready", fa)],
    ["set_mode", "stream"], ["pop_events"],
    ["upsert", claim("b-3", "b", "ready", fb)], ["upsert", claim("b-1", "a", "failed", fa)],
    ["upsert", claim("b-2", "a", "ready", fa)], ["pop_events"],
    ["upsert", claim("b-0", "a", "notready", fa)], ["upsert", claim("b-0", "a", "ready", fa)],
    ["pop_events"], ["lost", "b-4"], ["upsert", claim("b-4", "b", "pending", fb)], ["lost", "b-4"], ["pop_events"],
]))
sc.append(run("ready before any mode is held, then stream dispatches in ready order", [A, B], [
    ["upsert", claim("b-3", "b", "ready", fb)], ["upsert", claim("b-1", "a", "ready", fa)],
    ["upsert", claim("b-0", "a", "ready", fa)], ["pop_events"], ["set_mode", "stream"], ["pop_events"],
    ["set_mode", "group"], ["set_mode", "quorum"], ["set_mode", "stream"],
]))
# --- GROUP mode -------------------------------------------------------------------------------------
sc.append(run("group yields cohort then extras; failure path", [A, B], [
    ["set_mode", "group"], ["upsert", claim("b-0", "a", "ready", fa)], ["upsert", claim("b-1", "a", "ready", fa)],
    ["pop_groups"], ["pop_events"], ["upsert", claim("b-2", "a", "ready", fa)], ["pop_events"],
    ["upsert", claim("b-3", "b", "failed", fb)], ["pop_groups"], ["upsert", claim("b-4", "b", "failed", fb)],
    ["pop_groups"], ["pop_events"], ["set_mode", "group"], ["set_mode", "quorum"],
]))
sc.append(run("group outcome decided at set_mode, ready members after a failed group are dropped", [A, B], [
    ["upsert", claim("b-0", "a", "failed", fa)], ["upsert", claim("b-1", "a", "failed", fa)],
    ["upsert", claim("b-3", "b", "ready", fb)], ["set_mode", "group"], ["pop_groups"],
    ["upsert", claim("b-2", "a", "ready", fa)], ["pop_events"], ["pop_groups"],
]))
sc.append(run("group timeout via expire_fill, then late ready is dropped", [A], [
    ["upsert", claim("b-0", "a", "ready", fa)], ["set_mode", "group"], ["expire_fill"], ["expire_fill"],
    ["pop_groups"], ["upsert", claim("b-1", "a", "ready", fa)], ["pop_events"], ["pop_groups"],
]))
sc.append(run("expire before group mode -> undecided groups time out at set_mode", [A, B], [
    ["upsert", claim("b-3", "b", "ready", fb)], ["expire_fill"], ["set_mode", "group"], ["pop_groups"],
]))
# --- QUORUM mode ------------------------------------------------------------------------------------
sc.append(run("quorum reached, extras to events, later readies dispatched", [A, B], [
    ["set_mode", "quorum"], ["upsert", claim("b-0", "a", "ready", fa)], ["upsert", claim("b-3", "b", "ready", fb)],
    ["upsert", claim("b-4", "b", "ready", fb)], ["upsert", claim("b-1", "a", "ready", fa)], ["pop_events"],
    ["upsert", claim("b-2", "a", "ready", fa)], ["pop_events"], ["fail_quorum_timeout"], ["set_mode", "quorum"],
]))
sc.append(run("quorum unreachable via losses; group_failed true for every pool", [A, B], [
    ["upsert", claim("b-0", "a", "ready", fa)], ["set_mode", "quorum"], ["upsert", claim("b-1", "a", "pending", fa)],
    ["upsert", claim("b-2", "a", "pending", fa)], ["lost", "b-1"], ["lost", "b-2"], ["pop_events"],
    ["upsert", claim("b-3", "b", "ready", fb)], ["pop_events"],
]))
sc.append(run("quorum fail_quorum (destructive cancel) then ready dropped", [A], [
    ["set_mode", "quorum"], ["upsert", claim("b-0", "a", "ready", fa)], ["fail_quorum_timeout"],
    ["upsert", claim("b-1", "a", "ready", fa)], ["pop_events"],
]))
sc.append(run("quorum with expired fill fails at set_mode", [A], [
    ["expire_fill"], ["set_mode", "quorum"],
]))
sc.append(run("min_ready 0 groups: group and quorum decided at once", [("a", 2, 0), ("b", 1, 0)], [
    ["set_mode", "quorum"], ["upsert", claim("b-0", "a", "ready", (2, 0))], ["pop_events"],
]))
sc.append(run("min_ready 0 group mode", [("a", 2, 0)], [
    ["set_mode", "group"], ["pop_groups"], ["upsert", claim("b-0", "a", "ready", (2, 0))], ["pop_events"],
]))
# --- terminating / deleting -------------------------------------------------------------------------
sc.append(run("terminating claim is lost; failed stays failed through delete", [A], [
    ["set_mode", "stream"], ["upsert", claim("b-0", "a", "ready", fa)], ["upsert", claim("b-0", "a", "ready", fa, deleting=True)],
    ["upsert", claim("b-1", "a", "failed", fa)], ["upsert", claim("b-1", "a", "failed", fa, deleting=True)],
    ["lost", "b-1"], ["upsert", claim("b-1", "a", "ready", fa)], ["pop_events"],
]))
# --- resync -----------------------------------------------------------------------------------------
sc.append(run("resync marks missing lost and upserts the rest", [A, B], [
    ["seed", [claim("b-0", "a", "ready", fa), claim("b-1", "a", "pending", fa), claim("b-3", "b", "pending", fb)]],
    ["set_mode", "stream"], ["pop_events"],
    ["resync", [claim("b-0", "a", "ready", fa), claim("b-3", "b", "ready", fb), claim("b-2", "a", "pending", fa)]],
    ["pop_events"],
]))
sc.append(run("resync with two missing in one pool (order-insensitive)", [("a", 4, 1)], [
    ["seed", [claim("b-0", "a", "pending", (4, 1)), claim("b-1", "a", "pending", (4, 1)), claim("b-2", "a", "pending", (4, 1))]],
    ["set_mode", "stream"], ["resync", [claim("b-2", "a", "ready", (4, 1))]], ["pop_events"],
]))
# --- ordinals ---------------------------------------------------------------------------------------
sc.append(run("ordinals never reused; next_ordinal from arg, upserts and acquire", [A], [
    ["start_acquire", "x"], ["upsert", claim("b-9", "x", "pending")], ["start_acquire", "x"],
    ["upsert", claim("weird", "x", "pending")], ["upsert", claim("b-0", "a", "pending", fa)],
], next_ordinal=7))
sc.append(run("next_ordinal below size is raised to size", [A, B], [["start_acquire", "a"]], next_ordinal=2))
# --- acquire ----------------------------------------------------------------------------------------
sc.append(run("acquire lifecycle: pending, ready, then released later", [A], [
    ["start_acquire", "x"], ["acquired_member", "b-3"], ["upsert", claim("b-3", "x", "pending")],
    ["acquired_member", "b-3"], ["upsert", claim("b-3", "x", "ready")], ["acquired_member", "b-3"],
    ["has_pool", "x"], ["release_member", "b-3"], ["release_member", "b-3"], ["note_deleted", "b-3"],
    ["release_member", "b-3"], ["release_member", "nope"], ["has_pool", "zzz"],
]))
sc.append(run("acquire terminal and drop paths", [A], [
    ["start_acquire", "x"], ["upsert", claim("b-3", "x", "failed")], ["acquired_member", "b-3"],
    ["drop_acquired", "b-3", True], ["upsert", claim("b-3", "x", "ready")],
    ["start_acquire", "x"], ["drop_acquired", "b-4", True], ["start_acquire", "x"], ["drop_acquired", "b-5", False],
    ["start_acquire", "x"], ["upsert", claim("b-6", "x", "pending")], ["lost", "b-6"], ["acquired_member", "b-6"],
    ["drop_acquired", "b-6", True],
]))
sc.append(run("release_group releases members and in-flight acquires", [A, ("x", 0, 0)], [
    ["upsert", claim("b-0", "a", "ready", fa)], ["start_acquire", "a"], ["start_acquire", "x"],
    ["upsert", claim("b-4", "x", "ready")], ["release_group", "a"], ["acquired_member", "b-3"],
    ["drop_acquired", "b-3", True], ["drop_acquired", "b-3", False], ["fill_creates_released", "a"],
    ["upsert", claim("b-1", "a", "pending", fa)], ["record_create_failure", "b-2", "a", "nope"],
    ["release_group", "x"], ["pop_events"],
]))
# --- release_* and fill accounting -------------------------------------------------------------------
sc.append(run("release_not_ready skips ready and dispatched; group mode outcomes", [A, B], [
    ["set_mode", "group"], ["upsert", claim("b-0", "a", "ready", fa)], ["upsert", claim("b-1", "a", "ready", fa)],
    ["upsert", claim("b-2", "a", "pending", fa)], ["upsert", claim("b-3", "b", "failed", fb)],
    ["upsert", claim("b-4", "b", "pending", fb)], ["pop_groups"], ["release_not_ready"], ["pop_groups"],
    ["upsert", claim("b-2", "a", "ready", fa)], ["record_create_failure", "b-9", "b", "x"], ["pop_events"],
]))
sc.append(run("release_member of fill member makes group unreachable (quorum)", [A], [
    ["set_mode", "quorum"], ["upsert", claim("b-0", "a", "ready", fa)], ["upsert", claim("b-1", "a", "pending", fa)],
    ["release_member", "b-1"], ["release_member", "b-0"], ["pop_events"],
]))
sc.append(run("released ready member queued in events is skipped at pop", [A], [
    ["set_mode", "stream"], ["upsert", claim("b-0", "a", "ready", fa)], ["release_member", "b-0"], ["pop_events"],
]))
sc.append(run("create failures and skipped creates settle the fill", [A, B], [
    ["record_create_failure", "b-0", "a", "409"], ["record_create_failure", "b-0", "a", "again"],
    ["record_skipped_create"], ["record_skipped_create"], ["upsert", claim("b-3", "b", "ready", fb)],
    ["upsert", claim("b-4", "b", "ready", fb)], ["set_mode", "stream"], ["pop_events"],
    ["release_member", "b-0"], ["upsert", claim("b-2", "a", "ready", fa)],
]))
sc.append(run("record_create_failure after the watch saw the claim is ignored", [A], [
    ["upsert", claim("b-0", "a", "pending", fa)], ["record_create_failure", "b-0", "a", "x"],
]))
sc.append(run("late fill create after release_group is released on sight", [A], [
    ["release_group", "a"], ["upsert", claim("b-0", "a", "ready", fa)], ["upsert", claim("b-0", "a", "ready", fa)],
    ["pop_events"],
]))
sc.append(run("release_not_ready keeps ready members; their later updates still apply", [A], [
    ["set_mode", "stream"], ["upsert", claim("b-0", "a", "ready", fa)], ["release_not_ready"],
    ["upsert", claim("b-0", "a", "notready", fa)], ["upsert", claim("b-1", "a", "ready", fa)], ["pop_events"],
]))
sc.append(run("acquire released by release_group whose label delete succeeded needs no delete on drop", [A], [
    ["start_acquire", "a"], ["release_group", "a"], ["note_deleted", "b-3"], ["drop_acquired", "b-3", True],
]))
sc.append(run("an unable member that comes back Ready never re-enters the ready count", [A], [
    ["upsert", claim("b-0", "a", "ready", fa)], ["lost", "b-0"], ["upsert", claim("b-0", "a", "ready", fa)],
    ["upsert", claim("b-0", "a", "notready", fa)], ["upsert", claim("b-0", "a", "ready", fa)], ["set_mode", "stream"],
    ["pop_events"],
]))
# --- re-attach: missing fill ------------------------------------------------------------------------
sc.append(run("count_missing_fill after seed (re-attach)", [("a", 4, 2), ("b", 2, 2)], [
    ["seed", [claim("b-1", "a", "ready", (4, 2)), claim("b-5", "b", "ready", (2, 2)), claim("b-7", "x", "ready")]],
    ["count_missing_fill"], ["set_mode", "group"], ["pop_groups"], ["pop_events"],
], next_ordinal=8))
# --- all size 0 (acquire-only) ----------------------------------------------------------------------
sc.append(run("all groups size 0: quorum never decided by the state (handle would block)", [("a", 0, 0), ("b", 0, 0)], [
    ["set_mode", "quorum"], ["start_acquire", "a"], ["upsert", claim("b-0", "a", "ready")], ["pop_events"],
    ["expire_fill"],
]))
sc.append(run("all groups size 0: group and stream end at once", [("a", 0, 0)], [
    ["set_mode", "group"], ["pop_groups"], ["set_mode", "stream"],
]))
# --- non-fill claims are never counted ---------------------------------------------------------------
sc.append(run("claims without fill annotations or of unknown pools are not fill", [A], [
    ["set_mode", "stream"], ["upsert", claim("b-0", "a", "ready")], ["upsert", claim("b-1", "zz", "ready", (1, 1))],
    ["pop_events"], ["release_not_ready"],
]))

out = {"generator": {"python_commit": "8a72827f1db53c12a82229f49583105fd5df3e26", "python": sys.version.split()[0],
                     "pydantic": pydantic.VERSION, "PYTHONHASHSEED": "set by caller"},
       "scenarios": sc}
json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
