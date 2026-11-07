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

"""Fault-Injection and Chaos Suite for agent-sandbox-rl Batch Claiming.

Covers the downstream RL cells of comprehensive_verification_strategy.md and
provides deterministic failing reproductions for all RL batch-claiming bugs:
  - F-05 (INV-LEAK-2): AsyncSandboxFleet.acquire cancellation orphans worker-thread handle & slot
  - F-06 (INV-LEAK-2): SandboxFleet.acquire / _acquire_reserved KeyboardInterrupt slot & claim leak
  - F-07 (INV-LEAK-1): BatchClaims._batch_for teardown race & release_all upfront _opened clear
  - F-10 (INV-CONN-1, INV-LEAK-1): Batch-mode SandboxHandle.refresh() closed sandbox, leaked
         PodSnapshotManualTrigger CRs on release(), and stopped-batch close_connection() leak
  - F-11 (INV-LEAK-1, INV-LEAK-2): SandboxFleet._teardown() handle-0 failure stranding handles/pools
         and post-teardown SandboxFleet.acquire() creating remote on-demand pools
  - F-14 (INV-OWN-TIMEOUT-3): BatchClaim.release() & fleet rollback unbounded delete_claim()
  - F-19: Sub-second shutdownTime strftime truncation vs BatchClaim.expires_at + FakeHandle parity
  - F-20 (INV-LEAK-1): Custom FleetConfig(labels=...) omitting app=agent-sandbox-rl on BatchClaims
         and verify_teardown() / reap(run_id=...) ignoring batch Leases
  - F-21: Batch connect() SandboxNotReadyError unwrap, acquire_batch partial-failure handle leak,
         and preflight() missing batch Lease RBAC verification
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from k8s_agent_sandbox import BatchError, Member, MemberState
from k8s_agent_sandbox.exceptions import SandboxNotReadyError
from k8s_agent_sandbox.utils import construct_sandbox_claim_lifecycle_spec
from kubernetes import client as k8s_client

from agent_sandbox_rl import (
    AsyncSandboxFleet,
    BatchClaimConfig,
    ClusterRegistry,
    FleetConfig,
    SandboxFleet,
    constants,
)
from agent_sandbox_rl.batch_claims import BatchClaim, BatchClaims
from agent_sandbox_rl.exceptions import FleetError, FleetOvercommitError
from agent_sandbox_rl.handles import SandboxHandle
from agent_sandbox_rl.recycle import (
    reuse_git_restore_sandbox,
    reuse_git_restore_sandbox_async,
)
from agent_sandbox_rl.sources import Task
from agent_sandbox_rl.testing import FakeFleet


def _setup_batch_cluster(make_cluster: Any, name: str = "c1") -> Any:
    c = make_cluster(name)
    c.exec_core_api = lambda: c.core_api
    seq = 0

    def _claim_batch(_groups: Any, **_kwargs: Any) -> MagicMock:
        nonlocal seq
        seq += 1
        b = MagicMock()
        b.batch_id = f"b-{name}-{seq}"
        b.err.return_value = None
        m_seq = 0

        def _acquire(warmpool: str, timeout: float | None = None, **_akw: Any) -> Member:
            nonlocal m_seq
            m_seq += 1
            return Member(
                claim_name=f"{b.batch_id}-{m_seq}",
                sandbox_name=f"sb-{b.batch_id}-{m_seq}",
                warmpool=warmpool,
                pod_ips=("10.1.0.5",),
                state=MemberState.READY,
            )

        def _connect(member: Member) -> MagicMock:
            sb = MagicMock()
            sb.claim_name = member.claim_name
            sb.sandbox_id = member.sandbox_name
            sb.is_active = True
            sb.get_pod_name.return_value = f"pod-{member.claim_name}"
            sb.get_pod_ip.return_value = "10.1.0.5"
            return sb

        b.acquire.side_effect = _acquire
        b.connect.side_effect = _connect
        return b

    c.sandbox_client.claim_batch.side_effect = _claim_batch
    return c


# ===========================================================================
# F-05 & F-06: Async Cancellation & Sync SIGINT Reservation / Claim Rollback
# ===========================================================================


@pytest.mark.asyncio
async def test_rl_async_fleet_cancelled_acquire_reclaims_real_handle_and_slot(
    make_cluster: Any,
) -> None:
    """Reproduces F-05 (INV-LEAK-2): Cancelling `await async_fleet.acquire(task)` abandons
    `loop.run_in_executor(...)` while the worker thread finishes acquiring a real
    `SandboxHandle` and `BatchClaim`, permanently leaking `_claims_reserved=1` and the
    remote `BatchClaim`.
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    cfg = FleetConfig(claim_mode="batch", max_live_sandboxes=1)
    sync_fleet = SandboxFleet(cfg, registry=ClusterRegistry([c]))
    tasks = sync_fleet.load_tasks(["img:1", "img:1"])
    sync_fleet.plan()
    fleet = AsyncSandboxFleet(cfg, sync_fleet=sync_fleet)

    worker_entered = threading.Event()
    release_worker = threading.Event()
    worker_done = threading.Event()
    real_acquire_reserved = sync_fleet._acquire_reserved

    def gated_acquire_reserved(*args: Any, **kwargs: Any) -> SandboxHandle:
        worker_entered.set()
        release_worker.wait(timeout=2.0)
        try:
            return real_acquire_reserved(*args, **kwargs)
        finally:
            worker_done.set()

    h2: SandboxHandle | None = None
    try:
        with patch.object(sync_fleet, "_acquire_reserved", side_effect=gated_acquire_reserved):
            acq_task = asyncio.create_task(fleet.acquire(tasks[0]))
            await asyncio.to_thread(worker_entered.wait, 2.0)
            assert sync_fleet._claims_reserved == 1

            acq_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await acq_task

            release_worker.set()
            await asyncio.to_thread(worker_done.wait, 2.0)
            # Allow any done-callback scheduled on the worker future to execute.
            await asyncio.sleep(0.02)

        assert sync_fleet._claims_reserved == 0, (
            "F-05 (INV-LEAK-2) violated: cancelling AsyncSandboxFleet.acquire() orphaned the "
            "worker thread's SandboxHandle and leaked _claims_reserved=1!"
        )
        assert len(fleet.handles()) == 0
        try:
            h2 = await fleet.acquire(tasks[1])
        except FleetOvercommitError as exc:
            pytest.fail(f"Subsequent acquire() wedged by leaked _claims_reserved slot: {exc}")
    finally:
        release_worker.set()
        if h2 is not None:
            await fleet.release(h2)
        fleet.close(wait=True)


