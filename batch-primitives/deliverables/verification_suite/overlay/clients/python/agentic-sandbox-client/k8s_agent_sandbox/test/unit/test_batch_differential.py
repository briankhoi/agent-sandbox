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
Lockstep dual-runtime differential fuzzer for SandboxBatch vs AsyncSandboxBatch (Pipeline 5).

Drives sync and async batch handles side-by-side through identical seeded streams
of watch events, 410 relists, Lease renewals/faults, consumer reads, dynamic
acquires, sandbox connections, member/group releases, detach, and release,
checking full state, connection, error, and K8s call-log parity at every step.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import zlib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes import client as k8s_client
from kubernetes_asyncio import client as async_k8s_client

from k8s_agent_sandbox.async_sandbox_batch import AsyncSandboxBatch
from k8s_agent_sandbox.async_sandbox_client import AsyncSandboxClient
from k8s_agent_sandbox.batch_state import BatchState, ConsumerMode
from k8s_agent_sandbox.constants import (
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
)
from k8s_agent_sandbox.models import BatchGroup, MemberState, SandboxDirectConnectionConfig
from k8s_agent_sandbox.sandbox_batch import SandboxBatch
from k8s_agent_sandbox.sandbox_client import SandboxClient
from k8s_agent_sandbox.test.unit.batch_invariant_oracle import BatchInvariantOracle


def _group_timeout_resolves_held_ready() -> bool:
    """Probes whether F-12 (_time_out_undecided_groups resolving _held_ready) is fixed."""
    grp = BatchGroup(warmpool="probe-p", size=2, min_ready=2)
    probe = BatchState("probeb", [grp], next_ordinal=2)
    probe.set_mode(ConsumerMode.GROUP)
    probe.upsert_claim({
        "metadata": {
            "name": "probeb-0",
            "annotations": {
                BATCH_GROUP_SIZE_ANNOTATION: "2",
                BATCH_GROUP_MIN_READY_ANNOTATION: "2",
            },
        },
        "spec": {"warmPoolRef": {"name": "probe-p"}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True", "reason": "SandboxReady"}],
            "sandbox": {"name": "sbx-0", "podIPs": ["10.0.0.2"]},
        },
    })
    probe.expire_fill()
    return "probeb-0" in probe._resolved


_GROUP_TIMEOUT_FIXED = _group_timeout_resolves_held_ready()


def _make_lease(
    holder_identity: str | None = "holder-diff-1",
    resource_version: str = "10",
    uid: str = "lease-diff-uid-1",
    lease_duration_seconds: int = 60,
    annotations: dict[str, str] | None = None,
) -> k8s_client.V1Lease:
    return k8s_client.V1Lease(
        metadata=k8s_client.V1ObjectMeta(
            name="batch-diffbatch",
            resource_version=resource_version,
            uid=uid,
            annotations=dict(annotations or {}),
        ),
        spec=k8s_client.V1LeaseSpec(
            holder_identity=holder_identity,
            renew_time=datetime.now(UTC),
            lease_duration_seconds=lease_duration_seconds,
            lease_transitions=0,
        ),
    )


def _make_async_lease(
    holder_identity: str | None = "holder-diff-1",
    resource_version: str = "10",
    uid: str = "lease-diff-uid-1",
    lease_duration_seconds: int = 60,
    annotations: dict[str, str] | None = None,
) -> async_k8s_client.V1Lease:
    return async_k8s_client.V1Lease(
        metadata=async_k8s_client.V1ObjectMeta(
            name="batch-diffbatch",
            resource_version=resource_version,
            uid=uid,
            annotations=dict(annotations or {}),
        ),
        spec=async_k8s_client.V1LeaseSpec(
            holder_identity=holder_identity,
            renew_time=datetime.now(UTC),
            lease_duration_seconds=lease_duration_seconds,
            lease_transitions=0,
        ),
    )


