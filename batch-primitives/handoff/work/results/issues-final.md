# Upstream Issues 1–3 and PRs 1–3

---

## Issue 1 (#2082)

**Title**

Python SDK: client retains every terminated sandbox handle, leaking memory over the client's lifetime

**What happened?**

`SandboxClient` and `AsyncSandboxClient` store every handle from `create_sandbox()` and `get_sandbox()` in `_active_connection_sandboxes`. Calling `sandbox.terminate()` closes the local connection and deletes the `SandboxClaim`, yet leaves the handle in `_active_connection_sandboxes`. A long-lived client therefore retains every handle terminated via `sandbox.terminate()` until the client itself closes.

On `AsyncSandboxClient`, each retained handle holds its closed `AsyncSandboxConnector` and `httpx.AsyncClient`, including its `ssl.SSLContext` and loaded CA bundle, retaining about 0.7 MiB per terminated sandbox. Across 2,400 concurrent create, command, and `terminate()` cycles on a `kind` cluster, process RSS grew linearly from 93.8 MiB to 1,752.4 MiB.

`terminate()` should remove the handle from `_active_connection_sandboxes` once `SandboxClaim` deletion succeeds, matching `client.delete_sandbox()`.

**How can we reproduce it (as minimally and precisely as possible)?**

Run the script below on Linux with `k8s-agent-sandbox[async]` installed:

```python
import asyncio, gc, resource
from k8s_agent_sandbox import AsyncSandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig

async def ok(*a, **k): return {"metadata": {"uid": "u", "resourceVersion": "1"}}
async def ready(name, *a, **k): return name

async def main(n=200):
    client = AsyncSandboxClient(SandboxDirectConnectionConfig(api_url="http://router.invalid"), cleanup=False)
    h = client.k8s_helper  # stub the three Kubernetes calls this path makes
    h.create_sandbox_claim, h.wait_for_claim_ready, h.delete_sandbox_claim = ok, ready, ok
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for _ in range(n):
        sandbox = await client.create_sandbox("pool")
        await sandbox.terminate()
    del sandbox
    gc.collect()
    held = list(client._active_connection_sandboxes.values())  # private attribute, read only
    print(f"terminated {n} sandboxes; client still holds {len(held)} handles, "
          f"{sum(not s.is_active for s in held)} of them closed (expected 0)")
    print(f"peak RSS grew {(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss0) / 1024 / n:.2f} MiB per create/terminate cycle (Linux ru_maxrss)")

asyncio.run(main())
```

Output on `main`:

```
terminated 200 sandboxes; client still holds 200 handles, 200 of them closed (expected 0)
peak RSS grew 0.69 MiB per create/terminate cycle (Linux ru_maxrss)
```

**Version**

c4ed9ba962270bf2c34d868e8c92048e85038899

---

## Issue 2

**Title**

Python SDK: handle-to-connector reference cycle delays reclamation and raises memory peaks until cyclic garbage collection

**What happened?**

`Sandbox` and `AsyncSandbox` pass their bound methods `get_pod_ip`, `get_pod_name`, and `get_service_fqdn` to their connector and connection strategy during initialization. Because the handle references its connector and the connector references bound methods of the handle, every handle forms a reference cycle that reference counting alone cannot collect after `close_connection()`, `terminate()`, or `client.delete_sandbox()`.

Only Python's cyclic garbage collector can break the cycle, and its collection thresholds track container object counts rather than native memory such as the roughly 0.7 MiB of OpenSSL state behind each closed `AsyncSandbox`. Process memory therefore climbs between collections and stays elevated indefinitely if Python container allocations stop.

Closing a handle should release the callbacks stored on its connector and strategy so reference counting reclaims the closed handle as soon as all external references drop.

**How can we reproduce it (as minimally and precisely as possible)?**

Run the script below with `k8s-agent-sandbox[async]` installed:

```python
import asyncio, gc, weakref
from k8s_agent_sandbox.async_sandbox import AsyncSandbox
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig

async def main():
    sandbox = AsyncSandbox("claim", "sbx", connection_config=SandboxDirectConnectionConfig(api_url="http://router.invalid"))
    await sandbox.close_connection()
    ref = weakref.ref(sandbox)
    gc.disable()
    del sandbox
    print("alive before gc.collect():", ref() is not None)
    gc.collect()
    print("alive after gc.collect():", ref() is not None)

asyncio.run(main())
```

Output on `main`:

```
alive before gc.collect(): True
alive after gc.collect(): False
```

**Version**

c4ed9ba962270bf2c34d868e8c92048e85038899

---

## Issue 3

**Title**

Python SDK: atexit hook retains closed client instances, leaking memory until interpreter exit

**What happened?**

`AsyncSandboxClient.__init__` calls `atexit.register(self._atexit_cleanup)` when `cleanup=True`, which is its default, and `SandboxClient.__init__` calls `atexit.register(self._delete_automatic_sandboxes)` when `cleanup=True`. Because a bound method holds a strong reference to `self`, Python's global `atexit` registry retains every registered client instance until interpreter exit, even after `await client.close()` or exiting a `with` block.

Applications that instantiate a client per task or request retain every client instance for the lifetime of the process. Across 200 closed clients after `gc.collect()`, `tracemalloc` reports all 200 instances still live, retaining 1,369 B per unused `AsyncSandboxClient`, 9,600 B per used `AsyncSandboxClient`, and 11,407 B per `SandboxClient` with `cleanup=True`.

Exit cleanup should bind only the state needed to clean up remaining sandboxes rather than `self`, so reference counting reclaims closed client instances before interpreter exit.

**How can we reproduce it (as minimally and precisely as possible)?**

Run the script below with `k8s-agent-sandbox[async]` installed:

```python
import asyncio, gc, weakref
from kubernetes.client import ApiClient
from k8s_agent_sandbox import AsyncSandboxClient, SandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig

config = SandboxDirectConnectionConfig(api_url="http://router.invalid")

async_client = AsyncSandboxClient(config)  # cleanup=True is the default
asyncio.run(async_client.close())
async_ref = weakref.ref(async_client)

with SandboxClient(config, cleanup=True, api_client=ApiClient()) as sync_client:
    pass
sync_ref = weakref.ref(sync_client)

del async_client, sync_client
gc.collect()
print(f"closed AsyncSandboxClient alive after gc.collect(): {async_ref() is not None} (expected False)")
print(f"closed SandboxClient(cleanup=True) alive after gc.collect(): {sync_ref() is not None} (expected False)")
```

Output on `main`:

```
closed AsyncSandboxClient alive after gc.collect(): True (expected False)
closed SandboxClient(cleanup=True) alive after gc.collect(): True (expected False)
```

**Version**

c4ed9ba962270bf2c34d868e8c92048e85038899

---

# Paste-Ready PR Titles and Descriptions

## PR 1 (`fix/python-sdk-release-terminated-handles`)

**Title**

`fix(python-sdk): untrack terminated sandbox handles from client registry`

**Body**

````markdown
#### What this PR does / why we need it:

`SandboxClient` and `AsyncSandboxClient` store every handle they create or attach in `_active_connection_sandboxes`, and `sandbox.terminate()` previously left the handle in that registry after deleting the `SandboxClaim`. Because each closed `AsyncSandbox` retains roughly 0.7 MiB of `httpx` and OpenSSL state, long-lived clients grew in memory with every `terminate()` call.

This PR wires an identity-checked `_on_terminated` callback from both clients onto tracked handles using a static `_untrack` helper bound via `functools.partial` to `_active_connection_sandboxes`:

- `terminate()` invokes `_on_terminated()` after `delete_sandbox_claim` succeeds and before clearing `claim_name`, removing the registry entry only if it still points to that same handle instance.
- Failed `terminate()` calls and locally closed handles keep their registry entries so `delete_all()`, context-manager exit, and `atexit` cleanup still clean up live claims.
- Binding the registry dict rather than a bound client method ensures tracked handles do not keep a dropped client alive.