def test_rl_sync_fleet_keyboard_interrupt_rolls_back_reserved_slot(make_cluster: Any) -> None:
    """Reproduces F-06 (INV-LEAK-2): `SandboxFleet._on_signal` raises `KeyboardInterrupt`,
    yet `SandboxFleet.acquire()` (`fleet.py:1473`) and `_acquire_reserved()` (`fleet.py:1567`)
    catch `except Exception:`, leaking both `_claims_reserved` and the acquired `BatchClaim`.
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    opened_batches: list[MagicMock] = []
    base_claim_batch = c.sandbox_client.claim_batch.side_effect

    def _interrupt_on_connect(groups: Any, **kwargs: Any) -> MagicMock:
        b = base_claim_batch(groups, **kwargs)
        b.connect.side_effect = KeyboardInterrupt("SIGTERM")
        opened_batches.append(b)
        return b

    c.sandbox_client.claim_batch.side_effect = _interrupt_on_connect
    cfg = FleetConfig(claim_mode="batch", max_live_sandboxes=1)
    sync_fleet = SandboxFleet(cfg, registry=ClusterRegistry([c]))
    tasks = sync_fleet.load_tasks(["img:1"])
    sync_fleet.plan()

    with pytest.raises(KeyboardInterrupt):
        sync_fleet.acquire(tasks[0])

    assert sync_fleet._claims_reserved == 0, (
        "F-06 (INV-LEAK-2) violated: SandboxFleet.acquire() caught Exception instead of "
        "BaseException, leaking _claims_reserved=1 on KeyboardInterrupt!"
    )
    assert len(opened_batches) == 1 and opened_batches[0].release_member.call_count == 1, (
        "F-06 (INV-LEAK-2) violated: SandboxFleet._acquire_reserved() caught Exception "
        "instead of BaseException, skipping claim.release() on KeyboardInterrupt!"
    )


def test_rl_sync_fleet_release_keyboard_interrupt_restores_handle_to_fleet(
    make_cluster: Any,
) -> None:
    """Reproduces F-06c (INV-LEAK-2): `SandboxFleet.release()` (`fleet.py:1719, 1737`) removes
    `handle` from `self._handles` upfront and only restores it under `except Exception:`. When
    `KeyboardInterrupt` arrives during `handle.release()`, `handle` is permanently lost from
    `fleet.handles()` while `_claims_reserved=1` and the remote SandboxClaim remain live!
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    cfg = FleetConfig(claim_mode="batch", max_live_sandboxes=1)
    sync_fleet = SandboxFleet(cfg, registry=ClusterRegistry([c]))
    tasks = sync_fleet.load_tasks(["img:1"])
    sync_fleet.plan()
    handle = sync_fleet.acquire(tasks[0])

    handle.release = MagicMock(  # type: ignore[method-assign]
        side_effect=KeyboardInterrupt("SIGINT")
    )
    with pytest.raises(KeyboardInterrupt):
        sync_fleet.release(handle)

    assert handle in sync_fleet.handles(), (
        "F-06c (INV-LEAK-2) violated: SandboxFleet.release() dropped handle from _handles on "
        "KeyboardInterrupt while _claims_reserved=1 and remote claim remained live!"
    )