def _claim_dict(
    name: str,
    *,
    warmpool: str,
    group_spec: BatchGroup | None,
    ready_state: str,
    pod_gen: int = 1,
    rv: str = "11",
) -> dict[str, Any]:
    annotations: dict[str, str] = {}
    if group_spec is not None:
        annotations[BATCH_GROUP_SIZE_ANNOTATION] = str(group_spec.size)
        annotations[BATCH_GROUP_MIN_READY_ANNOTATION] = str(group_spec.min_ready or 0)
    if ready_state == "ready":
        ip_octet = (zlib.crc32(name.encode("utf-8")) % 200) + 2
        status: dict[str, Any] = {
            "conditions": [{"type": "Ready", "status": "True", "reason": "SandboxReady"}],
            "sandbox": {
                "name": f"sbx-{name}-{pod_gen}",
                "podIPs": [f"10.42.{pod_gen % 200}.{ip_octet}"],
            },
        }
    elif ready_state == "cold":
        status = {
            "conditions": [{"type": "Ready", "status": "False", "reason": "SandboxNotReady"}],
            "sandbox": {"name": f"sbx-{name}-{pod_gen}"},
        }
    elif ready_state == "failed":
        status = {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "PodFailed",
                    "message": "container exited 1",
                }
            ],
            "sandbox": {"name": f"sbx-{name}-{pod_gen}"},
        }
    else:
        status = {}
    return {
        "metadata": {"name": name, "resourceVersion": rv, "annotations": annotations},
        "spec": {"warmPoolRef": {"name": warmpool}},
        "status": status,
    }


