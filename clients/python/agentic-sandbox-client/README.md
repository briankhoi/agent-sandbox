# Agentic Sandbox Client Python

This Python client provides a simple, high-level interface for creating and interacting with
sandboxes managed by the Agent Sandbox controller. It's designed to be used as a context manager,
ensuring that sandbox resources are properly created and cleaned up.

It supports a **scalable, cloud-native architecture** using Kubernetes Gateways and a specialized
Router, while maintaining a convenient **Tunnel Mode** for local testing.

## Architecture

The client operates in four connectivity modes:

1.  **Gateway Mode:** Traffic flows from the Client -> Cloud Load Balancer (Gateway)
    -> Router Service -> Sandbox Pod. This supports external ingress via Gateway API.
2.  **Tunnel Mode:** Traffic flows from Localhost -> `kubectl port-forward` -> Router
    Service -> Sandbox Pod. This requires no public IP and works on Kind/Minikube for local development.
3.  **In-Cluster Mode:** The client connects **directly to the sandbox pod** (via pod IP or cluster
    DNS), bypassing the router. Intended for workloads running inside the cluster.
4.  **Direct URL Mode:** The client connects directly to a provided `api_url`, bypassing
    discovery. This is useful when connecting through a custom domain or a manually specified router URL.

## Prerequisites