# ===========================================================================
# F-07 & F-14: BatchClaims Teardown Race, Failed-Release Retention, & Timeout
# ===========================================================================


def test_rl_batch_claims_teardown_race_tracks_batch_and_raises_fleet_error() -> None:
    """Reproduces F-07a (INV-LEAK-1): When `release_all()` runs while `_batch_for` is
    inside `claim_batch([])` and inline `batch.release()` hits a transient `503`,
    `_batch_for` drops `raced_batch` outside `_opened` and leaks raw `ApiException`.
    """
    bc = BatchClaims(BatchClaimConfig(), labels={"run": "r1"}, ready_timeout=30)
    cluster: Any = MagicMock()
    cluster.name = "c1"
    cluster.namespace = "default"

    raced_batch = MagicMock()
    raced_batch.batch_id = "b-raced"
    raced_batch.release.side_effect = [
        k8s_client.ApiException(status=503, reason="Service Unavailable"),
        None,
    ]

    def claim_batch_while_teardown_races(*_args: Any, **_kwargs: Any) -> MagicMock:
        bc.release_all()
        return raced_batch

    cluster.sandbox_client.claim_batch.side_effect = claim_batch_while_teardown_races

    with pytest.raises(FleetError):
        bc.acquire(cluster, "pool-a")

    bc.release_all()
    assert raced_batch.release.call_count == 2, (
        "F-07a (INV-LEAK-1) violated: raced_batch was dropped outside _opened so retry "
        "release_all() never released it!"
    )


def test_rl_batch_claims_release_all_retains_failed_batches_for_retry() -> None:
    """Reproduces F-07b (INV-LEAK-1): `BatchClaims.release_all()` clears `self._opened = []`
    upfront before calling `batch.release()`; if `batch.release()` fails with 503, a second
    `release_all()` call sees `_opened == []` and leaks the batch and its Lease!
    """
    bc = BatchClaims(BatchClaimConfig(), labels={"run": "r1"}, ready_timeout=30)
    cluster: Any = MagicMock()
    cluster.name = "c1"
    cluster.namespace = "default"

    flaky_batch = MagicMock()
    flaky_batch.batch_id = "b-flaky"
    flaky_batch.err.return_value = None
    flaky_batch.acquire.return_value = Member(
        claim_name="b-flaky-0",
        sandbox_name="sb-0",
        warmpool="pool-a",
        pod_ips=("10.0.0.2",),
        state=MemberState.READY,
    )
    flaky_batch.release.side_effect = [
        k8s_client.ApiException(status=503, reason="Service Unavailable"),
        None,
    ]
    cluster.sandbox_client.claim_batch.return_value = flaky_batch

    _ = bc.acquire(cluster, "pool-a")
    bc.release_all()
    bc.release_all()
    assert flaky_batch.release.call_count == 2, (
        "F-07b (INV-LEAK-1) violated: BatchClaims.release_all() cleared _opened upfront so "
        "the retry skipped flaky_batch!"
    )


def test_rl_batch_claim_release_fallback_passes_bounded_request_timeout() -> None:
    """Reproduces F-14 (INV-OWN-TIMEOUT-3): Stopped-batch fallback `BatchClaim.release()`
    must pass a bounded `request_timeout` to `cluster.resources.delete_claim()`.
    """
    batch = MagicMock()
    batch.release_member.side_effect = BatchError("batch stopped")
    member = MagicMock(claim_name="b1-0")
    cluster = MagicMock()

    claim = BatchClaim(batch=batch, member=member, expires_at=100.0, cluster=cluster)
    claim.release()

    cluster.resources.delete_claim.assert_called_once()
    kwargs = cluster.resources.delete_claim.call_args.kwargs
    assert kwargs.get("request_timeout") is not None, (
        "F-14 (INV-OWN-TIMEOUT-3) violated: BatchClaim.release() fallback called "
        "cluster.resources.delete_claim('b1-0') without a bounded request_timeout!"
    )