class _DualRuntimeRig:
    """Wraps a stopped SandboxBatch and AsyncSandboxBatch with identical scripted K8s backends."""

    def __init__(self, groups: list[BatchGroup]) -> None:
        self.groups = groups
        self.sync_ops: list[tuple[str, str]] = []
        self.async_ops: list[tuple[str, str]] = []
        self.sync_closed_ids: list[str] = []
        self.async_closed_ids: list[str] = []
        self.sync_rv_counter = 20
        self.async_rv_counter = 20
        self.sync_client: SandboxClient[Any] | None = None
        self.async_client: AsyncSandboxClient[Any] | None = None
        self.sync_batch: SandboxBatch | None = None
        self.async_batch: AsyncSandboxBatch | None = None

    async def setup(self) -> None:
        with patch("k8s_agent_sandbox.sandbox_client.K8sHelper"):
            self.sync_client = SandboxClient(cleanup=False)
        sh: Any = self.sync_client.k8s_helper
        sh.api_pool_size.return_value = 25
        sh.count_live_sandbox_claims.return_value = 0

        def _sync_get_pool(name: str, _ns: str, **_kw: Any) -> dict[str, Any]:
            self.sync_ops.append(("get_warmpool", name))
            return {
                "spec": {"sandboxTemplateRef": {"name": "tmpl"}},
                "status": {"readyReplicas": 2},
            }

        def _sync_get_tmpl(name: str, _ns: str, **_kw: Any) -> dict[str, Any]:
            self.sync_ops.append(("get_template", name))
            return {"metadata": {"name": name}}

        sh.get_sandbox_warmpool.side_effect = _sync_get_pool
        sh.get_sandbox_template.side_effect = _sync_get_tmpl
        sh.create_batch_lease.return_value = _make_lease()
        sh.list_sandbox_claim_objects.return_value = ([], "10")

        def _make_sync_sbx(**kw: Any) -> MagicMock:
            sid = kw.get("sandbox_id", "")
            sbx = MagicMock(sandbox_id=sid, claim_name=kw.get("claim_name"))
            sbx.close_connection = MagicMock(side_effect=lambda: self.sync_closed_ids.append(sid))
            return sbx

        sync_client_any: Any = self.sync_client
        sync_client_any.sandbox_class = MagicMock(side_effect=_make_sync_sbx)
        with (
            patch.object(SandboxBatch, "_start", lambda _s: None),
            patch.object(SandboxBatch, "_create_worker", lambda _s: None),
        ):
            self.sync_batch = self.sync_client.claim_batch(
                self.groups,
                batch_id="diffbatch",
                lease_duration=60,
                quorum_timeout=5,
                create_rps=1000.0,
            )
        self.sync_batch._holder_identity = "holder-diff-1"
        assert self.sync_batch._lease.spec is not None
        self.sync_batch._lease.spec.holder_identity = "holder-diff-1"

        with patch("k8s_agent_sandbox.async_sandbox_client.AsyncK8sHelper"):
            self.async_client = AsyncSandboxClient(
                connection_config=SandboxDirectConnectionConfig(api_url="http://127.0.0.1:8080"),
                cleanup=False,
            )
        ah: Any = self.async_client.k8s_helper
        ah.api_pool_size = AsyncMock(return_value=25)
        ah.count_live_sandbox_claims = AsyncMock(return_value=0)

        async def _async_get_pool(name: str, _ns: str, **_kw: Any) -> dict[str, Any]:
            self.async_ops.append(("get_warmpool", name))
            return {
                "spec": {"sandboxTemplateRef": {"name": "tmpl"}},
                "status": {"readyReplicas": 2},
            }

        async def _async_get_tmpl(name: str, _ns: str, **_kw: Any) -> dict[str, Any]:
            self.async_ops.append(("get_template", name))
            return {"metadata": {"name": name}}

        ah.get_sandbox_warmpool = AsyncMock(side_effect=_async_get_pool)
        ah.get_sandbox_template = AsyncMock(side_effect=_async_get_tmpl)
        ah.create_batch_lease = AsyncMock(return_value=_make_async_lease())
        ah.list_sandbox_claim_objects = AsyncMock(return_value=([], "10"))

        def _make_async_sbx(**kw: Any) -> MagicMock:
            sid = kw.get("sandbox_id", "")
            sbx = MagicMock(
                sandbox_id=sid,
                claim_name=kw.get("claim_name"),
                connector=MagicMock(),
            )

            async def _close() -> None:
                self.async_closed_ids.append(sid)

            sbx.close_connection = AsyncMock(side_effect=_close)
            return sbx

        async_client_any: Any = self.async_client
        async_client_any.sandbox_class = MagicMock(side_effect=_make_async_sbx)

        async def _noop_worker(_self: Any) -> None:
            return None

        with (
            patch.object(AsyncSandboxBatch, "_start", lambda _s: None),
            patch.object(AsyncSandboxBatch, "_create_worker", _noop_worker),
        ):
            self.async_batch = await self.async_client.claim_batch(
                self.groups,
                batch_id="diffbatch",
                lease_duration=60,
                quorum_timeout=5,
                create_rps=1000.0,
            )
        self.async_batch._holder_identity = "holder-diff-1"
        assert self.async_batch._lease.spec is not None
        self.async_batch._lease.spec.holder_identity = "holder-diff-1"

        # Clear setup call logs so differential assertions compare runtime actions cleanly.
        self.sync_ops.clear()
        self.async_ops.clear()
        self._install_default_handlers()

    def _install_default_handlers(self) -> None:
        assert self.sync_client is not None and self.async_client is not None
        assert self.sync_batch is not None and self.async_batch is not None
        sh: Any = self.sync_client.k8s_helper
        ah: Any = self.async_client.k8s_helper
        sb = self.sync_batch
        ab = self.async_batch

        def _sync_read_lease(name: str, _ns: str, **_kw: Any) -> k8s_client.V1Lease:
            self.sync_ops.append(("read_lease", name))
            return sb._lease

        sh.read_batch_lease.side_effect = _sync_read_lease

        def _sync_replace_lease(name: str, _ns: str, body: Any, **_kw: Any) -> k8s_client.V1Lease:
            self.sync_ops.append(("replace_lease", name))
            self.sync_rv_counter += 1
            written = _make_lease(
                holder_identity=body.spec.holder_identity,
                resource_version=str(self.sync_rv_counter),
                lease_duration_seconds=body.spec.lease_duration_seconds or 60,
                annotations=body.metadata.annotations,
            )
            sb._lease = written
            return written

        def _sync_del_claim(name: str, _ns: str, **_kw: Any) -> None:
            self.sync_ops.append(("delete_claim", name))

        def _sync_del_label(_ns: str, sel: str, **_kw: Any) -> None:
            self.sync_ops.append(("delete_by_label", sel))

        def _sync_del_lease(name: str, _ns: str, **_kw: Any) -> None:
            self.sync_ops.append(("delete_lease", name))

        sh.replace_batch_lease.side_effect = _sync_replace_lease
        sh.delete_sandbox_claim.side_effect = _sync_del_claim
        sh.delete_sandbox_claims_by_label.side_effect = _sync_del_label
        sh.delete_batch_lease.side_effect = _sync_del_lease

        async def _async_read_lease(name: str, _ns: str, **_kw: Any) -> async_k8s_client.V1Lease:
            self.async_ops.append(("read_lease", name))
            return ab._lease

        async def _async_replace_lease(
            name: str, _ns: str, body: Any, **_kw: Any
        ) -> async_k8s_client.V1Lease:
            self.async_ops.append(("replace_lease", name))
            self.async_rv_counter += 1
            written = _make_async_lease(
                holder_identity=body.spec.holder_identity,
                resource_version=str(self.async_rv_counter),
                lease_duration_seconds=body.spec.lease_duration_seconds or 60,
                annotations=body.metadata.annotations,
            )
            ab._lease = written
            return written

        async def _async_del_claim(name: str, _ns: str, **_kw: Any) -> None:
            self.async_ops.append(("delete_claim", name))

        async def _async_del_label(_ns: str, sel: str, **_kw: Any) -> None:
            self.async_ops.append(("delete_by_label", sel))

        async def _async_del_lease(name: str, _ns: str, **_kw: Any) -> None:
            self.async_ops.append(("delete_lease", name))

        ah.read_batch_lease = AsyncMock(side_effect=_async_read_lease)
        ah.replace_batch_lease = AsyncMock(side_effect=_async_replace_lease)
        ah.delete_sandbox_claim = AsyncMock(side_effect=_async_del_claim)
        ah.delete_sandbox_claims_by_label = AsyncMock(side_effect=_async_del_label)
        ah.delete_batch_lease = AsyncMock(side_effect=_async_del_lease)

    async def deliver_watch_burst(
        self,
        events: list[dict[str, Any]],
        *,
        trigger_410_first: bool = False,
        relist_items: list[dict[str, Any]] | None = None,
    ) -> None:
        """Feeds an identical watch burst (and optional 410 relist) into both real
        _watch_loop methods.
        """
        assert self.sync_client is not None and self.async_client is not None
        assert self.sync_batch is not None and self.async_batch is not None
        sh: Any = self.sync_client.k8s_helper
        ah: Any = self.async_client.k8s_helper
        sb = self.sync_batch
        ab = self.async_batch

        sb._watch_stop.clear()
        sync_watch_calls = 0

        def _sync_list(_ns: str, _sel: str, **_kw: Any) -> tuple[list[dict[str, Any]], str]:
            self.sync_ops.append(("relist", str(len(relist_items or []))))
            return list(relist_items or []), "50"

        def _sync_watch(*_a: Any, **_kw: Any) -> Any:
            nonlocal sync_watch_calls
            sync_watch_calls += 1
            if trigger_410_first and sync_watch_calls == 1:
                raise k8s_client.ApiException(status=410, reason="Gone")
            for ev in events:
                yield ev
            sb._watch_stop.set()

        sh.list_sandbox_claim_objects.side_effect = _sync_list
        sh.watch_sandbox_claims.side_effect = _sync_watch
        sb._watch_loop()
        sb._watch_stop.clear()

        async_watch_calls = 0

        async def _async_list(_ns: str, _sel: str, **_kw: Any) -> tuple[list[dict[str, Any]], str]:
            self.async_ops.append(("relist", str(len(relist_items or []))))
            return list(relist_items or []), "50"

        async def _async_watch(*_a: Any, **_kw: Any) -> Any:
            nonlocal async_watch_calls
            async_watch_calls += 1
            if trigger_410_first and async_watch_calls == 1:
                raise async_k8s_client.ApiException(status=410, reason="Gone")
            for ev in events:
                yield ev
            raise asyncio.CancelledError()

        ah.list_sandbox_claim_objects = AsyncMock(side_effect=_async_list)
        ah.watch_sandbox_claims = _async_watch
        with contextlib.suppress(asyncio.CancelledError):
            await ab._watch_loop()

    def assert_parity(self, step_desc: str) -> None:
        """Asserts complete behavioral and structural parity between sync and async handles."""
        assert self.sync_batch is not None and self.async_batch is not None
        sb = self.sync_batch
        ab = self.async_batch

        assert sb.members() == ab.members(), (
            f"[{step_desc}] members() diverged:\n  sync={sb.members()}\n  async={ab.members()}"
        )
        assert type(sb.err()) is type(ab.err()), (
            f"[{step_desc}] err() type diverged: sync={sb.err()!r} vs async={ab.err()!r}"
        )
        assert sb.lease_degraded() == ab.lease_degraded(), (
            f"[{step_desc}] lease_degraded() diverged: "
            f"sync={sb.lease_degraded()} vs async={ab.lease_degraded()}"
        )
        assert set(sb._connected.keys()) == set(ab._connected.keys()), (
            f"[{step_desc}] _connected keys diverged: "
            f"sync={sorted(sb._connected)} vs async={sorted(ab._connected)}"
        )
        assert len(sb._stale_connections) == len(ab._stale_connections), (
            f"[{step_desc}] _stale_connections len diverged: "
            f"sync={len(sb._stale_connections)} vs async={len(ab._stale_connections)}"
        )
        assert self.sync_closed_ids == self.async_closed_ids, (
            f"[{step_desc}] closed sandbox connection sequence diverged: "
            f"sync={self.sync_closed_ids} vs async={self.async_closed_ids}"
        )
        assert sb._state.next_ordinal == ab._state.next_ordinal, (
            f"[{step_desc}] next_ordinal diverged: "
            f"sync={sb._state.next_ordinal} vs async={ab._state.next_ordinal}"
        )
        assert sb._state._pending_deletes == ab._state._pending_deletes, (
            f"[{step_desc}] _pending_deletes diverged: "
            f"sync={sb._state._pending_deletes} vs async={ab._state._pending_deletes}"
        )
        assert self.sync_ops == self.async_ops, (
            f"[{step_desc}] outbound K8s call log diverged:\n"
            f"  sync={self.sync_ops}\n  async={self.async_ops}"
        )

        # Enforce dropped-ready resolved check automatically once F-12 is fixed.
        BatchInvariantOracle.assert_structural_invariants(
            sb._state,
            check_dropped_ready_resolved=_GROUP_TIMEOUT_FIXED,
            in_flight_acquire_names=set(),
        )
        BatchInvariantOracle.assert_structural_invariants(
            ab._state,
            check_dropped_ready_resolved=_GROUP_TIMEOUT_FIXED,
            in_flight_acquire_names=set(),
        )


