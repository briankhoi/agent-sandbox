"""Checks how Python TOP (8a72827f) handles a batch whose groups are ALL size 0 (acquire-only).

1. validate_claim_batch_args accepts it (no total-size check), and the fill shutdown math with a tiny
   create_rps does not raise (ceil(0/rps) == 0), so F5's tiny-rps pacer case is reachable from
   claim_batch, not just from get_batch.
2. claim_batch leaves _fill_deadline None (no plan -> no creator sets it), so wait_for_quorum() on
   such a handle has nothing to wake it: it blocks until detach/release/err(). Shown with the real
   wait_for_quorum on a handle built with only the fields it reads.
3. events() and group_outcomes() end at once.
"""
import math
import threading
import time

from k8s_agent_sandbox import batch_state, batch_utils, sandbox_batch
from k8s_agent_sandbox.models import BatchGroup

groups = [BatchGroup(warmpool="a", size=0), BatchGroup(warmpool="b", size=0, min_ready=0)]
args = batch_utils.validate_claim_batch_args(groups, None, "ball0", 5e-324, None, None, None, None)
size = sum(g.size for g in args.groups)
print(f"1. validate ok: size={size} create_rps={args.create_rps!r} "
      f"pace_seconds=ceil({size}/rps)={math.ceil(size / args.create_rps)} interval=1/rps={1 / args.create_rps!r}")
print(f"   fill_create_plan={batch_utils.fill_create_plan('ball0', args.groups)}")


def handle():
    b = sandbox_batch.SandboxBatch.__new__(sandbox_batch.SandboxBatch)
    b.batch_id = "ball0"
    b._lock = threading.Lock()
    b._changed = threading.Condition(b._lock)
    b._state = batch_state.BatchState("ball0", args.groups)
    b._detached = False
    b._released = False
    b._fill_deadline = None  # what claim_batch leaves when the plan is empty
    return b


b = handle()
res = {}


def run():
    try:
        res["v"] = b.wait_for_quorum()
    except Exception as e:  # noqa: BLE001
        res["v"] = f"{type(e).__name__}: {e}"


t = threading.Thread(target=run, daemon=True)
t.start()
t.join(timeout=3)
print(f"2. wait_for_quorum after 3s: {'STILL BLOCKED' if t.is_alive() else res['v']!r}; "
      f"state.quorum_result()={b._state.quorum_result()!r}")
b2 = handle()
print(f"3. events()={list(b2.events())!r}")
b3 = handle()
print(f"   group_outcomes()={list(b3.group_outcomes())!r}")