# ===========================================================================
# F-10: Batch-Mode SandboxHandle.refresh(), Snapshot Trigger Leak, & Close Leak
# ===========================================================================


def test_rl_batch_handle_refresh_reconnects_closed_sandbox_after_watch_eviction(
    make_cluster: Any,
) -> None:
    """Reproduces F-10a (INV-CONN-1): During `suspend()` + `resume()` in `claim_mode='batch'`,
    `SandboxBatch._watch_loop` observes `Ready=False` / pod replacement and closes the old
    cached `Sandbox` (`is_active=False`). `SandboxHandle.refresh()` reads `pod_name` via K8s
    API yet never reconnects `self.sandbox` via `batch.connect(latest_member)`, leaving
    `handle.sandbox.is_active == False`!
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    old_sb = MagicMock(claim_name="b1-0", sandbox_id="sb-0", is_active=False)
    old_sb.get_pod_name.return_value = "pod-resumed-1"
    old_sb.get_pod_ip.return_value = "10.1.0.9"

    new_sb = MagicMock(claim_name="b1-0", sandbox_id="sb-0", is_active=True)
    new_sb.get_pod_name.return_value = "pod-resumed-1"
    new_sb.get_pod_ip.return_value = "10.1.0.9"

    updated_member = Member(
        claim_name="b1-0",
        sandbox_name="sb-0",
        warmpool="pool-a",
        pod_ips=("10.1.0.9",),
        state=MemberState.READY,
    )
    batch = MagicMock()
    batch.members.return_value = [updated_member]
    batch.connect.return_value = new_sb

    bclaim = BatchClaim(batch=batch, member=updated_member, expires_at=999.0, cluster=c)
    handle = SandboxHandle(
        task=Task(id="t1", image="img:1"),
        cluster_name="c1",
        claim_name="b1-0",
        sandbox_id="sb-0",
        pod_name="pod-old-0",
        hostname="sb-0",
        sandbox=old_sb,
        _cluster=c,
        _batch_claim=bclaim,
    )

    handle.refresh()
    assert getattr(handle.sandbox, "is_active", False) is True, (
        "F-10a (INV-CONN-1) violated: SandboxHandle.refresh() left handle.sandbox pointing "
        "at the watch-closed Sandbox handle (is_active=False) instead of reconnecting!"
    )


def test_rl_batch_handle_release_cleans_snapshot_triggers(make_cluster: Any) -> None:
    """Reproduces F-10b (INV-LEAK-1): In `claim_mode='batch'`, `SandboxHandle.release()`
    returns early via `self._batch_claim.release()` without calling
    `sandbox.snapshots.delete_manual_triggers()`, leaking `PodSnapshotManualTrigger` CRs.
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    snap_engine = MagicMock()
    sb = MagicMock(claim_name="b1-0", sandbox_id="sb-0", snapshots=snap_engine)

    batch = MagicMock()
    member = Member(
        claim_name="b1-0",
        sandbox_name="sb-0",
        warmpool="pool-a",
        pod_ips=("10.1.0.5",),
        state=MemberState.READY,
    )
    bclaim = BatchClaim(batch=batch, member=member, expires_at=999.0, cluster=c)
    handle = SandboxHandle(
        task=Task(id="t1", image="img:1"),
        cluster_name="c1",
        claim_name="b1-0",
        sandbox_id="sb-0",
        pod_name="pod-0",
        hostname="sb-0",
        sandbox=sb,
        _cluster=c,
        _batch_claim=bclaim,
    )

    handle.release()
    assert snap_engine.delete_manual_triggers.call_count == 1, (
        "F-10b (INV-LEAK-1) violated: batch-mode SandboxHandle.release() skipped "
        "sandbox.snapshots.delete_manual_triggers(), leaking PodSnapshotManualTrigger CRs!"
    )