def _capture_sync(fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, type[BaseException] | None]:
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:
        return None, type(exc)


async def _capture_async(
    coro_fn: Any, *args: Any, **kwargs: Any
) -> tuple[Any, type[BaseException] | None]:
    try:
        return await coro_fn(*args, **kwargs), None
    except Exception as exc:
        return None, type(exc)


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", list(range(50)))
async def test_seeded_lockstep_sync_async_differential_parity(seed: int) -> None:
    """Runs 50 seeded 20-step lockstep traces across SandboxBatch and AsyncSandboxBatch."""
    rng = random.Random(seed)
    group_choices = [
        [BatchGroup(warmpool="gpu-a", size=2, min_ready=1)],
        [
            BatchGroup(warmpool="gpu-a", size=2, min_ready=2),
            BatchGroup(warmpool="cpu-a", size=1, min_ready=1),
        ],
        [
            BatchGroup(warmpool="gpu-a", size=3, min_ready=2),
            BatchGroup(warmpool="gpu-b", size=0, min_ready=0),
        ],
    ]
    groups = group_choices[seed % len(group_choices)]
    rig = _DualRuntimeRig(groups)
    await rig.setup()
    assert rig.sync_batch is not None and rig.async_batch is not None
    assert rig.sync_client is not None and rig.async_client is not None

    fill_slots: list[tuple[str, BatchGroup]] = []
    ord_counter = 0
    for g in groups:
        for _ in range(g.size):
            fill_slots.append((f"diffbatch-{ord_counter}", g))
            ord_counter += 1

    pod_gens: dict[str, int] = {cname: 1 for cname, _ in fill_slots}
    consumer_kind = ["stream", "group", "quorum"][seed % 3]
    quorum_called = False

    for step_idx in range(18):
        action = rng.choice([
            "watch_event",
            "watch_event",
            "watch_event",
            "watch_410",
            "renew_ok",
            "renew_503",
            "renew_409_same",
            "connect_ready",
            "dynamic_acquire",
            "release_member",
            "release_not_ready",
            "release_group",
            "drain_consumer",
        ])
        desc = f"seed={seed} step={step_idx} action={action}"

        if action == "watch_event" and fill_slots:
            cname, grp = rng.choice(fill_slots)
            ev_kind = rng.choice(["ready", "ready", "cold", "failed", "deleted", "pod_swap"])
            if ev_kind == "deleted":
                burst = [
                    {
                        "type": "DELETED",
                        "object": {"metadata": {"name": cname, "resourceVersion": "30"}},
                    }
                ]
            else:
                if ev_kind == "pod_swap":
                    pod_gens[cname] = pod_gens.get(cname, 1) + 1
                    st_name = "ready"
                else:
                    st_name = ev_kind
                burst = [
                    {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "29"}}},
                    {
                        "type": "MODIFIED",
                        "object": _claim_dict(
                            cname,
                            warmpool=grp.warmpool,
                            group_spec=grp,
                            ready_state=st_name,
                            pod_gen=pod_gens.get(cname, 1),
                            rv="30",
                        ),
                    },
                ]
            await rig.deliver_watch_burst(burst)

        elif action == "watch_410" and fill_slots:
            relist_objs: list[dict[str, Any]] = []
            for cname, grp in fill_slots:
                if rng.random() < 0.75:
                    relist_objs.append(
                        _claim_dict(
                            cname,
                            warmpool=grp.warmpool,
                            group_spec=grp,
                            ready_state=rng.choice(["ready", "cold"]),
                            pod_gen=pod_gens.get(cname, 1),
                            rv="45",
                        )
                    )
            await rig.deliver_watch_burst([], trigger_410_first=True, relist_items=relist_objs)

        elif action == "renew_ok":
            rig._install_default_handlers()
            sr = rig.sync_batch._renew_once()
            ar = await rig.async_batch._renew_once()
            assert sr == ar, f"[{desc}] _renew_once() return diverged: {sr} vs {ar}"

        elif action == "renew_503":
            sh_any: Any = rig.sync_client.k8s_helper
            ah_any: Any = rig.async_client.k8s_helper
            sh_any.replace_batch_lease.side_effect = k8s_client.ApiException(
                status=503, reason="Service Unavailable"
            )
            ah_any.replace_batch_lease = AsyncMock(
                side_effect=async_k8s_client.ApiException(status=503, reason="Service Unavailable")
            )
            sr = rig.sync_batch._renew_once()
            ar = await rig.async_batch._renew_once()
            assert sr == ar
            rig._install_default_handlers()

        elif action == "renew_409_same":
            sync_attempts = 0
            async_attempts = 0
            sb_ref = rig.sync_batch
            ab_ref = rig.async_batch

            def _sync_409_then_ok(name: str, _ns: str, body: Any, **_kw: Any) -> k8s_client.V1Lease:
                nonlocal sync_attempts
                sync_attempts += 1
                rig.sync_ops.append(("replace_lease", name))
                if sync_attempts == 1:
                    raise k8s_client.ApiException(status=409, reason="Conflict")
                written = _make_lease(
                    holder_identity=body.spec.holder_identity,
                    resource_version="88",
                    lease_duration_seconds=body.spec.lease_duration_seconds or 60,
                )
                sb_ref._lease = written
                return written

            async def _async_409_then_ok(
                name: str, _ns: str, body: Any, **_kw: Any
            ) -> async_k8s_client.V1Lease:
                nonlocal async_attempts
                async_attempts += 1
                rig.async_ops.append(("replace_lease", name))
                if async_attempts == 1:
                    raise async_k8s_client.ApiException(status=409, reason="Conflict")
                written = _make_async_lease(
                    holder_identity=body.spec.holder_identity,
                    resource_version="88",
                    lease_duration_seconds=body.spec.lease_duration_seconds or 60,
                )
                ab_ref._lease = written
                return written

            sh_409: Any = rig.sync_client.k8s_helper
            ah_409: Any = rig.async_client.k8s_helper
            sh_409.replace_batch_lease.side_effect = _sync_409_then_ok
            ah_409.replace_batch_lease = AsyncMock(side_effect=_async_409_then_ok)
            sr = rig.sync_batch._renew_once()
            ar = await rig.async_batch._renew_once()
            assert sr == ar
            rig._install_default_handlers()

        elif action == "connect_ready":
            members = rig.sync_batch.members()
            if members:
                target = rng.choice(members)
                _, s_exc = _capture_sync(rig.sync_batch.connect, target)
                _, a_exc = await _capture_async(rig.async_batch.connect, target)
                assert s_exc is a_exc, f"[{desc}] connect() exc diverged: {s_exc} vs {a_exc}"

        elif action == "dynamic_acquire":
            pool = rng.choice(["gpu-a", "dyn-extra"])
            outcome_mode = rng.choice(["ready", "create_422", "pod_failed"])

            def _sync_create(cname: str, wpool: str, _ns: str, **_kw: Any) -> None:
                rig.sync_ops.append(("create_claim", cname))
                if outcome_mode == "create_422":
                    raise k8s_client.ApiException(status=422, reason="Invalid")
                obj = _claim_dict(
                    cname,
                    warmpool=wpool,
                    group_spec=None,
                    ready_state="ready" if outcome_mode == "ready" else "failed",
                    pod_gen=1,
                )
                assert rig.sync_batch is not None
                with rig.sync_batch._lock:
                    m = rig.sync_batch._state.upsert_claim(obj)
                    if m is not None and m.state is not MemberState.PENDING:
                        rig.sync_batch._wake_acquire(cname)

            async def _async_create(cname: str, wpool: str, _ns: str, **_kw: Any) -> None:
                rig.async_ops.append(("create_claim", cname))
                if outcome_mode == "create_422":
                    raise async_k8s_client.ApiException(status=422, reason="Invalid")
                obj = _claim_dict(
                    cname,
                    warmpool=wpool,
                    group_spec=None,
                    ready_state="ready" if outcome_mode == "ready" else "failed",
                    pod_gen=1,
                )
                assert rig.async_batch is not None
                async with rig.async_batch._lock:
                    m = rig.async_batch._state.upsert_claim(obj)
                    if m is not None and m.state is not MemberState.PENDING:
                        rig.async_batch._wake_acquire(cname)

            sh_dyn: Any = rig.sync_client.k8s_helper
            ah_dyn: Any = rig.async_client.k8s_helper
            sh_dyn.create_sandbox_claim.side_effect = _sync_create
            ah_dyn.create_sandbox_claim = AsyncMock(side_effect=_async_create)
            s_val, s_exc = _capture_sync(rig.sync_batch.acquire, pool, 1.0)
            a_val, a_exc = await _capture_async(rig.async_batch.acquire, pool, 1.0)
            assert s_exc is a_exc, f"[{desc}] acquire() exc diverged: {s_exc} vs {a_exc}"
            assert s_val == a_val, f"[{desc}] acquire() result diverged: {s_val} vs {a_val}"

        elif action == "release_member":
            candidates = [m.claim_name for m in rig.sync_batch.members()] + ["nonexistent-claim"]
            target_name = rng.choice(candidates)
            _, s_exc = _capture_sync(rig.sync_batch.release_member, target_name)
            _, a_exc = await _capture_async(rig.async_batch.release_member, target_name)
            assert s_exc is a_exc, f"[{desc}] release_member() exc diverged: {s_exc} vs {a_exc}"

        elif action == "release_not_ready":
            _, s_exc = _capture_sync(rig.sync_batch.release_not_ready)
            _, a_exc = await _capture_async(rig.async_batch.release_not_ready)
            assert s_exc is a_exc

        elif action == "release_group":
            pool = rng.choice(["gpu-a", "cpu-a", "dyn-extra", "unknown-pool"])
            _, s_exc = _capture_sync(rig.sync_batch.release_group, pool)
            _, a_exc = await _capture_async(rig.async_batch.release_group, pool)
            assert s_exc is a_exc

        elif action == "drain_consumer":
            if consumer_kind == "stream":
                # Pop only already-queued items without blocking if fill isn't settled yet.
                s_it = rig.sync_batch.events()
                a_it = rig.async_batch.events()
                while rig.sync_batch._state._events_to_yield and (
                    rig.sync_batch._state.fill_settled()
                    or any(
                        ev.member.claim_name not in rig.sync_batch._state._released
                        or ev.type.value != "member_ready"
                        for ev in rig.sync_batch._state._events_to_yield
                    )
                ):
                    s_ev = next(s_it, None)
                    a_ev = await anext(a_it, None)
                    assert s_ev == a_ev, f"[{desc}] events() item diverged: {s_ev} vs {a_ev}"
                    if s_ev is None:
                        break
            elif consumer_kind == "group":
                s_it_g = rig.sync_batch.group_outcomes()
                a_it_g = rig.async_batch.group_outcomes()
                while rig.sync_batch._state._group_outcomes_to_yield:
                    s_out = next(s_it_g)
                    a_out = await anext(a_it_g)
                    assert type(s_out.error) is type(a_out.error)
                    assert s_out.members == a_out.members
            elif consumer_kind == "quorum" and not quorum_called:
                if rig.sync_batch._state.quorum_result() is not None:
                    quorum_called = True
                    s_q, s_exc = _capture_sync(rig.sync_batch.wait_for_quorum)
                    a_q, a_exc = await _capture_async(rig.async_batch.wait_for_quorum)
                    assert s_exc is a_exc
                    assert s_q == a_q

        rig.assert_parity(desc)

    # Terminal lifecycle parity: alternate between detach(grace=...) and release().
    rig._install_default_handlers()
    if seed % 2 == 0:
        _, s_exc = _capture_sync(rig.sync_batch.detach, 30)
        _, a_exc = await _capture_async(rig.async_batch.detach, 30)
        assert s_exc is a_exc
    else:
        _, s_exc = _capture_sync(rig.sync_batch.release)
        _, a_exc = await _capture_async(rig.async_batch.release)
        assert s_exc is a_exc

    rig.assert_parity(f"seed={seed} terminal")


