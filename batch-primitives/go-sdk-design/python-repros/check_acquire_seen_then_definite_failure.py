"""Repro (alignment A7, gap 2): an acquire whose earlier attempt committed (the watch saw the claim) and whose last attempt
failed definitely (e.g. 403 quota) calls drop_acquired(created=False). Does the state ask for a delete?
Run from clients/python/agentic-sandbox-client with PYTHONPATH=$PWD.
"""
from k8s_agent_sandbox.batch_state import BatchState
from k8s_agent_sandbox.models import BatchGroup

s = BatchState("b", [BatchGroup(warmpool="a", size=0, min_ready=0)])
name = s.start_acquire("a")
claim = {
    "metadata": {"name": name, "labels": {"agents.x-k8s.io/batch-id": "b"}, "resourceVersion": "5"},
    "spec": {"warmPoolRef": {"name": "a"}},
    "status": {},
}
s.upsert_claim(claim)  # the watch saw attempt 1's claim
print("member seen by the watch:", name in [m.claim_name for m in s.members()])
needs_delete = s.drop_acquired(name, created=False)  # attempt 3 got a 403
print(f"drop_acquired(created=False) -> needs delete = {needs_delete}; member still listed = "
      f"{name in [m.claim_name for m in s.members()]}")