def test_rl_stopped_batch_fallback_release_closes_sandbox_connection(
    make_cluster: Any,
) -> None:
    """Reproduces F-10c (INV-CONN-1): When `batch.release_member` raises `BatchError`
    (stopped batch), fallback release deletes the claim via `cluster.resources.delete_claim`
    without closing `handle.sandbox` client connection!
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    sb = MagicMock(claim_name="b1-0", sandbox_id="sb-0")

    batch = MagicMock()
    batch.release_member.side_effect = BatchError("batch stopped")
    member = Member(
        claim_name="b1-0",
        sandbox_name="sb-0",
        warmpool="pool-a",
        pod_ips=("10.1.0.5",),
        state=MemberState.READY,
    )
    bclaim = BatchClaim(batch=batch, member=member, expires_at=999.0, cluster=c)
    handle = SandboxHandle(
        task=Task(id="t1", image="img:1"),
        cluster_name="c1",
        claim_name="b1-0",
        sandbox_id="sb-0",
        pod_name="pod-0",
        hostname="sb-0",
        sandbox=sb,
        _cluster=c,
        _batch_claim=bclaim,
    )

    handle.release()
    assert sb.close_connection.call_count >= 1, (
        "F-10c (INV-CONN-1) violated: stopped-batch fallback release never closed "
        "handle.sandbox client connection!"
    )


# ===========================================================================
# F-11: Fleet._teardown Wedge on Handle-0 Error & Post-Teardown acquire()
# ===========================================================================


def test_rl_fleet_teardown_sweeps_remaining_handles_and_pools_when_handle_0_raises(
    make_cluster: Any,
) -> None:
    """Reproduces F-11a (INV-LEAK-1): When handle 0's `release()` raises inside
    `fleet.teardown()`, `teardown()` must still re-raise that error (preserving the
    `test_batch_claims.py:238` contract) AFTER releasing handles `1..N-1` and sweeping
    run-scoped pools/templates, instead of setting `_torndown=True` upfront and skipping
    lines `2001-2093` forever!
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    c.resources.list_warmpools.return_value = ["pool-run-1"]
    fleet = SandboxFleet(FleetConfig(claim_mode="batch"), registry=ClusterRegistry([c]))
    tasks = fleet.load_tasks(["img:1", "img:1"])
    fleet.plan()
    h0 = fleet.acquire(tasks[0])
    h1 = fleet.acquire(tasks[1])

    h0.release = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("transient apiserver 500 on h0")
    )
    h1_released = False
    orig_h1_release = h1.release

    def track_h1_release() -> None:
        nonlocal h1_released
        h1_released = True
        orig_h1_release()

    h1.release = track_h1_release  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="transient apiserver 500 on h0"):
        fleet.teardown()

    assert h1_released, (
        "F-11a (INV-LEAK-1) violated: h0.release() raising aborted release_all() before h1 "
        "was released!"
    )
    assert c.resources.delete_warmpool.call_count >= 1, (
        "F-11a (INV-LEAK-1) violated: h0.release() raising skipped run-scoped warmpool/template "
        "teardown sweep while setting _torndown=True permanently!"
    )