@pytest.mark.asyncio
async def test_differential_acquire_interruption_cleanup_parity() -> None:
    """Verifies lockstep cleanup parity between sync SandboxBatch (KeyboardInterrupt) and
    async AsyncSandboxBatch (repeated CancelledError) during _drop_acquired under real
    production lock discipline (protected by INV-LOCK-ASYNC-1).
    """
    rig = _DualRuntimeRig([BatchGroup(warmpool="gpu-a", size=0, min_ready=0)])
    await rig.setup()
    assert rig.sync_batch is not None and rig.async_batch is not None
    assert rig.sync_client is not None and rig.async_client is not None

    def _sync_create(_cname: str, _pool: str, _ns: str, **_kw: Any) -> None:
        return None

    def _sync_interrupt_delete(_cname: str, _ns: str, **_kw: Any) -> None:
        raise KeyboardInterrupt()

    sh: Any = rig.sync_client.k8s_helper
    sh.create_sandbox_claim.side_effect = _sync_create
    sh.delete_sandbox_claim.side_effect = _sync_interrupt_delete
    with (
        patch.object(SandboxBatch, "_wait_acquired", side_effect=KeyboardInterrupt()),
        pytest.raises(KeyboardInterrupt),
    ):
        rig.sync_batch.acquire("gpu-a", timeout=5.0)

    created_async = asyncio.Event()
    delete_entered_async = asyncio.Event()
    allow_delete_async = asyncio.Event()

    async def _async_create(_cname: str, _pool: str, _ns: str, **_kw: Any) -> None:
        created_async.set()

    async def _async_interruptible_delete(_cname: str, _ns: str, **_kw: Any) -> None:
        delete_entered_async.set()
        await allow_delete_async.wait()

    ah: Any = rig.async_client.k8s_helper
    ah.create_sandbox_claim = AsyncMock(side_effect=_async_create)
    ah.delete_sandbox_claim = AsyncMock(side_effect=_async_interruptible_delete)

    acq_task = asyncio.create_task(rig.async_batch.acquire("gpu-a", timeout=10.0))
    await asyncio.wait_for(created_async.wait(), timeout=2.0)
    await asyncio.sleep(0.01)

    acq_task.cancel()
    await asyncio.wait_for(delete_entered_async.wait(), timeout=2.0)
    acq_task.cancel()
    allow_delete_async.set()
    with pytest.raises(asyncio.CancelledError):
        await acq_task
    await rig.async_batch._wait_active_deletes()

    assert set(rig.sync_batch._acquire_waiters.keys()) == set(
        rig.async_batch._acquire_waiters.keys()
    )
    assert rig.sync_batch._state._released == rig.async_batch._state._released
    assert rig.sync_batch._state._pending_deletes == rig.async_batch._state._pending_deletes