- A running Kubernetes cluster.
- The [**Agent Sandbox Controller**](https://github.com/kubernetes-sigs/agent-sandbox?tab=readme-ov-file#installation) installed.
- `kubectl` installed and configured locally.

## Setup: Deploying the Router

Before using the client in Gateway Mode or Tunnel Mode, deploy the `sandbox-router` into your cluster.

1.  **Deploy the Router:**

    Follow the instructions in [sandbox-router](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/sandbox-router) to deploy the router using the manifests in [sandbox-router/deploy](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/sandbox-router/deploy). *(Note: If you installed a specific client release tag, replace `main` in these URLs with the corresponding tag.)*

2.  **Create a Sandbox Warmpool:**

    Ensure a `SandboxWarmPool` exists in your target namespace. The test_client.py
    uses the [python-runtime-sandbox](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/examples/python-runtime-sandbox) image.

    ```bash
    kubectl apply -f python-sandbox-warmpool.yaml
    ```

## Installation

1.  **Create a virtual environment:**

    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    ```

2.  **Install Agent Sandbox Client**
    

    * **Option 1: Install from PyPI (Recommended):**

        The package is available on [PyPI](https://pypi.org/project/k8s-agent-sandbox/) as `k8s-agent-sandbox`.

        ```bash
        pip install k8s-agent-sandbox
        ```

        If you are using [tracing with GCP](GCP.md#tracing-with-open-telemetry-and-google-cloud-trace), install with the optional tracing dependencies:

        ```bash
        pip install "k8s-agent-sandbox[tracing]"
        ```


    * **Option 2: Install from source via git:**

        ```bash
        # Replace "main" with a specific version tag (e.g., "v0.1.0") from
        # https://github.com/kubernetes-sigs/agent-sandbox/releases to pin a version tag.
        export VERSION="main"

        pip install "git+https://github.com/kubernetes-sigs/agent-sandbox.git@${VERSION}#subdirectory=clients/python/agentic-sandbox-client"
        ```

        **Note**: This package uses `setuptools-scm` for dynamic versioning. For Option 2 and Option 3, when installing locally, you may notice the version increment if your local repository has uncommitted changes or is ahead of the last tagged release. This is expected behavior to ensure unique versioning during development.

    * **Option 3: Install from source in editable mode:**

        If you have not already done so, first clone this repository:

        ```bash
        cd ~
        git clone https://github.com/kubernetes-sigs/agent-sandbox.git
        cd agent-sandbox/clients/python/agentic-sandbox-client
        ```

        And then install the agentic-sandbox-client into your activated .venv:

        ```bash
        pip install -e .
        ```

        If you are using [tracing with GCP](GCP.md#tracing-with-open-telemetry-and-google-cloud-trace),
        install with the optional tracing dependencies:

        ```bash
        pip install -e ".[tracing]"
        ```

## Usage Examples

### 1. Gateway Mode (GKE Gateway)

Use this when running against a real cluster with a public Gateway IP. The client automatically
discovers the Gateway.

```python
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxGatewayConnectionConfig

# Connect via the GKE Gateway
client = SandboxClient(
    connection_config=SandboxGatewayConnectionConfig(
        gateway_name="external-http-gateway",  # Name of the Gateway resource
    )
)

sandbox = client.create_sandbox(warmpool="python-sandbox-warmpool", namespace="default")
try:
    print(sandbox.commands.run("echo 'Hello from Cloud!'").stdout)
finally:
    sandbox.terminate()
```

### 2. Tunnel Mode (Local Port-Forward)

Use this for local development or CI. The client automatically opens a secure tunnel to the
Router Service using `kubectl`.

```python
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxLocalTunnelConnectionConfig

# Automatically tunnels to svc/sandbox-router-svc
client = SandboxClient(
    connection_config=SandboxLocalTunnelConnectionConfig()
)

sandbox = client.create_sandbox(warmpool="python-sandbox-warmpool", namespace="default")
try:
    print(sandbox.commands.run("echo 'Hello from Local!'").stdout)
finally:
    sandbox.terminate()
```

You can pass per-claim environment variables when creating a sandbox:

```python
sandbox = client.create_sandbox(
    warmpool="python-sandbox-warmpool",
    namespace="default",
    env={"FOO": "bar"},
)
```

Setting `env` populates `SandboxClaim.spec.env`, which forces a cold start
from the warm pool template instead of adopting a pre-warmed pod. This may
increase startup latency.

### 3. In-Cluster Mode (Direct Pod Connection)

Use this when the client runs **inside the cluster** (for example, another pod in the same cluster).
The client connects **directly to the sandbox runtime pod**, bypassing the sandbox router.

The client first uses the pod IP reported in the Sandbox status. If the pod IP is not available
(for example, before status is populated or when running against an older controller), it falls
back to the stable cluster DNS endpoint:
`http://{sandbox_id}.{namespace}.svc.cluster.local:{server_port}`.

```python
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

connection_config = SandboxInClusterConnectionConfig()

client = SandboxClient(connection_config=connection_config)

sandbox = client.create_sandbox(warmpool="python-sandbox-warmpool", namespace="default")
try:
    print(sandbox.commands.run("echo 'Hello from in-cluster!'").stdout)
finally:
    sandbox.terminate()
```

### 4. Direct URL Mode

Use `SandboxDirectConnectionConfig` to bypass discovery entirely. Useful for:

- **Internal Agents:** Running inside the cluster (e.g. router Service DNS).
- **Custom Domains:** Connecting via HTTPS (e.g., `https://sandbox.example.com`).

```python
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig

client = SandboxClient(
    connection_config=SandboxDirectConnectionConfig(
       api_url="http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080"
    )
)

sandbox = client.create_sandbox(warmpool="python-sandbox-warmpool", namespace="default")
try:
    sandbox.commands.run("ls -la")
finally:
    sandbox.terminate()
```

### 5. Custom Ports

If your sandbox runtime listens on a port other than 8888 (e.g., a Node.js app on 3000), specify `server_port`.

```python
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxLocalTunnelConnectionConfig

client = SandboxClient(
    connection_config=SandboxLocalTunnelConnectionConfig(server_port=3000)
)

sandbox = client.create_sandbox(warmpool="node-sandbox-warmpool", namespace="default")
```

### File Operations

`read()` remains convenient for small files and returns the complete contents as
`bytes`. Use `read_to()` for large files so the response is copied incrementally
into a caller-owned binary destination:

```python
with open("artifact-copy.tar", "wb") as destination:
    written = sandbox.files.read_to(
        "artifact.tar",
        destination,
        max_bytes=512 * 1024 * 1024,
    )

print(f"downloaded {written} bytes")
```

`read_to()` never closes the destination. It always closes the HTTP response,
including after a size-limit violation or destination error. If an error occurs,
data already written remains in the destination. Omitting `max_bytes` disables
the optional per-call download limit.

`AsyncFilesystem.read_to()` provides the same behavior for an asynchronous sink
whose `write(bytes)` method is awaitable and returns the number of bytes accepted:

```python
written = await sandbox.files.read_to(
    "artifact.tar",
    async_destination,
    max_bytes=512 * 1024 * 1024,
)
```

### 6. Async Client

For async applications (FastAPI, aiohttp, async agent orchestrators), use the `AsyncSandboxClient`.
Install the async extras first:

```bash
pip install k8s-agent-sandbox[async]
```

The async client requires an explicit connection config — `SandboxLocalTunnelConnectionConfig`
is not supported because it relies on a synchronous `kubectl port-forward` subprocess. Use
`SandboxGatewayConnectionConfig`, `SandboxDirectConnectionConfig`,
`SandboxInClusterConnectionConfig`, or `SandboxdPodTunnelConnectionConfig`. For the portable
`sandboxd` runtime, install
both optional extras: `pip install 'k8s-agent-sandbox[async,grpc]'`.

**Direct connection (explicit URL, e.g. router service):**

```python
import asyncio
from k8s_agent_sandbox import AsyncSandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig

async def main():
    config = SandboxDirectConnectionConfig(
        api_url="http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080"
    )

    async with AsyncSandboxClient(connection_config=config) as client:
        sandbox = await client.create_sandbox(
            warmpool="python-sandbox-warmpool",
            namespace="default",
        )
        result = await sandbox.commands.run("echo 'Hello from async!'")
        print(result.stdout)

asyncio.run(main())
```

**In-cluster (direct to sandbox pod; default: cluster DNS):**

```python
import asyncio
from k8s_agent_sandbox import AsyncSandboxClient
from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

async def main():
    config = SandboxInClusterConnectionConfig()  # default: cluster DNS

    async with AsyncSandboxClient(connection_config=config) as client:
        sandbox = await client.create_sandbox(
            warmpool="python-sandbox-warmpool",
            namespace="default",
        )
        result = await sandbox.commands.run("echo 'Hello from async!'")
        print(result.stdout)

asyncio.run(main())
```

**sandboxd runtime (direct pod tunnel):**

`SandboxdPodTunnelConnectionConfig` forwards sandboxd's REST filesystem port and gRPC
process port directly from the sandbox Pod. The async client establishes and tears down
both forwards without blocking the event loop.

```python
import asyncio
from k8s_agent_sandbox import AsyncSandboxClient
from k8s_agent_sandbox.models import SandboxdPodTunnelConnectionConfig

async def main():
    config = SandboxdPodTunnelConnectionConfig()
    async with AsyncSandboxClient(connection_config=config) as client:
        sandbox = await client.create_sandbox(
            warmpool="sandboxd-warmpool",
            namespace="default",
        )
        result = await sandbox.commands.run("echo 'Hello from sandboxd'")
        await sandbox.files.write("hello.txt", result.stdout)

asyncio.run(main())
```

### 7. Labels and Pod Metadata

`create_sandbox` lets you attach metadata at two different levels:

- `labels`: Kubernetes labels on the **SandboxClaim object** itself
  (`SandboxClaim.metadata.labels`). Useful for selecting/listing claims.
- `pod_labels` / `pod_annotations`: labels and annotations stamped onto the
  running Sandbox **Pod** via `spec.additionalPodMetadata`. Because they live on
  the Pod, the workload can read them from inside the sandbox through the
  [Downward API](https://kubernetes.io/docs/concepts/workloads/pods/downward-api/)
  (for example, to stamp a tenant or client identifier and reject requests that
  don't belong to it).

```python
sandbox = client.create_sandbox(
    warmpool="python-sandbox-warmpool",
    namespace="default",
    labels={"team": "platform"},            # on the SandboxClaim object
    pod_labels={"client-id": "tenant-a"},   # on the running Pod
    pod_annotations={"owner": "tenant-a"},  # on the running Pod
)
```

`pod_labels` are validated with the same Kubernetes label rules as `labels`. The
same parameters are available on `AsyncSandboxClient.create_sandbox`.

Behavioral notes:

- A `pod_label` / `pod_annotation` whose key already exists on the warmpool
  template with a different value is rejected by the controller's "No
  Overrides" rule, and the reconcile errors.
- Client-side validation only checks RFC-1123 label syntax. The controller's
  domain allow-list and system-label restrictions are enforced server-side and
  are not replicated client-side.

### 8. Custom Volume Claim Templates

You can dynamically request persistent volumes to be attached to your Sandbox Pod by specifying `volume_claim_templates`. This allows the sandbox to mount custom PersistentVolumeClaims (PVCs).

```python
sandbox = client.create_sandbox(
    warmpool="python-sandbox-warmpool",
    namespace="default",
    volume_claim_templates=[
        {
            "metadata": {
                "name": "my-volume",
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {
                        "storage": "1Gi",
                    },
                },
            },
        }
    ],
)
```

The volume claim templates are validated against the warmpool template's policy and rules (e.g., whether custom volume claims are allowed or if overrides are permitted).

### 9. Startup Latency: How the SDK Waits for Readiness

`create_sandbox()` is fully **watch-based** — it never polls the Kubernetes
API on an interval, so there is no poll-interval latency added on top of the
controller's own claim-to-Ready time.

The wait is a **single watch on the SandboxClaim**. The claim controller
publishes the bound sandbox name (`status.sandbox.name`), the pod IPs
(`status.sandbox.podIPs`) and the forwarded `Ready` condition in one status
update when it adopts a warm-pool sandbox, so the first watch event that
carries the sandbox name normally also carries `Ready=True` and
`create_sandbox()` returns immediately. On a cold start (no warm sandbox
available, or `env`/`volume_claim_templates` set, which force cold starts)
the same watch simply keeps streaming claim updates until the forwarded
`Ready` condition flips to `True`.

Latency guidance:

- **Do not poll** `Sandbox`/`SandboxClaim` objects with `get_*` calls in a
  loop to detect readiness; a poll interval of `T` adds an average of `T/2`
  (uniformly distributed 0..`T`) on top of the controller latency. Use
  `create_sandbox()` / the claim `Ready` condition watch.
- `sandbox_ready_timeout` (default 180s) bounds the whole wait; the watch
  returns as soon as the claim is Ready, the timeout only caps the worst case.
- The Kubernetes client reuses a single authenticated connection pool for
  the watch, so no extra TLS handshakes occur on the ready path.
- With the local-tunnel connection mode, the first request additionally pays
  for the `kubectl port-forward` startup; the SDK probes the local port every
  50ms while it comes up. Gateway/in-cluster modes do not have this step.

### 10. Batch claims

A batch is a set of `SandboxClaims` sharing an `agents.x-k8s.io/batch-id` label, spread across one or
more warmpools in one namespace, with a `coordination.k8s.io/v1` Lease (named `batch-<id>`) that the
SDK renews while the driver is alive. Each warmpool's share is a `BatchGroup(warmpool, size, min_ready)`;
`min_ready` defaults to `size`.

`SandboxClient.claim_batch()` creates a batch and returns a `SandboxBatch` handle (`AsyncSandboxClient.claim_batch()`
returns an `AsyncSandboxBatch`). It checks that each group's `SandboxWarmPool` and its `SandboxTemplate` exist,
creates the Lease, starts one label-scoped watch, and then creates the claims `<id>-0` to `<id>-<N-1>` in the
background, paced by `create_rps` (default 50 per second) and `max_in_flight` (default 20). It returns without
waiting for the claims. Each claim gets a `shutdownTime` of its own create time plus
`quorum_timeout + work_budget + 600s`, so the controller deletes it even if the driver never cleans up.

Consume the members in one of two ways, and always `release()` the batch when done:

```python
from k8s_agent_sandbox import BatchEventType, BatchGroup, SandboxClient

client = SandboxClient()

# Stream: use each sandbox the moment it is Ready.
batch = client.claim_batch([BatchGroup(warmpool="python-sandbox-pool", size=3)], work_budget=1800)
try:
    for event in batch.events():  # ends once no member can still arrive
        if event.type is BatchEventType.MEMBER_READY:
            batch.connect(event.member).commands.run("echo hello")
        # MEMBER_FAILED / MEMBER_LOST: one fewer sandbox; LEASE_DEGRADED: renewals are failing
finally:
    batch.release()

# Per group: start each group's cohort as soon as its own min_ready is met.
batch = client.claim_batch([
    BatchGroup(warmpool="pool-a", size=10, min_ready=8),
    BatchGroup(warmpool="pool-b", size=4, min_ready=3),
])
try:
    groups = batch.iter_ready_groups()  # call this before calling events()
    for group in groups:
        if group.error is not None:     # QuorumUnreachableError, or TimeoutError after quorum_timeout
            continue
        for member in group.members:    # exactly min_ready members, lowest ordinals first
            batch.connect(member).commands.run("echo hello")
    for event in batch.events():        # the members beyond each group's min_ready
        ...
finally:
    batch.release()
```

- `events()` streams `MEMBER_READY`, `MEMBER_FAILED` (a terminal claim or a failed create), and `MEMBER_LOST`
  for the claims `claim_batch` created, plus `LEASE_DEGRADED` once per episode of failing Lease renewals. It
  ends once every member is Ready, failed, lost, or released, or once `quorum_timeout` (default 600s) passes,
  whichever is first. Call it once and keep the iterator (`stream = batch.events()`) to stop and resume
  reading; a second `events()` call raises `BatchError`.
- `iter_ready_groups()` yields one `GroupReady` per group, as soon as that group has `min_ready` Ready members,
  can no longer reach `min_ready` (`error=QuorumUnreachableError`), or runs out of `quorum_timeout`
  (`error=TimeoutError`). One group never waits on another.
- **Call `iter_ready_groups()` before calling `events()`.** The first of the two to be called decides how
  the handle hands out members, so `stream = batch.events()` fixes the mode even before you iterate it. If `iter_ready_groups()` comes first, `events()` holds back each group's first
  `min_ready` Ready members for it and streams only the rest (and a group's held-back members, if the group
  yields an error). If `events()` comes first, the handle is stream-only and `iter_ready_groups()` raises
  `BatchError`.
- Each handle hands a member out at most once across `events()` and `iter_ready_groups()`. That guarantee is
  per handle: after a driver crash, a new handle from `get_batch()` may hand out members the old one already did.
- Once `iter_ready_groups()` has been called, a group whose creates fail too often for it to reach `min_ready`
  has its remaining creates cancelled, and `iter_ready_groups()` yields `QuorumUnreachableError` for it. Other
  groups keep filling; the batch is never released automatically. A stream-only batch keeps creating after a
  failed create, since `min_ready` means nothing to its consumer.
- `release()` stops creation, the watch, and renewal, deletes every claim with the batch label via
  `deletecollection` (re-listing until only terminating claims remain), then deletes the Lease. It is
  idempotent. If a deletion fails, for example with a 403 because the Role below lacks `deletecollection`,
  the error propagates and the Lease stays so a later `release()` can retry. `SandboxClient.delete_all()`
  (and so `cleanup=True`), `AsyncSandboxClient.delete_all()`, and `async with AsyncSandboxClient(...)` release
  the batches the client tracks; the async client's atexit hook releases them too.

`claim_batch()` raises `ValueError` for invalid arguments, `SandboxWarmPoolNotFoundError` or
`SandboxTemplateNotFoundError` for a missing dependency, and `BatchExistsError` if the batch's Lease
already exists, all before any claim is created. `work_budget`, `quorum_timeout`, and `lease_duration`
are whole seconds (`int`).

`SandboxClient.get_batch()` attaches to an existing batch, for example from another process after a crash:

```python
batch = client.get_batch("b1234abcd12", namespace="default")

ready = [m for m in batch.members() if m.ready]
for member in ready:
    sandbox = batch.connect(member)
    sandbox.commands.run("echo hello")

batch.detach()
```

`get_batch` takes over the Lease and renews it in the background. It raises `BatchLeaseExpiredError`
if the Lease is missing or stale, `BatchInUseError` if another process holds it live, and `BatchNotFoundError`
if there's no Lease and no labeled claims.

`SandboxBatch` (and its async twin `AsyncSandboxBatch`) expose:

- `batch_id`, `namespace`, `groups`, `size`: the batch's identity and its per-warmpool `BatchGroup`s.
- `members(warmpool=None)`: a snapshot of every `Member`, sorted by ordinal.
- `connect(member)`: a connected `Sandbox`/`AsyncSandbox` for a ready member.
- `events()`, `iter_ready_groups()`: stream or per-group consumption, as above.
- `err()`: the error that stopped the background watch/renewal, or `None`.
- `release()`: deletes the batch's claims and its Lease; idempotent.
- `detach(grace=None)`: stops the background tasks and releases the Lease so another `get_batch` can take over; idempotent.

#### RBAC

A batch driver needs, in the batch's namespace:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: sandbox-batch-driver
rules:
- apiGroups: ["extensions.agents.x-k8s.io"]
  resources: ["sandboxclaims"]
  verbs: ["create", "get", "list", "watch", "delete", "deletecollection"]
- apiGroups: ["coordination.k8s.io"]
  resources: ["leases"]
  verbs: ["create", "get", "update", "delete"]
- apiGroups: ["extensions.agents.x-k8s.io"]
  resources: ["sandboxwarmpools", "sandboxtemplates"]
  verbs: ["get"]  # claim_batch() checks each group's warmpool and template before creating anything
```

The two `get` verbs on `sandboxwarmpools` and `sandboxtemplates` are what let `claim_batch()` check that each
group's warm pool and template exist before it creates anything. Without them, `claim_batch()` logs a warning
and skips that check; a missing dependency then surfaces as the group's `quorum_timeout`.

## Testing

A test script is included to verify the full lifecycle (Creation -> Execution -> File I/O -> Cleanup).

### Run in Tunnel Mode:

```bash
python test_client.py --namespace default
```

### Run in Gateway Mode:

```bash
python test_client.py --gateway-name external-http-gateway
```