def test_rl_post_teardown_acquire_fails_fast_without_creating_remote_pools(
    make_cluster: Any,
) -> None:
    """Reproduces F-11b (INV-LEAK-1): Unlike `FakeFleet.acquire()`, `SandboxFleet.acquire()`
    lacks an entry `if self._torndown:` check and calls `ensure_template` + `create_warmpool`
    on the cluster before discovering `BatchClaims` is closed or `self._torndown`!
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    fleet = SandboxFleet(FleetConfig(claim_mode="batch"), registry=ClusterRegistry([c]))
    fleet.teardown()
    c.resources.ensure_template.reset_mock()
    c.resources.create_warmpool.reset_mock()

    with pytest.raises(FleetError):
        fleet.acquire(Task(id="late-task", image="unplanned-img:1"))

    c.resources.ensure_template.assert_not_called()
    c.resources.create_warmpool.assert_not_called()


# ===========================================================================
# F-19: Sub-Second shutdownTime Truncation & FakeHandle.claim_expires_at Parity
# ===========================================================================


def test_rl_batch_claim_expires_at_never_exceeds_truncated_k8s_shutdown_time() -> None:
    """Reproduces F-19: `construct_sandbox_claim_lifecycle_spec` formats `shutdownTime` with
    `.strftime('%Y-%m-%dT%H:%M:%SZ')` (truncating microseconds toward zero, up to 0.999999s
    earlier), whereas `BatchClaims.acquire` sets `expires_at = t0 + ttl_s` without subtracting
    1s for sub-second truncation. Also checks `FakeHandle.claim_expires_at` parity.
    """
    fixed_wall = dt.datetime(2026, 11, 7, 3, 0, 0, 950_000, tzinfo=dt.timezone.utc)
    with patch("k8s_agent_sandbox.utils.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_wall
        spec = construct_sandbox_claim_lifecycle_spec(100)
    shutdown_wall = dt.datetime.fromisoformat(spec["shutdownTime"].replace("Z", "+00:00"))
    actual_k8s_ttl_s = (shutdown_wall - fixed_wall).total_seconds()
    # actual_k8s_ttl_s is 99.05s because .950000s was truncated!

    bc = BatchClaims(
        BatchClaimConfig(work_budget=40),
        labels={"run": "r1"},
        ready_timeout=0,
    )
    # ttl_s = quorum_timeout(0) + work_budget(40) + BATCH_SHUTDOWN_MARGIN_SECONDS(600) = 640
    cluster: Any = MagicMock(name="c1", namespace="default")
    fake_batch = MagicMock()
    fake_batch.err.return_value = None
    fake_batch.acquire.return_value = Member(
        claim_name="b1-0",
        sandbox_name="sb-0",
        warmpool="pool-a",
        pod_ips=("10.0.0.2",),
        state=MemberState.READY,
    )
    cluster.sandbox_client.claim_batch.return_value = fake_batch

    t0 = time.monotonic()
    claim = bc.acquire(cluster, "pool-a")
    claimed_ttl = claim.expires_at - t0

    assert actual_k8s_ttl_s < 100.0
    assert claimed_ttl <= 639.05, (
        f"F-19 violated: BatchClaim.expires_at assumed full {claimed_ttl:.2f}s TTL without "
        "accounting for .strftime('%Y-%m-%dT%H:%M:%SZ') sub-second truncation!"
    )


def test_rl_fake_handle_exposes_claim_expires_at_for_recycle_parity() -> None:
    """Reproduces F-19b: `FakeFleet(FleetConfig(claim_mode='batch')).acquire(...)` leaves
    `FakeHandle.claim_expires_at` as `None` instead of setting a finite batch shutdown deadline,
    so `reuse_git_restore_sandbox` never exercises `_claim_expiring` against `FakeFleet`!
    """
    fake_fleet = FakeFleet(FleetConfig(claim_mode="batch"))
    fh = fake_fleet.acquire(Task(id="t1", image="img:1"))
    assert fh.claim_expires_at is not None, (
        "F-19b violated: FakeFleet(claim_mode='batch').acquire() returned FakeHandle with "
        "claim_expires_at=None, skipping batch claim-expiry recycling checks in FakeFleet tests!"
    )


# ===========================================================================
# F-20: Custom FleetConfig(labels=...) Managed Label & Orphaned Batch Leases
# ===========================================================================


def test_rl_custom_fleet_labels_preserve_managed_app_label_on_batch_claims(
    make_cluster: Any,
) -> None:
    """Reproduces F-20a (INV-LEAK-1): When a caller passes custom
    `FleetConfig(labels={'team': 'rl'})`, `ResourceManager` merges `DEFAULT_LABELS`
    (`app=agent-sandbox-rl`), whereas `SandboxFleet.__init__` passes
    `{**config.labels, RUN_ID_LABEL: run_id}` to `BatchClaims`, omitting
    `app=agent-sandbox-rl` so `reap_orphans` / `reap(all_managed=True)` never sees
    orphaned batch claims!
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    fleet = SandboxFleet(
        FleetConfig(claim_mode="batch", labels={"team": "rl"}),
        registry=ClusterRegistry([c]),
    )
    tasks = fleet.load_tasks(["img:1"])
    fleet.plan()
    _ = fleet.acquire(tasks[0])

    batch_labels = c.sandbox_client.claim_batch.call_args.kwargs.get("labels", {})
    for k, v in constants.DEFAULT_LABELS.items():
        assert batch_labels.get(k) == v, (
            f"F-20a (INV-LEAK-1) violated: BatchClaims omitted managed label {k}={v!r} when "
            f"custom FleetConfig.labels was supplied (got labels={batch_labels})!"
        )