#### Which issue(s) this PR is related to:

Fixes #2082

#### Release Note
<!--
If this PR requires a release note (for features, fixes, or breaking changes), please add it below.
If no release note is needed, you can leave this block empty or write "NONE".

Note: Automated release notes are generated by Gemini using the PR description in the first section.
Please ensure that description is clear and comprehensive.
For breaking changes, ensure you describe the required actions clearly and the PR is labeled with `release-note-action-required`.
-->
```release-note
The Python SDK now removes terminated sandbox handles from `SandboxClient` and `AsyncSandboxClient` registries so long-lived clients do not retain closed handles in memory.
```
````

---

## PR 2 (`fix/python-sdk-free-closed-connectors`)

**Title**

`fix(python-sdk): break handle-to-connector reference cycle on close`

**Body**

````markdown
#### What this PR does / why we need it:

`Sandbox` and `AsyncSandbox` pass their bound methods `get_pod_ip`, `get_pod_name`, and `get_service_fqdn` to their connector and connection strategy, creating a reference cycle between the handle and its connector. After a handle closes and callers drop all references to it, reference counting cannot reclaim it, leaving native `httpx` and OpenSSL state allocated until Python's cyclic garbage collector runs.

This PR adds `_release_sandbox_callbacks()` on `SandboxConnector`, `AsyncSandboxConnector`, and their connection strategies to clear stored handle callbacks when a handle finishes closing:

- `AsyncSandboxConnector.close()` is terminal and calls `_release_sandbox_callbacks()` directly.
- `Sandbox.close_connection()` calls `connector._release_sandbox_callbacks()` under `_transport_lock` after `connector.close()`, preserving `SandboxConnector.close()` as a transport reset for reconnects and GKE snapshot suspend and resume.

#### Which issue(s) this PR is related to:

Fixes #<issue 2>

#### Release Note
<!--
If this PR requires a release note (for features, fixes, or breaking changes), please add it below.
If no release note is needed, you can leave this block empty or write "NONE".

Note: Automated release notes are generated by Gemini using the PR description in the first section.
Please ensure that description is clear and comprehensive.
For breaking changes, ensure you describe the required actions clearly and the PR is labeled with `release-note-action-required`.
-->
```release-note
The Python SDK now clears stored handle callbacks when a sandbox handle closes so reference counting reclaims closed handles without waiting for cyclic garbage collection.
```
````

---

## PR 3 (`fix/python-sdk-release-client-exit-hook`)

**Title**

`fix(python-sdk): avoid retaining closed clients in atexit cleanup hook`

**Body**

````markdown
#### What this PR does / why we need it:

`AsyncSandboxClient` by default and `SandboxClient` when `cleanup=True` registered bound methods with `atexit.register()`, keeping every client instance alive in Python's global `atexit` callback list until interpreter exit even after `close()` or exiting a `with` block.

This PR converts both exit-cleanup methods to `@staticmethod` helpers registered via `functools.partial` over only `_active_connection_sandboxes`, `_explicit_claims`, and, for async, the injected `ApiClient`:

- Reference counting reclaims closed and unreferenced clients immediately instead of retaining them until process exit.
- Exit cleanup behavior is unchanged for unclosed clients, clients reused after `close()`, and handles whose earlier close failed.

As #<PR 1> has not been merged yet, this branch is stacked on #<PR 1> and this PR's own diff is the top commit.

#### Which issue(s) this PR is related to:

Fixes #<issue 3>

#### Release Note
<!--
If this PR requires a release note (for features, fixes, or breaking changes), please add it below.
If no release note is needed, you can leave this block empty or write "NONE".

Note: Automated release notes are generated by Gemini using the PR description in the first section.
Please ensure that description is clear and comprehensive.
For breaking changes, ensure you describe the required actions clearly and the PR is labeled with `release-note-action-required`.
-->
```release-note
The Python SDK no longer retains closed `SandboxClient` and `AsyncSandboxClient` instances in Python's global `atexit` registry until interpreter exit.
```
````