@pytest.mark.asyncio
async def test_differential_group_timeout_resolves_dropped_ready_members() -> None:
    """Reproduces F-12 (INV-ACCT-1) across both SandboxBatch and AsyncSandboxBatch:
    when a group with min_ready=2 times out with 1 Ready + 1 Pending claim,
    `_time_out_undecided_groups` must add dropped `_waiting` claims to `_resolved`.
    """
    grp = BatchGroup(warmpool="gpu-a", size=2, min_ready=2)
    rig = _DualRuntimeRig([grp])
    await rig.setup()
    assert rig.sync_batch is not None and rig.async_batch is not None

    await rig.deliver_watch_burst([
        {
            "type": "ADDED",
            "object": _claim_dict(
                "diffbatch-0", warmpool="gpu-a", group_spec=grp, ready_state="ready", rv="21"
            ),
        },
        {
            "type": "ADDED",
            "object": _claim_dict(
                "diffbatch-1", warmpool="gpu-a", group_spec=grp, ready_state="cold", rv="22"
            ),
        },
    ])
    rig.sync_batch._fill_deadline = 0.0
    rig.async_batch._fill_deadline = 0.0

    s_outcomes = list(rig.sync_batch.group_outcomes())
    a_outcomes = [o async for o in rig.async_batch.group_outcomes()]
    assert len(s_outcomes) == 1 and len(a_outcomes) == 1
    assert s_outcomes[0].error is not None and a_outcomes[0].error is not None

    BatchInvariantOracle.assert_structural_invariants(
        rig.sync_batch._state,
        check_dropped_ready_resolved=True,
        in_flight_acquire_names=set(),
    )
    BatchInvariantOracle.assert_structural_invariants(
        rig.async_batch._state,
        check_dropped_ready_resolved=True,
        in_flight_acquire_names=set(),
    )