def test_rl_run_leftovers_and_verify_teardown_account_for_batch_leases(
    make_cluster: Any,
) -> None:
    """Reproduces F-20b (INV-LEAK-1): `SandboxFleet._run_leftovers` checks claims, warmpools,
    templates, and pods, yet completely ignores `coordination.k8s.io/v1` batch `Lease` objects
    carrying `RUN_ID_LABEL`, reporting a run as clean even when its batch Lease is still live!
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    c.resources.count_pods.return_value = 0
    fleet = SandboxFleet(FleetConfig(claim_mode="batch"), registry=ClusterRegistry([c]))

    leftovers = fleet._run_leftovers(c)
    assert "leases" in leftovers, (
        f"F-20b (INV-LEAK-1) violated: SandboxFleet._run_leftovers() returned keys "
        f"{sorted(leftovers.keys())} without checking run-labeled batch Leases!"
    )


# ===========================================================================
# F-21: Batch connect() SandboxNotReadyError & acquire_batch Partial Rollback
# ===========================================================================


def test_rl_acquire_batch_rolls_back_partial_handles_on_error(make_cluster: Any) -> None:
    """Reproduces F-21c (INV-LEAK-1): When `fleet.acquire_batch([t0, t1])` fails on `t1`,
    already-acquired `h0` is leaked instead of rolled back!
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    fleet = SandboxFleet(FleetConfig(claim_mode="batch"), registry=ClusterRegistry([c]))
    tasks = fleet.load_tasks(["img:1", "img:1"])
    fleet.plan()

    call_num = 0
    real_acquire = fleet.acquire

    def fail_on_second(task: Task, **kw: Any) -> SandboxHandle:
        nonlocal call_num
        call_num += 1
        if call_num == 2:
            raise FleetError("second task pool exhausted")
        return real_acquire(task, **kw)

    with (
        patch.object(fleet, "acquire", side_effect=fail_on_second),
        pytest.raises(FleetError, match="second task pool exhausted"),
    ):
        fleet.acquire_batch(tasks)

    assert len(fleet.handles()) == 0, (
        "F-21c (INV-LEAK-1) violated: SandboxFleet.acquire_batch() leaked h0 when acquiring "
        f"task 1 raised FleetError (fleet.handles()={fleet.handles()})!"
    )


def test_rl_batch_connect_not_ready_raises_batch_error(make_cluster: Any) -> None:
    """Reproduces F-21a: If watch flips a member not-Ready between `batch.acquire()` and
    `batch.connect()`, `connect()` raises `SandboxNotReadyError` (which does NOT inherit from
    `BatchError` or `SandboxException`), leaking unwrapped out of `fleet.acquire()` instead of
    satisfying the batch-claim `BatchError` failure contract (`fleet.py:1447-1450`,
    `test_batch_claims.py:282, 357`).
    """
    c = _setup_batch_cluster(make_cluster, "c1")

    def claim_batch_with_flapping_connect(_groups: Any, **_kw: Any) -> MagicMock:
        b = MagicMock(batch_id="b-flap")
        b.err.return_value = None
        b.acquire.return_value = Member(
            claim_name="b-flap-1",
            sandbox_name="sb-flap-1",
            warmpool="pool-a",
            pod_ips=("10.1.0.5",),
            state=MemberState.READY,
        )
        b.connect.side_effect = SandboxNotReadyError("member became not Ready")
        return b

    c.sandbox_client.claim_batch.side_effect = claim_batch_with_flapping_connect
    fleet = SandboxFleet(FleetConfig(claim_mode="batch"), registry=ClusterRegistry([c]))
    t_single = fleet.load_tasks(["img:1"])
    fleet.plan()
    with pytest.raises(BatchError):
        fleet.acquire(t_single[0])


