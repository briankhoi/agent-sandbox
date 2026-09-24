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

import time
import uuid
from datetime import UTC, datetime

import kubernetes
import pytest
from test.e2e.clients.python.framework.context import TestContext
from test.e2e.clients.python.test_e2e_python_sdk import (  # noqa: F401
    deploy_router,
    sandbox_coldpool,
    sandbox_template,
    sandbox_warmpool,
    tc,
    temp_namespace,
)

from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.exceptions import (
    BatchNotFoundError,
    SandboxWarmPoolNotFoundError,
)
from k8s_agent_sandbox.models import (
    BatchEventType,
    BatchGroup,
    SandboxLocalTunnelConnectionConfig,
)

MEMBERS_READY_TIMEOUT_SECONDS = 120
# Long enough for a warm pool's claims to become Ready, short enough to wait out in a test.
BATCH_QUORUM_TIMEOUT_SECONDS = 90


def _batch_manifest(batch_id: str, warmpool: str) -> str:
    lease_duration = 300
    # metav1.MicroTime parses with RFC3339Micro, which requires exactly six fractional digits.
    renew_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    claims = "\n---\n".join(
        f"""apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxClaim
metadata:
  name: {batch_id}-{ordinal}
  labels:
    agents.x-k8s.io/batch-id: {batch_id}
  annotations:
    agents.x-k8s.io/batch-group-size: "2"
    agents.x-k8s.io/batch-group-min-ready: "2"
spec:
  warmPoolRef:
    name: {warmpool}"""
        for ordinal in (0, 1)
    )
    lease = f"""apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: batch-{batch_id}
  labels:
    agents.x-k8s.io/batch-id: {batch_id}
  annotations:
    agents.x-k8s.io/batch-lease-duration: "{lease_duration}"
spec:
  leaseDurationSeconds: {lease_duration}
  renewTime: "{renew_time}\""""
    return f"{claims}\n---\n{lease}\n"


def test_batch_get_batch_members_connect_detach(
    tc, temp_namespace, sandbox_warmpool, deploy_router
):
    batch_id = f"b{uuid.uuid4().hex[:10]}"
    tc.apply_manifest_text(_batch_manifest(batch_id, sandbox_warmpool), namespace=temp_namespace)

    config = SandboxLocalTunnelConnectionConfig(router_namespace=temp_namespace)
    client = SandboxClient(connection_config=config)
    try:
        batch = client.get_batch(batch_id, namespace=temp_namespace)

        deadline = time.monotonic() + MEMBERS_READY_TIMEOUT_SECONDS
        while True:
            members = batch.members()
            if len(members) == 2 and all(m.ready for m in members):
                break
            if time.monotonic() > deadline:
                pytest.fail(f"batch members did not become ready in time: {members}")
            time.sleep(1)

        sandbox = batch.connect(members[0])
        result = sandbox.commands.run("echo 'Hello from batch'")
        assert result.stdout == "Hello from batch\n"
        assert result.exit_code == 0

        batch.detach()

        custom_objects_api = tc.get_custom_objects_api()
        for ordinal in (0, 1):
            claim = custom_objects_api.get_namespaced_custom_object(
                group="extensions.agents.x-k8s.io",
                version="v1beta1",
                namespace=temp_namespace,
                plural="sandboxclaims",
                name=f"{batch_id}-{ordinal}",
            )
            assert claim is not None

        coordination_api = kubernetes.client.CoordinationV1Api(tc.get_api_client())
        lease = coordination_api.read_namespaced_lease(f"batch-{batch_id}", temp_namespace)
        assert lease.spec.holder_identity is None
    finally:
        client.delete_all()


def test_get_batch_not_found_raises(tc, temp_namespace):
    client = SandboxClient()
    with pytest.raises(BatchNotFoundError):
        client.get_batch("bnonexistent1", namespace=temp_namespace)


def _labeled_claims(tc, namespace, batch_id):
    return tc.get_custom_objects_api().list_namespaced_custom_object(
        group="extensions.agents.x-k8s.io",
        version="v1beta1",
        namespace=namespace,
        plural="sandboxclaims",
        label_selector=f"agents.x-k8s.io/batch-id={batch_id}",
    )["items"]


def _read_lease(tc, namespace, batch_id):
    coordination_api = kubernetes.client.CoordinationV1Api(tc.get_api_client())
    try:
        return coordination_api.read_namespaced_lease(f"batch-{batch_id}", namespace)
    except kubernetes.client.ApiException as e:
        if e.status == 404:
            return None
        raise


def test_claim_batch_events_then_release(tc, temp_namespace, sandbox_warmpool):
    client = SandboxClient()
    batch = client.claim_batch(
        [BatchGroup(warmpool=sandbox_warmpool, size=3)],
        namespace=temp_namespace,
        quorum_timeout=BATCH_QUORUM_TIMEOUT_SECONDS,
    )
    try:
        assert _read_lease(tc, temp_namespace, batch.batch_id) is not None
        events = list(batch.events())
        ready = [e.member.claim_name for e in events if e.type is BatchEventType.MEMBER_READY]
        assert sorted(ready) == sorted(f"{batch.batch_id}-{i}" for i in range(3)), events
    finally:
        batch.release()

    remaining = [
        claim
        for claim in _labeled_claims(tc, temp_namespace, batch.batch_id)
        if not (claim.get("metadata") or {}).get("deletionTimestamp")
    ]
    assert remaining == []
    assert _read_lease(tc, temp_namespace, batch.batch_id) is None


def test_claim_batch_nonexistent_warmpool_is_rejected_before_anything_exists(
    tc, temp_namespace, sandbox_warmpool
):
    client = SandboxClient()
    batch_id = f"b{uuid.uuid4().hex[:10]}"
    with pytest.raises(SandboxWarmPoolNotFoundError):
        client.claim_batch(
            [
                BatchGroup(warmpool=sandbox_warmpool, size=1),
                BatchGroup(warmpool="python-sdk-nonexistent-pool", size=1),
            ],
            namespace=temp_namespace,
            batch_id=batch_id,
        )
    assert _labeled_claims(tc, temp_namespace, batch_id) == []
    assert _read_lease(tc, temp_namespace, batch_id) is None


def test_iter_ready_groups_yields_error_for_a_stuck_group_and_members_for_the_other(
    tc, temp_namespace, sandbox_warmpool
):
    # The stuck pool exists, so claim_batch's precheck passes, but its image can never be
    # pulled: its claim stays pending with no terminal reason until quorum_timeout.
    tc.apply_manifest_text(
        """apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: python-sdk-unpullable-template
spec:
  podTemplate:
    spec:
      containers:
      - name: never-starts
        image: kind.local/python-sdk-image-that-does-not-exist:none
---
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxWarmPool
metadata:
  name: python-sdk-stuck-pool
spec:
  replicas: 0
  sandboxTemplateRef:
    name: python-sdk-unpullable-template
""",
        namespace=temp_namespace,
    )

    client = SandboxClient()
    batch = client.claim_batch(
        [
            BatchGroup(warmpool=sandbox_warmpool, size=2),
            BatchGroup(warmpool="python-sdk-stuck-pool", size=1),
        ],
        namespace=temp_namespace,
        quorum_timeout=BATCH_QUORUM_TIMEOUT_SECONDS,
    )
    try:
        results = {group.warmpool: group for group in batch.iter_ready_groups()}
    finally:
        batch.release()

    healthy = results[sandbox_warmpool]
    assert healthy.error is None
    assert len(healthy.members) == 2
    stuck = results["python-sdk-stuck-pool"]
    assert stuck.members == []
    assert isinstance(stuck.error, TimeoutError)
