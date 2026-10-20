"""B16 scope: what Python TOP (8a72827f) get_batch does when no group can be reconstructed.

get_batch rebuilds groups only from claims that carry the fill annotations (batch_state.reconstruct_groups),
so an all-size-0 batch -- or a batch whose fill claims are all gone -- attaches with no groups. Unlike
claim_batch, _attach sets the fill deadline to now + quorum_timeout (sandbox_batch.py:377).

Drives the REAL SandboxBatch._attach against a stub K8sHelper (Lease present, no holder, quorum-timeout 2 s)
for two claim sets: none at all, and one acquire claim (no fill annotations). Then calls the real
wait_for_quorum(), events() and iter_ready_groups().
Usage (from clients/python/agentic-sandbox-client of the TOP checkout):
    PYTHONPATH=$PWD python check_attach_no_groups.py
"""
import threading
import time
from datetime import UTC, datetime

from kubernetes.client import V1Lease, V1LeaseSpec, V1ObjectMeta

from k8s_agent_sandbox import sandbox_batch
from k8s_agent_sandbox.constants import BATCH_ID_LABEL, BATCH_QUORUM_TIMEOUT_ANNOTATION


class StubHelper:
    def __init__(self, claims):
        self.claims = claims
        self.stop = threading.Event()

    def read_batch_lease(self, name, namespace, _request_timeout=None):
        now = datetime.now(UTC)
        return V1Lease(metadata=V1ObjectMeta(name=name, labels={}, annotations={BATCH_QUORUM_TIMEOUT_ANNOTATION: "2"},
                                             resource_version="7"),
                       spec=V1LeaseSpec(holder_identity=None, lease_duration_seconds=60, renew_time=now, acquire_time=now,
                                        lease_transitions=0))

    def list_sandbox_claim_objects(self, namespace, label_selector, _request_timeout=None):
        return list(self.claims), "100"

    def replace_batch_lease(self, name, namespace, lease, _request_timeout=None):
        return lease

    def watch_sandbox_claims(self, namespace, label_selector, rv, timeout_seconds, _request_timeout=None):
        self.stop.wait(1.0)  # an idle stream, so the real loop doesn't spin
        return iter(())


class StubClient:
    def __init__(self, helper):
        self.k8s_helper = helper


acquire_claim = {"metadata": {"name": "ball0-0", "resourceVersion": "99", "labels": {BATCH_ID_LABEL: "ball0"}},
                 "spec": {"warmPoolRef": {"name": "a"}}, "status": {}}

for label, claims in [("no claims", []), ("one acquire claim", [acquire_claim])]:
    helper = StubHelper(claims)
    b = sandbox_batch.SandboxBatch._attach(StubClient(helper), "ball0", "ns")
    print(f"[{label}] groups={b.groups} size={b.size} members={[m.claim_name for m in b.members()]}")
    b2 = sandbox_batch.SandboxBatch._attach(StubClient(helper), "ball0", "ns")
    t0 = time.monotonic()
    print(f"[{label}] events()={list(b2.events())!r} after {time.monotonic() - t0:.2f}s")
    b3 = sandbox_batch.SandboxBatch._attach(StubClient(helper), "ball0", "ns")
    t0 = time.monotonic()
    print(f"[{label}] iter_ready_groups()={list(b3.iter_ready_groups())!r} after {time.monotonic() - t0:.2f}s")
    t0 = time.monotonic()
    try:
        r = b.wait_for_quorum()
        print(f"[{label}] wait_for_quorum() returned {r!r} after {time.monotonic() - t0:.2f}s")
    except Exception as e:  # noqa: BLE001
        print(f"[{label}] wait_for_quorum() raised {type(e).__name__}: {e} after {time.monotonic() - t0:.2f}s")
    helper.stop.set()
    for h in (b, b2, b3):
        h._watch_stop.set()
        h._renew_stop.set()
