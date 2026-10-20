"""Handle-level golden traces: Python TOP (8a72827f) SandboxBatch._next wake-ups and consumer iteration.

Each scenario builds a real SandboxBatch (only the fields events()/iter_ready_groups()/wait_for_quorum()
and _next read: _lock, _changed, _state = a real BatchState, _detached, _fill_deadline), starts ONE
consumer thread on the real public method, and applies a timeline of ops from the main thread at
t = k * UNIT under the batch's Condition (upsert/mark_lost/note_error + notify_all, or detach). The
consumer records every item it gets and how it ends, with the time in whole UNITs. Nothing about the
handle is reimplemented here; the Go test replays the same timeline under testing/synctest.

Run from clients/python/agentic-sandbox-client of the TOP checkout:
    PYTHONPATH=$PWD python gen_handle_traces.py > handle_traces.json
"""
import json
import sys
import threading
import time

from k8s_agent_sandbox import batch_state, sandbox_batch
from k8s_agent_sandbox.exceptions import BatchError, BatchTimeoutError, QuorumUnreachableError
from k8s_agent_sandbox.models import BatchGroup

UNIT = 0.25
SIZE = "agents.x-k8s.io/batch-group-size"
MR = "agents.x-k8s.io/batch-group-min-ready"
GROUPS = [("a", 2, 1), ("b", 1, 1)]  # fill: hb-0, hb-1 -> a; hb-2 -> b


def claim(name, pool, st):
    size, mr = {p: (s, m) for p, s, m in GROUPS}[pool]
    c = {"metadata": {"name": name, "annotations": {SIZE: str(size), MR: str(mr)}}, "spec": {"warmPoolRef": {"name": pool}}}
    sb = {"name": f"sb-{name}", "podIPs": ["10.0.0.1"], "serviceFQDN": f"sb-{name}.ns.svc"}
    if st == "ready":
        c["status"] = {"conditions": [{"type": "Ready", "status": "True", "reason": "Ready", "message": ""}], "sandbox": sb}
    elif st == "failed":
        c["status"] = {"conditions": [{"type": "Ready", "status": "False", "reason": "PodFailed", "message": "boom"}]}
    return c


def err_name(e):
    for cls in (QuorumUnreachableError, BatchTimeoutError, BatchError):
        if isinstance(e, cls):
            return cls.__name__
    return type(e).__name__


def handle(fill_deadline_units):
    b = sandbox_batch.SandboxBatch.__new__(sandbox_batch.SandboxBatch)
    b.batch_id = "hb"
    b._lock = threading.Lock()
    b._changed = threading.Condition(b._lock)
    b._state = batch_state.BatchState("hb", [BatchGroup(warmpool=p, size=s, min_ready=m) for p, s, m in GROUPS])
    b._detached = False
    b._fill_deadline = None
    return b


def run(name, consumer, ops, fill_deadline_units=None):
    b = handle(fill_deadline_units)
    trace = []
    start = time.monotonic()
    if fill_deadline_units is not None:
        b._fill_deadline = start + fill_deadline_units * UNIT

    def at():
        return round((time.monotonic() - start) / UNIT)

    def consume():
        try:
            if consumer == "events":
                for ev in b.events():
                    trace.append([at(), f"{ev.type.value}:{ev.member.claim_name}"])
            elif consumer == "events-break-and-resume":
                for ev in b.events():
                    trace.append([at(), f"iter1 {ev.type.value}:{ev.member.claim_name}"])
                    break
                for ev in b.events():
                    trace.append([at(), f"iter2 {ev.type.value}:{ev.member.claim_name}"])
            elif consumer == "groups":
                for g in b.iter_ready_groups():
                    if g.error is not None:
                        trace.append([at(), f"group:{g.warmpool}:ERR:{err_name(g.error)}"])
                    else:
                        trace.append([at(), f"group:{g.warmpool}:{','.join(m.claim_name for m in g.members)}"])
            elif consumer == "quorum":
                ms = b.wait_for_quorum()
                trace.append([at(), f"quorum:{','.join(m.claim_name for m in ms)}"])
                return
            trace.append([at(), "end+err" if b._state.error() is not None else "end"])
        except Exception as e:  # noqa: BLE001
            trace.append([at(), f"raise:{err_name(e)}"])

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    for unit, op, *args in ops:
        time.sleep(max(0.0, start + unit * UNIT - time.monotonic()))
        with b._changed:
            if op == "upsert":
                b._state.upsert_claim(claim(*args))
            elif op == "lost":
                b._state.mark_lost(args[0])
            elif op == "error":
                b._state.note_error(RuntimeError("watch: forbidden"))
            elif op == "detach":
                b._detached = True
            b._changed.notify_all()
    t.join(timeout=20 * UNIT)
    if t.is_alive():
        trace.append([at(), "STILL BLOCKED"])
    return {"name": name, "consumer": consumer, "fill_deadline_units": fill_deadline_units,
            "ops": [[u, op, *([claim(*a) if op == "upsert" else a for a in [args]] if args else [])] for u, op, *args in ops],
            "trace": trace}