def test_rl_preflight_verifies_leases_rbac_when_claim_mode_is_batch(
    make_cluster: Any,
) -> None:
    """Reproduces F-21b: When `FleetConfig(claim_mode='batch')` is configured, `fleet.preflight()`
    checks CRDs, controller deployment, namespace, and manifest dry-runs, yet never checks
    `coordination.k8s.io` `leases` access in the namespace, allowing `setup()` to warm all pools
    before the very first `acquire()` fails with `403 Forbidden` on `create_batch_lease`!
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    crd_version = MagicMock()
    crd_version.name = "v1beta1"
    crd_version.served = True
    fake_crd = MagicMock()
    fake_crd.spec.versions = [crd_version]

    with (
        patch("agent_sandbox_rl.preflight._version_api") as mock_ver,
        patch("agent_sandbox_rl.preflight._crd_api") as mock_crd,
    ):
        mock_ver.return_value.get_code.return_value = MagicMock(git_version="v1.32.0")
        mock_crd.return_value.read_custom_resource_definition.return_value = fake_crd
        c.apps_api.read_namespaced_deployment.return_value = MagicMock(
            status=MagicMock(ready_replicas=1)
        )

        fleet = SandboxFleet(FleetConfig(claim_mode="batch"), registry=ClusterRegistry([c]))
        reports = fleet.preflight()

    check_names = [ch.name for ch in reports["c1"].checks]
    assert any("lease" in name for name in check_names), (
        f"F-21b violated: fleet.preflight() in claim_mode='batch' ran checks {check_names} "
        "without verifying coordination.k8s.io Lease access!"
    )


# ===========================================================================
# F-28: Recycle Re-Claim Prime Failure Replica Ref-Count Underflow & JIT Leak
# ===========================================================================


@pytest.mark.asyncio
async def test_rl_async_recycle_reclaim_prime_failure_does_not_underflow_held_replicas(
    make_cluster: Any,
) -> None:
    """Reproduces F-28a (INV-LEAK-1): In `reuse_git_restore_sandbox_async` (`recycle.py:500-519,
    572-575`), after a batch rotation (`uses >= max_reuses` or `_claim_expiring`), `recyclable`
    is `True` and `held[img]` is `0`. Re-acquiring sets `handle = await afleet.acquire(t)`; if
    `reset.prime` fails before `held[img] += 1` (`:517`), `finally:` sees `holding == True` and
    decrements `held[img]` to `-1`, scaling the warm pool to `desired = active(0) - (-1) = 1`
    dangling replica after all shards exit!
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    cfg = FleetConfig(claim_mode="batch")
    sync_fleet = SandboxFleet(cfg, registry=ClusterRegistry([c]))
    tasks = sync_fleet.load_tasks(["img:1", "img:1"])
    sync_fleet.plan()
    afleet = AsyncSandboxFleet(cfg, sync_fleet=sync_fleet)

    scaled_replicas: list[int] = []
    fake_reset = MagicMock()
    fake_reset.recyclable.return_value = True
    fake_reset.prime.side_effect = ["anchor-0", RuntimeError("prime failed after rotation")]

    try:
        with patch.object(
            sync_fleet,
            "set_pool_replicas",
            side_effect=lambda _img, desired: scaled_replicas.append(desired),
        ):
            results = await reuse_git_restore_sandbox_async(
                afleet,
                tasks,
                lambda _t, _h: "ok",
                concurrency=1,
                reset=fake_reset,
                max_reuses=1,
                use_session=False,
                scale_on_hold=True,
            )
        assert results[0] == "ok"
        assert isinstance(results[1], RuntimeError)
        assert scaled_replicas[-1] == 0, (
            "F-28a (INV-LEAK-1) violated: prime failure on post-rotation re-claim decremented "
            f"held[img] to -1 and scaled warm pool to {scaled_replicas[-1]} dangling replica "
            f"(scaled_replicas={scaled_replicas})!"
        )
    finally:
        afleet.close(wait=True)


def test_rl_sync_recycle_reclaim_prime_failure_unwarms_jit_pool(
    make_cluster: Any,
) -> None:
    """Reproduces F-28b (INV-LEAK-1): In sync `reuse_git_restore_sandbox` (`recycle.py:337-354,
    389-394`), post-rotation re-claim calls `fleet.warm_image(..., replicas_override=1)` before
    `acquire` and `reset.prime`. When `reset.prime` fails before `fleet.unwarm_image` (`:354`),
    `finally:` releases `handle` without calling `unwarm_image`, leaving a dangling warm replica!
    """
    c = _setup_batch_cluster(make_cluster, "c1")
    cfg = FleetConfig(claim_mode="batch")
    sync_fleet = SandboxFleet(cfg, registry=ClusterRegistry([c]))
    tasks = sync_fleet.load_tasks(["img:1", "img:1"])
    sync_fleet.plan()

    pool_events: list[str] = []
    fake_reset = MagicMock()
    fake_reset.recyclable.return_value = True
    fake_reset.prime.side_effect = ["anchor-0", RuntimeError("prime failed after rotation")]

    with (
        patch.object(
            sync_fleet,
            "warm_image",
            side_effect=lambda *_a, **_kw: pool_events.append("warm"),
        ),
        patch.object(
            sync_fleet,
            "unwarm_image",
            side_effect=lambda *_a, **_kw: pool_events.append("unwarm"),
        ),
    ):
        results = reuse_git_restore_sandbox(
            sync_fleet,
            tasks,
            lambda _t, _h: "ok",
            concurrency=1,
            reset=fake_reset,
            max_reuses=1,
            use_session=False,
            scale_on_hold=True,
        )

    assert results[0] == "ok"
    assert isinstance(results[1], RuntimeError)
    assert pool_events[-1] == "unwarm", (
        "F-28b (INV-LEAK-1) violated: sync reuse_git_restore_sandbox left JIT warm_image pool "
        f"scaled up after re-claim prime failure (pool_events={pool_events})!"
    )
