# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Concurrency, CAS ownership, semaphore accounting, and lock-inversion stress suite (Pipeline 6).

Exercises high-contention multi-thread and multi-coroutine workloads with strict
deadlock watchdogs:
  1. 50-worker sync ThreadPoolExecutor acquire/connect/release_member/release_group churn.
  2. 50-worker async asyncio.gather acquire/connect/release_member/release_group churn.
  3. Atomic optimistic-concurrency Lease store racing detach(grace) against 8 get_batch() callers.
  4. Multi-creator fill flood racing against concurrent release_not_ready() and release().
  5. Live reproduction of AsyncSandboxClient.close holding self._lock across await
     close_connection() and starving concurrent batch registration / tracking (INV-LOCK-ASYNC-1).
"""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes import client as k8s_client
from kubernetes_asyncio import client as async_k8s_client

from k8s_agent_sandbox.async_sandbox_batch import AsyncSandboxBatch
from k8s_agent_sandbox.async_sandbox_client import AsyncSandboxClient
from k8s_agent_sandbox.constants import (
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
    BATCH_LEASE_DURATION_ANNOTATION,
    BATCH_NEXT_ORDINAL_ANNOTATION,
    BATCH_QUORUM_TIMEOUT_ANNOTATION,
    BATCH_WORK_BUDGET_ANNOTATION,
)
from k8s_agent_sandbox.exceptions import BatchError, BatchInUseError
from k8s_agent_sandbox.models import BatchGroup, MemberState, SandboxDirectConnectionConfig
from k8s_agent_sandbox.sandbox_batch import SandboxBatch
from k8s_agent_sandbox.sandbox_client import SandboxClient
from k8s_agent_sandbox.test.unit.batch_invariant_oracle import BatchInvariantOracle


def _make_lease(
    batch_id: str = "stressbatch",
    holder_identity: str | None = "holder-stress-1",
    resource_version: str = "10",
    uid: str = "lease-stress-uid-1",
    lease_duration_seconds: int = 60,
    annotations: dict[str, str] | None = None,
) -> k8s_client.V1Lease:
    base_ann = {
        BATCH_LEASE_DURATION_ANNOTATION: str(lease_duration_seconds),
        BATCH_QUORUM_TIMEOUT_ANNOTATION: "30",
        BATCH_WORK_BUDGET_ANNOTATION: "60",
    }
    if annotations:
        base_ann.update(annotations)
    return k8s_client.V1Lease(
        metadata=k8s_client.V1ObjectMeta(
            name=f"batch-{batch_id}",
            resource_version=resource_version,
            uid=uid,
            labels={"agents.x-k8s.io/batch-id": batch_id},
            annotations=base_ann,
        ),
        spec=k8s_client.V1LeaseSpec(
            holder_identity=holder_identity,
            renew_time=datetime.now(UTC),
            lease_duration_seconds=lease_duration_seconds,
            lease_transitions=0,
        ),
    )


def _ready_claim_dict(
    name: str,
    warmpool: str,
    *,
    group_spec: BatchGroup | None = None,
    rv: str = "15",
) -> dict[str, Any]:
    ann: dict[str, str] = {}
    if group_spec is not None:
        ann[BATCH_GROUP_SIZE_ANNOTATION] = str(group_spec.size)
        ann[BATCH_GROUP_MIN_READY_ANNOTATION] = str(group_spec.min_ready or 0)
    return {
        "metadata": {"name": name, "resourceVersion": rv, "annotations": ann},
        "spec": {"warmPoolRef": {"name": warmpool}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True", "reason": "SandboxReady"}],
            "sandbox": {"name": f"sbx-{name}", "podIPs": ["10.99.0.10"]},
        },
    }


def test_sync_50_worker_acquire_and_release_churn_no_deadlock_or_slot_leak() -> None:
    """Hammer SandboxBatch with 50 concurrent threads across acquire, connect,
    release_member, and release_group.
    """
    max_in_flight = 8
    with patch("k8s_agent_sandbox.sandbox_client.K8sHelper"):
        client: SandboxClient[Any] = SandboxClient(cleanup=False)
    helper: Any = client.k8s_helper
    helper.api_pool_size.return_value = 32
    helper.count_live_sandbox_claims.return_value = 0
    helper.get_sandbox_warmpool.return_value = {
        "spec": {"sandboxTemplateRef": {"name": "tmpl"}},
        "status": {"readyReplicas": 16},
    }
    helper.get_sandbox_template.return_value = {"metadata": {"name": "tmpl"}}
    helper.create_batch_lease.return_value = _make_lease()
    helper.list_sandbox_claim_objects.return_value = ([], "10")
    client_any: Any = client
    client_any.sandbox_class = MagicMock(
        side_effect=lambda **kw: MagicMock(
            sandbox_id=kw.get("sandbox_id"),
            claim_name=kw.get("claim_name"),
            close_connection=MagicMock(),
        )
    )

    with (
        patch.object(SandboxBatch, "_start", lambda _s: None),
        patch.object(SandboxBatch, "_create_worker", lambda _s: None),
    ):
        batch = client.claim_batch(
            [BatchGroup(warmpool="pool-a", size=0), BatchGroup(warmpool="pool-b", size=0)],
            batch_id="stressbatch",
            max_in_flight=max_in_flight,
            create_rps=5000.0,
            quorum_timeout=10,
        )

    create_counter = 0
    counter_lock = threading.Lock()

    def _on_create(cname: str, wpool: str, _ns: str, **_kw: Any) -> None:
        nonlocal create_counter
        with counter_lock:
            create_counter += 1
            n = create_counter
        if n % 11 == 0:
            raise k8s_client.ApiException(status=503, reason="Transient")
        time.sleep(0.002)
        obj = _ready_claim_dict(cname, wpool)
        with batch._lock:
            m = batch._state.upsert_claim(obj)
            if m is not None and m.state is not MemberState.PENDING:
                batch._wake_acquire(cname)

    def _on_delete(_cname: str, _ns: str, **_kw: Any) -> None:
        time.sleep(0.001)

    def _on_delete_label(_ns: str, _sel: str, **_kw: Any) -> None:
        time.sleep(0.002)

    helper.create_sandbox_claim.side_effect = _on_create
    helper.delete_sandbox_claim.side_effect = _on_delete
    helper.delete_sandbox_claims_by_label.side_effect = _on_delete_label

    def _worker(wid: int) -> None:
        pool = "pool-a" if wid % 3 != 0 else ("pool-b" if wid % 2 == 0 else "pool-c")
        if wid in (17, 34):
            time.sleep(0.01)
            try:
                batch.release_group("pool-b")
            except BatchError:
                pass
            return
        try:
            member = batch.acquire(pool, timeout=4.0)
            sbx = batch.connect(member)
            assert sbx is not None
            if wid % 2 == 0:
                batch.release_member(member)
        except BatchError:
            pass

    with ThreadPoolExecutor(max_workers=50) as pool_exec:
        futs = [pool_exec.submit(_worker, i) for i in range(50)]
        done, not_done = wait(futs, timeout=8.0)
        assert not not_done, f"Deadlock detected: {len(not_done)} sync workers hung past 8.0s!"
        for f in done:
            f.result()

    # Verify all max_in_flight permits returned cleanly.
    for i in range(max_in_flight):
        assert batch._in_flight.acquire(timeout=0.1), f"Permit {i} leaked in sync _in_flight!"
    for _ in range(max_in_flight):
        batch._in_flight.release()

    assert dict(batch._active_deletes) == {}
    assert dict(batch._acquire_waiters) == {}
    assert dict(batch._state._checking_acquires) == {}
    BatchInvariantOracle.assert_structural_invariants(
        batch._state,
        in_flight_acquire_names=set(),
    )


@pytest.mark.asyncio
async def test_async_50_worker_acquire_and_release_churn_no_deadlock_or_slot_leak() -> None:
    """Hammer AsyncSandboxBatch with 50 concurrent coroutines across acquire/connect/release."""
    max_in_flight = 8
    with patch("k8s_agent_sandbox.async_sandbox_client.AsyncK8sHelper"):
        client: AsyncSandboxClient[Any] = AsyncSandboxClient(
            connection_config=SandboxDirectConnectionConfig(api_url="http://127.0.0.1:8080"),
            cleanup=False,
        )
    helper: Any = client.k8s_helper
    helper.api_pool_size = AsyncMock(return_value=32)
    helper.count_live_sandbox_claims = AsyncMock(return_value=0)
    helper.get_sandbox_warmpool = AsyncMock(
        return_value={
            "spec": {"sandboxTemplateRef": {"name": "tmpl"}},
            "status": {"readyReplicas": 16},
        }
    )
    helper.get_sandbox_template = AsyncMock(return_value={"metadata": {"name": "tmpl"}})
    helper.create_batch_lease = AsyncMock(return_value=_make_lease())
    helper.list_sandbox_claim_objects = AsyncMock(return_value=([], "10"))
    client_any: Any = client
    client_any.sandbox_class = MagicMock(
        side_effect=lambda **kw: MagicMock(
            sandbox_id=kw.get("sandbox_id"),
            claim_name=kw.get("claim_name"),
            connector=MagicMock(),
            close_connection=AsyncMock(),
        )
    )

    async def _noop_async(_s: Any) -> None:
        return None

    with (
        patch.object(AsyncSandboxBatch, "_start", lambda _s: None),
        patch.object(AsyncSandboxBatch, "_create_worker", _noop_async),
    ):
        batch = await client.claim_batch(
            [BatchGroup(warmpool="pool-a", size=0), BatchGroup(warmpool="pool-b", size=0)],
            batch_id="stressbatch",
            max_in_flight=max_in_flight,
            create_rps=5000.0,
            quorum_timeout=10,
        )

    create_counter = 0

    async def _on_create(cname: str, wpool: str, _ns: str, **_kw: Any) -> None:
        nonlocal create_counter
        create_counter += 1
        if create_counter % 11 == 0:
            raise async_k8s_client.ApiException(status=503, reason="Transient")
        await asyncio.sleep(0.002)
        obj = _ready_claim_dict(cname, wpool)
        async with batch._lock:
            m = batch._state.upsert_claim(obj)
            if m is not None and m.state is not MemberState.PENDING:
                batch._wake_acquire(cname)

    async def _on_delete(_cname: str, _ns: str, **_kw: Any) -> None:
        await asyncio.sleep(0.001)

    async def _on_delete_label(_ns: str, _sel: str, **_kw: Any) -> None:
        await asyncio.sleep(0.002)

    helper.create_sandbox_claim = AsyncMock(side_effect=_on_create)
    helper.delete_sandbox_claim = AsyncMock(side_effect=_on_delete)
    helper.delete_sandbox_claims_by_label = AsyncMock(side_effect=_on_delete_label)

    async def _coro_worker(wid: int) -> None:
        pool = "pool-a" if wid % 3 != 0 else ("pool-b" if wid % 2 == 0 else "pool-c")
        if wid in (17, 34):
            await asyncio.sleep(0.01)
            try:
                await batch.release_group("pool-b")
            except BatchError:
                pass
            return
        try:
            member = await batch.acquire(pool, timeout=4.0)
            sbx = await batch.connect(member)
            assert sbx is not None
            if wid % 2 == 0:
                await batch.release_member(member)
        except BatchError:
            pass

    await asyncio.wait_for(
        asyncio.gather(*(_coro_worker(i) for i in range(50))),
        timeout=8.0,
    )

    assert batch._in_flight._value == max_in_flight, (
        f"Async _in_flight permits leaked: {batch._in_flight._value} != {max_in_flight}"
    )
    assert batch._active_deletes == 0
    assert dict(batch._acquire_waiters) == {}
    assert dict(batch._state._checking_acquires) == {}
    BatchInvariantOracle.assert_structural_invariants(
        batch._state,
        in_flight_acquire_names=set(),
    )


class _AtomicLeaseK8sStore:
    """Thread-safe K8s Lease store enforcing strict resourceVersion CAS (409 on conflict)."""

    def __init__(self, initial_lease: k8s_client.V1Lease, claims: list[dict[str, Any]]) -> None:
        self._lock = threading.Lock()
        self._lease: k8s_client.V1Lease | None = copy.deepcopy(initial_lease)
        assert initial_lease.metadata is not None
        self._rv = int(initial_lease.metadata.resource_version or "10")
        self._claims = copy.deepcopy(claims)

    def read_lease(self, _name: str, _ns: str, **_kw: Any) -> k8s_client.V1Lease | None:
        with self._lock:
            return copy.deepcopy(self._lease)

    def replace_lease(
        self, _name: str, _ns: str, body: k8s_client.V1Lease, **_kw: Any
    ) -> k8s_client.V1Lease:
        with self._lock:
            if self._lease is None:
                raise k8s_client.ApiException(status=404, reason="NotFound")
            assert body.metadata is not None and self._lease.metadata is not None
            expected_rv = body.metadata.resource_version
            if expected_rv != self._lease.metadata.resource_version:
                raise k8s_client.ApiException(status=409, reason="Conflict")
            self._rv += 1
            updated = copy.deepcopy(body)
            assert updated.metadata is not None
            updated.metadata.resource_version = str(self._rv)
            self._lease = updated
            return copy.deepcopy(updated)

    def list_claims(self, _ns: str, _sel: str, **_kw: Any) -> tuple[list[dict[str, Any]], str]:
        with self._lock:
            return copy.deepcopy(self._claims), str(self._rv)


def test_concurrent_detach_vs_get_batch_atomic_lease_cas_no_split_brain() -> None:
    """Races detach(grace=30) against 8 concurrent get_batch() threads across 15 CAS
    rounds (INV-OWN-1).
    """
    grp = BatchGroup(warmpool="pool-a", size=2, min_ready=1)
    seeded_claims = [
        _ready_claim_dict("casbatch-0", "pool-a", group_spec=grp),
        _ready_claim_dict("casbatch-1", "pool-a", group_spec=grp),
    ]

    for round_idx in range(15):
        store = _AtomicLeaseK8sStore(
            _make_lease(
                batch_id="casbatch", holder_identity=f"orig-{round_idx}", resource_version="10"
            ),
            seeded_claims,
        )

        def _make_bound_client() -> SandboxClient[Any]:
            with patch("k8s_agent_sandbox.sandbox_client.K8sHelper"):
                c: SandboxClient[Any] = SandboxClient(cleanup=False)
            h: Any = c.k8s_helper
            h.api_pool_size.return_value = 25
            h.count_live_sandbox_claims.return_value = 0
            h.read_batch_lease.side_effect = store.read_lease
            h.replace_batch_lease.side_effect = store.replace_lease
            h.list_sandbox_claim_objects.side_effect = store.list_claims
            h.create_batch_lease.return_value = store.read_lease("batch-casbatch", "default")
            h.get_sandbox_warmpool.return_value = {
                "spec": {"sandboxTemplateRef": {"name": "tmpl"}},
                "status": {"readyReplicas": 2},
            }
            h.get_sandbox_template.return_value = {"metadata": {"name": "tmpl"}}
            return c

        orig_client = _make_bound_client()
        orig_helper: Any = orig_client.k8s_helper
        orig_helper.list_sandbox_claim_objects.side_effect = None
        orig_helper.list_sandbox_claim_objects.return_value = ([], "10")
        with (
            patch.object(SandboxBatch, "_start", lambda _s: None),
            patch.object(SandboxBatch, "_create_worker", lambda _s: None),
        ):
            orig_batch = orig_client.claim_batch([grp], batch_id="casbatch", lease_duration=60)
        orig_helper.list_sandbox_claim_objects.side_effect = store.list_claims
        orig_batch._holder_identity = f"orig-{round_idx}"
        current_lease = store.read_lease("batch-casbatch", "default")
        assert current_lease is not None
        orig_batch._lease = current_lease
        # Simulate dynamic ordinal bump before detach so INV-OWN-3 next_ordinal handoff is checked.
        orig_batch._state.start_acquire("pool-a")
        orig_batch._state.drop_acquired("casbatch-2", created=False)

        barrier = threading.Barrier(9)
        winners: list[SandboxBatch] = []
        winners_lock = threading.Lock()

        def _detacher() -> None:
            barrier.wait(timeout=5.0)
            orig_batch.detach(grace=30)

        def _attacher() -> None:
            c = _make_bound_client()
            barrier.wait(timeout=5.0)
            # Stagger slightly across race window before and right after detach CAS write.
            for _ in range(3):
                try:
                    with patch.object(SandboxBatch, "_start", lambda _s: None):
                        won = c.get_batch("casbatch")
                    with winners_lock:
                        winners.append(won)
                    return
                except BatchInUseError:
                    time.sleep(0.001)

        with ThreadPoolExecutor(max_workers=9) as ex:
            futs = [ex.submit(_detacher), *(ex.submit(_attacher) for _ in range(8))]
            done, not_done = wait(futs, timeout=8.0)
            assert not not_done, "Deadlock during concurrent detach vs get_batch!"
            for f in done:
                f.result()

        assert orig_batch._lease_released is True
        assert len(winners) <= 1, (
            f"INV-OWN-1 SPLIT-BRAIN: {len(winners)} handles simultaneously won get_batch()!"
        )
        final_lease = store.read_lease("batch-casbatch", "default")
        assert final_lease is not None and final_lease.metadata is not None
        assert final_lease.metadata.annotations is not None
        assert final_lease.metadata.annotations.get(BATCH_NEXT_ORDINAL_ANNOTATION) == "3", (
            "INV-OWN-3 violated: detach() failed to persist next_ordinal=3 on Lease annotations"
        )
        if len(winners) == 1:
            assert final_lease.spec is not None
            assert winners[0]._holder_identity == final_lease.spec.holder_identity
            assert winners[0]._state.next_ordinal >= 3


def test_rapid_creator_flood_vs_release_not_ready_and_release_drain() -> None:
    """Runs real _create_worker threads against concurrent release_not_ready() and release()."""
    with patch("k8s_agent_sandbox.sandbox_client.K8sHelper"):
        client: SandboxClient[Any] = SandboxClient(cleanup=False)
    helper: Any = client.k8s_helper
    helper.api_pool_size.return_value = 16
    helper.count_live_sandbox_claims.return_value = 0
    helper.get_sandbox_warmpool.return_value = {
        "spec": {"sandboxTemplateRef": {"name": "tmpl"}},
        "status": {"readyReplicas": 20},
    }
    helper.get_sandbox_template.return_value = {"metadata": {"name": "tmpl"}}
    initial_lease = _make_lease(batch_id="floodbatch", holder_identity="h-flood")
    helper.create_batch_lease.return_value = initial_lease
    helper.replace_batch_lease.side_effect = lambda _n, _ns, body, **_kw: _make_lease(
        batch_id="floodbatch",
        holder_identity=body.spec.holder_identity,
        resource_version="55",
        lease_duration_seconds=body.spec.lease_duration_seconds or 60,
    )
    helper.list_sandbox_claim_objects.return_value = ([], "10")

    def _slow_create(_cname: str, _wpool: str, _ns: str, **_kw: Any) -> None:
        time.sleep(0.005)

    def _slow_delete(_cname: str, _ns: str, **_kw: Any) -> None:
        time.sleep(0.003)

    helper.create_sandbox_claim.side_effect = _slow_create
    helper.delete_sandbox_claim.side_effect = _slow_delete

    with patch.object(SandboxBatch, "_start", lambda _s: None):
        batch = client.claim_batch(
            [BatchGroup(warmpool="pool-a", size=18, min_ready=9)],
            batch_id="floodbatch",
            max_in_flight=6,
            create_rps=2000.0,
        )
    helper.read_batch_lease.side_effect = lambda *_a, **_kw: copy.deepcopy(batch._lease)
    batch._holder_identity = "h-flood"
    assert batch._lease.spec is not None
    batch._lease.spec.holder_identity = "h-flood"

    def _run_release_not_ready() -> None:
        time.sleep(0.01)
        batch.release_not_ready()

    def _run_release() -> None:
        time.sleep(0.02)
        batch.release()

    with ThreadPoolExecutor(max_workers=4) as ex:
        f1 = ex.submit(_run_release_not_ready)
        f2 = ex.submit(_run_release)
        done, not_done = wait([f1, f2], timeout=8.0)
        assert not not_done, "Deadlock between _create_worker, release_not_ready(), and release()!"
        for f in done:
            try:
                f.result()
            except BatchError:
                pass

    for creator in batch._creators:
        assert not creator.is_alive(), "Creator thread leaked after release() finished!"
    for i in range(6):
        assert batch._in_flight.acquire(timeout=0.1), f"Permit {i} leaked after creator flood!"
    for _ in range(6):
        batch._in_flight.release()
    assert dict(batch._active_deletes) == {}
    BatchInvariantOracle.assert_structural_invariants(batch._state, in_flight_acquire_names=set())


@pytest.mark.asyncio
async def test_async_client_close_holds_lock_across_io_starving_batch_ops() -> None:
    """Reproduces F-15 (INV-LOCK-ASYNC-1): AsyncSandboxClient.close() holds self._lock
    across await.
    """
    with patch("k8s_agent_sandbox.async_sandbox_client.AsyncK8sHelper"):
        client: AsyncSandboxClient[Any] = AsyncSandboxClient(
            connection_config=SandboxDirectConnectionConfig(api_url="http://127.0.0.1:8080"),
            cleanup=False,
        )
    helper: Any = client.k8s_helper
    helper.close = AsyncMock()

    entered_close_io = asyncio.Event()

    async def _slow_close_connection() -> None:
        entered_close_io.set()
        await asyncio.sleep(0.25)

    slow_sbx = MagicMock()
    slow_sbx.close_connection = AsyncMock(side_effect=_slow_close_connection)
    client._active_connection_sandboxes[("default", "sbx-slow")] = slow_sbx

    close_task = asyncio.create_task(client.close())
    await asyncio.wait_for(entered_close_io.wait(), timeout=1.0)

    # Measure how long acquiring client._lock (used by batch registration / list_active_sandboxes)
    # stalls while close() awaits slow network/subprocess teardown.
    t0 = time.monotonic()
    async with client._lock:
        pass
    elapsed = time.monotonic() - t0
    await close_task

    assert elapsed < 0.05, (
        f"INV-LOCK-ASYNC-1 violated: AsyncSandboxClient.close() held self._lock across "
        f"`await sandbox.close_connection()`, blocking concurrent client._lock acquisition for "
        f"{elapsed:.3f}s (expected < 0.050s)"
    )