def ops_json(ops):
    out = []
    for u, op, *args in ops:
        out.append({"unit": u, "op": op, "claim": claim(*args) if op == "upsert" else None,
                    "name": args[0] if op == "lost" else None})
    return out


SCENARIOS = [
    ("events: every fill member Ready -> settled, ends at once", "events",
     [(1, "upsert", "hb-0", "a", "ready"), (2, "upsert", "hb-2", "b", "ready"), (3, "upsert", "hb-1", "a", "ready")], 8),
    ("events: wakes at the fill deadline and ends", "events", [(1, "upsert", "hb-0", "a", "ready")], 4),
    ("events: pending then deleted -> member_lost, then the fill deadline", "events",
     [(1, "upsert", "hb-0", "a", "pending"), (2, "lost", "hb-0"), (3, "upsert", "hb-1", "a", "failed")], 5),
    ("events: break after the first item, call again to continue", "events-break-and-resume",
     [(1, "upsert", "hb-0", "a", "ready"), (2, "upsert", "hb-2", "b", "ready"), (3, "upsert", "hb-1", "a", "ready")], 8),
    ("events: err() set while waiting -> ends (Python: no exception)", "events",
     [(1, "upsert", "hb-0", "a", "ready"), (2, "error")], 8),
    ("events: detach while waiting -> raises", "events", [(1, "upsert", "hb-0", "a", "ready"), (2, "detach")], 8),
    ("groups: a yields at min_ready, b times out at the fill deadline", "groups", [(1, "upsert", "hb-0", "a", "ready")], 4),
    ("groups: a unreachable after two failures", "groups",
     [(1, "upsert", "hb-0", "a", "failed"), (2, "upsert", "hb-1", "a", "failed"), (3, "upsert", "hb-2", "b", "ready")], 8),
    ("quorum: reached when the last group gets min_ready", "quorum",
     [(1, "upsert", "hb-0", "a", "ready"), (2, "upsert", "hb-2", "b", "ready")], 8),
    ("quorum: fill deadline -> BatchTimeoutError", "quorum", [(1, "upsert", "hb-0", "a", "ready")], 3),
    ("quorum: no fill deadline yet, Ready arrives late", "quorum",
     [(2, "upsert", "hb-2", "b", "ready"), (5, "upsert", "hb-1", "a", "ready")], None),
    ("quorum: detach while waiting -> raises", "quorum", [(1, "upsert", "hb-0", "a", "ready"), (2, "detach")], 8),
]

out = {"generator": {"python_commit": "8a72827f1db53c12a82229f49583105fd5df3e26", "python": sys.version.split()[0],
                     "unit_seconds": UNIT},
       "groups": [{"warmpool": p, "size": s, "min_ready": m} for p, s, m in GROUPS], "scenarios": []}
for name, consumer, ops, fd in SCENARIOS:
    r = run(name, consumer, ops, fd)
    out["scenarios"].append({"name": name, "consumer": consumer, "fill_deadline_units": fd, "ops": ops_json(ops),
                             "trace": r["trace"]})
json.dump(out, sys.stdout, indent=1)
