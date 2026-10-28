# Final issue texts with verified reproduction scripts

This document contains five issues formatted for the repository's bug-report template. Replace placeholders such as `#<issue A>` with the real issue numbers once filed. Each section ends with an internal "Verification" block that records the exact command and output on upstream main and, for Issues A, B, and 4, on the corresponding fix branch. Omit the "Verification" block when pasting into GitHub.

Every run below shares the same test environment:
- Each baseline run installs `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` via `setuptools_scm` after release tag `1.0.8` from upstream main `c4ed9ba962270bf2c34d868e8c92048e85038899`, dated 2026-10-27.
- The environment runs Python 3.13.15 on Linux with httpx 0.28.1, requests 2.34.2, kubernetes 37.0.1, and kubernetes_asyncio 33.3.0.
- Each tree uses a fresh virtual environment created with `python -m venv venv && venv/bin/pip install -e "<tree>/clients/python/agentic-sandbox-client[async]"`.
- None of the reproduction scripts require a cluster, because each script stubs only the Kubernetes helper methods exercised on its path while calling the public SDK surface.

Fix branches:
- PR 1 `origin/fix/python-sdk-release-terminated-handles` at `9feeb8a9` is a prose-only amend of `0a54a028` on base `7f40dc1a` and fixes Issue A. Verification ran against `0a54a028` with version `1.0.9.dev38+g0a54a0288`.
- PR 2 `origin/fix/python-sdk-free-closed-connectors` at `fbbbb944` is a prose-only amend of `e62610f9` on base `c4ed9ba9` and fixes Issue B. Verification ran against `e62610f9` with version `1.0.9.dev41+ge62610f95`.
- PR 3 `origin/fix/python-sdk-release-client-exit-hook` at `368cedb6` is a prose-only amend of `afae285e` stacked on PR 1 `9feeb8a9` and fixes Issue 4. Verification ran against `afae285e` with version `1.0.9.dev39+gafae285ed`.

---

## Issue A

**Title**

Python SDK: client retains every terminated sandbox handle, leaking memory over the client's lifetime

**What happened?**

When a caller using a long-lived `AsyncSandboxClient` repeatedly calls `create_sandbox()`, executes a command on the handle, and calls `sandbox.terminate()`, process memory grows on every cycle and never returns to baseline while the client remains open.

`SandboxClient` and `AsyncSandboxClient` record every handle they create or attach in the internal `_active_connection_sandboxes` registry. `Sandbox.terminate()` and `AsyncSandbox.terminate()` close the local connection and delete the Kubernetes `SandboxClaim`, yet leave the handle in `_active_connection_sandboxes`. Consequently, the client retains one closed handle for every sandbox terminated through `sandbox.terminate()` until the caller closes or discards the client itself. Each retained `AsyncSandbox` holds its closed `AsyncSandboxConnector`, whose closed `httpx.AsyncClient` retains its `ssl.SSLContext` and loaded CA bundle.

`terminate()` should remove the terminated handle from the client's registry once claim deletion succeeds, matching `client.delete_sandbox()`.

This defect causes **unbounded memory growth** proportional to the cumulative number of sandboxes terminated over the client's lifetime.

- The cluster-free reproduction script below measures 0.69 MiB of peak RSS growth per create-and-terminate cycle on the async client and confirms that the client still retains all 200 closed handles after 200 cycles.
- A kind-cluster benchmark with Python 3.12.15, glibc 2.41, and 40 concurrent workers that each create a sandbox, run one command, and call `terminate()` confirms the same growth under load. Detailed traces live in `test/benchmarks/batch-claim/results/memprobe/` and `results/g4/` on the `bench/async-mem-probe` branch.
  - Across 2,400 cycles, RSS rose continuously from 93.8 MiB to 1,752.4 MiB at a slope of 72.4 MiB per 100 cycles. At completion, glibc reported 1,660 MiB of allocated heap in use, or roughly 0.68 MiB per cycle. Constraining `MALLOC_ARENA_MAX=2` kept the slope at 94.5% of the default, confirming live object retention rather than allocator fragmentation. `memray` attributed 84% of tracked memory to `ssl.create_default_context` under `AsyncSandboxConnector.__init__`.
  - Across 600 cycles, RSS rose from 156 MiB to 525 MiB, with a second-half slope of 71.6 MiB per 100 cycles.
  - After creating 100 sandboxes and terminating all of them, the client still tracked 101 handles and retained 0.667 MiB per handle.
- A local cluster-free probe of the synchronous client lives in `test/benchmarks/batch-claim/results/leakfix/handle_retained.py` and `handle_retained.out` on the `bench/async-mem-probe` branch. On Python 3.13.15, each retained closed `Sandbox` handle holds 0.044 MiB of allocated memory, or 0.036 MiB in `tracemalloc`, because `SandboxConnector` does not allocate a per-handle SSL context.
- Code inspection also shows an unmeasured shutdown cost. `AsyncSandboxClient` exit cleanup issues one `DELETE` request per key in `_active_connection_sandboxes`, so every terminated handle still present in the registry triggers a redundant `DELETE` request at interpreter exit. The API server responds with HTTP 404, which the client treats as success.

Two workarounds exist on current `main`. Callers can invoke `client.delete_sandbox(claim_name, namespace)` instead of `sandbox.terminate()`, which removes the registry entry. Alternatively, callers can invoke `client.list_active_sandboxes()` periodically to prune inactive handles, although that workaround triggers #<issue 3> because it also drops closed handles whose remote `SandboxClaim` still exists.

See also #<issue B> and #<issue 4>. #<issue B> tracks the connector callback cycle that delays freeing unreferenced closed handles until Python's cyclic garbage collector runs. #<issue 4> tracks the `atexit` registration that retains closed clients until interpreter exit.

**How can we reproduce it (as minimally and precisely as possible)?**

1. Install the package with `pip install "k8s-agent-sandbox[async]"`, or install from a checkout of `main` with `pip install -e "clients/python/agentic-sandbox-client[async]"`.
2. Save the script below as `repro.py` and run `python repro.py`. The script requires no Kubernetes cluster because it stubs the three helper methods that create a claim, wait for readiness, and delete the claim. It inspects `_active_connection_sandboxes` read-only to report retained handles and reads Linux `ru_maxrss` in KiB.

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

The optional snippet below illustrates the structure of the real-cluster loop behind the kind benchmark figures above. Running it requires a cluster with the extension CRDs installed and a `SandboxWarmPool` named `python-sandbox-pool`. The benchmark figures above come from the benchmark harness in that directory rather than a standalone run of this illustrative snippet.

```python
import asyncio, resource
from k8s_agent_sandbox import AsyncSandboxClient
from k8s_agent_sandbox.models import SandboxGatewayConnectionConfig  # or any async-supported config

async def main(cycles=600, workers=40):
    async with AsyncSandboxClient(SandboxGatewayConnectionConfig(gateway_name="external-http-gateway")) as client:
        done = 0
        async def worker():
            nonlocal done
            for _ in range(cycles // workers):
                sandbox = await client.create_sandbox("python-sandbox-pool")
                await sandbox.commands.run("echo ok")
                await sandbox.terminate()
                done += 1
                if done % 100 == 0:
                    print(f"cycles={done} tracked={len(client._active_connection_sandboxes)} "
                          f"maxrss={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MiB")
        await asyncio.gather(*(worker() for _ in range(workers)))

asyncio.run(main())
```

With 40 workers against a warm pool of 120 replicas on `main`, RSS grew by roughly 72 MiB per 100 cycles, rising from 156 MiB to 525 MiB over 600 cycles and from 93.8 MiB to 1,752.4 MiB over 2,400 cycles.

**Version**

- upstream `main` `c4ed9ba962270bf2c34d868e8c92048e85038899`
- `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` installed from that commit, Python 3.13.15, httpx 0.28.1, Linux. The kind-cluster measurements ran under Python 3.12.15. The 2,400-cycle runs used `7fd162d9`, which shares the exact code on this path with `main` `840fdfde`, and the 600-cycle and 100-sandbox runs used `main` `7f40dc1a`.

### Verification for Issue A

```
$ venv-main/bin/python repro_a.py      # upstream main c4ed9ba9
terminated 200 sandboxes; client still holds 200 handles, 200 of them closed (expected 0)
peak RSS grew 0.69 MiB per create/terminate cycle (Linux ru_maxrss)

$ venv-pr1/bin/python repro_a.py       # PR 1, 9feeb8a9 (prose-only amend of 0a54a028)
terminated 200 sandboxes; client still holds 0 handles, 0 of them closed (expected 0)
peak RSS grew 0.25 MiB per create/terminate cycle (Linux ru_maxrss)
```

The script reproduces the defect on `main` and confirms the fix on PR 1. About 0.25 MiB per cycle remains on PR 1 while unreferenced handles await cyclic garbage collection, which Issue B in PR 2 resolves. On the kind cluster, combining all three PRs reduced the 600-cycle RSS slope from 71.6 MiB to 0.85 MiB per 100 cycles in `results/g4/README.md`.

---

## Issue B

**Title**

Python SDK: handle-to-connector reference cycle delays reclamation and raises memory peaks until cyclic garbage collection

**What happened?**

When a caller closes a sandbox handle via `terminate()`, `close_connection()`, or `client.delete_sandbox()` and drops all references to it, Python does not reclaim the handle until the cyclic garbage collector runs.

During initialization, `Sandbox` and `AsyncSandbox` pass bound methods `get_pod_ip`, `get_pod_name`, and `get_service_fqdn` to their connector, and the connector forwards them to its connection strategy. Because the handle holds the connector and the connector holds bound methods of the handle, the pair forms a reference cycle that reference counting alone cannot collect. Only Python's cyclic garbage collector can break the cycle. Because cyclic collection triggers on container object counts rather than native byte volume, the collector does not account for the roughly 0.7 MiB of native OpenSSL state held behind each closed `AsyncSandbox`. Process memory therefore climbs until a cyclic collection runs, and remains elevated indefinitely if the process stops allocating Python container objects.

Reference counting should reclaim a closed sandbox handle as soon as no references to it remain.

This defect causes **delayed reclamation and elevated memory peaks** rather than unbounded growth, and affects every closed handle regardless of whether a client still tracks it.

- The cluster-free reproduction script below demonstrates that with cyclic garbage collection disabled, a closed and unreferenced `AsyncSandbox` remains live until an explicit `gc.collect()` call reclaims it.
- A kind-cluster benchmark with Python 3.12.15 and `AsyncSandboxClient` layered on top of the #<issue A> fix isolated the cost of this cycle. Detailed traces live in `test/benchmarks/batch-claim/results/g3/` on the `bench/async-mem-probe` branch.
  - Across 600 create, command, and terminate cycles with 40 workers, second-half RSS still grew by 29.3 MiB per 100 cycles, and glibc allocated bytes peaked at 309 MiB. Breaking the callback cycle at close reduced RSS growth to 0.46 MiB per 100 cycles. With the cycle broken, glibc allocated bytes declined from 72 MiB at the start of the window to 41 MiB at completion.
  - After creating 100 sandboxes, terminating all of them, and performing no further allocations, 0.66 MiB per handle remained allocated 60 seconds later despite an empty client registry. Breaking the cycle at close reduced retained memory to 0.0001 MiB per handle.
- A local cluster-free probe in `test/benchmarks/batch-claim/results/leakfix/handle_cycle_edges.py` and `handle_cycle_edges.out` on the `bench/async-mem-probe` branch measured 0.694 MiB per closed, unreferenced `AsyncSandbox` prior to `gc.collect()` on Python 3.13.15 with cyclic collection disabled. Clearing the stored callbacks at close reduced that retained footprint to 0.006 MiB.

Releasing callbacks at close does not reclaim handles that callers drop without closing first. Unclosed handles retain their active connector until cyclic garbage collection runs, so callers should always close or terminate handles when finished with them.

See also #<issue A>, which tracks client registry retention of terminated handles and accounts for unbounded growth over a client's lifetime.

**How can we reproduce it (as minimally and precisely as possible)?**

1. Install the package with `pip install "k8s-agent-sandbox[async]"`, or install from a checkout of `main` with `pip install -e "clients/python/agentic-sandbox-client[async]"`.
2. Save the script below as `repro.py` and run `python repro.py`. The script requires no cluster because it stubs the Kubernetes helper calls. It calls `client.delete_sandbox()` so that the client registry does not retain the handle via #<issue A>, then checks a `weakref` with cyclic collection disabled to test whether reference counting reclaims the closed handle.

```python
import asyncio, gc, weakref
from k8s_agent_sandbox import AsyncSandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig

async def ok(*a, **k): return {"metadata": {"uid": "u", "resourceVersion": "1"}}
async def ready(name, *a, **k): return name

async def main():
    client = AsyncSandboxClient(SandboxDirectConnectionConfig(api_url="http://router.invalid"), cleanup=False)
    h = client.k8s_helper  # stub the three Kubernetes calls this path makes
    h.create_sandbox_claim, h.wait_for_claim_ready, h.delete_sandbox_claim = ok, ready, ok
    sandbox = await client.create_sandbox("pool")
    await client.delete_sandbox(sandbox.claim_name)  # closes it and drops the client's entry
    ref = weakref.ref(sandbox)
    gc.disable()
    del sandbox
    print(f"closed handle with no references, before gc.collect(): alive={ref() is not None} (expected False)")
    gc.collect()
    print(f"after gc.collect(): alive={ref() is not None}")

asyncio.run(main())
```

Output on `main`:

```
closed handle with no references, before gc.collect(): alive=True (expected False)
after gc.collect(): alive=False
```

**Version**

- upstream `main` `c4ed9ba962270bf2c34d868e8c92048e85038899`
- `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` installed from that commit, Python 3.13.15, httpx 0.28.1, Linux.

### Verification for Issue B

```
$ venv-main/bin/python repro_b.py      # upstream main c4ed9ba9
closed handle with no references, before gc.collect(): alive=True (expected False)
after gc.collect(): alive=False

$ venv-pr2/bin/python repro_b.py       # PR 2, fbbbb944 (prose-only amend of e62610f9)
closed handle with no references, before gc.collect(): alive=False (expected False)
after gc.collect(): alive=False
```

The script reproduces the defect on `main` and confirms the fix on PR 2.

---

## Issue 3

**Title**

Python SDK: list_active_sandboxes() evicts locally closed handles, leaking live SandboxClaims on cleanup

**What happened?**

When a caller closes a handle's local connection with `sandbox.close_connection()`, calls `client.list_active_sandboxes()`, and then calls `client.delete_all()`, `client.delete_all()` fails to delete the existing Kubernetes `SandboxClaim`.

In both `sandbox_client.py` and `async_sandbox_client.py`, `list_active_sandboxes()` pops every tracked handle where `obj.is_active` is `False`. A handle becomes inactive whenever its local connection closes. That happens on `close_connection()` or on a failed `get_sandbox()` lookup, where the client closes the handle locally and keeps it tracked because the `SandboxClaim` may still exist in the cluster. Once `list_active_sandboxes()` evicts the entry from `_active_connection_sandboxes`, `delete_all()`, context-manager exit, and `atexit` cleanup never delete that claim because all three iterate only over `_active_connection_sandboxes`.

`list_active_sandboxes()` should either operate without mutating tracking state or evict only handles whose `SandboxClaim` a successful `terminate()` has already deleted, preserving claim cleanup for locally closed handles.

This defect causes **leaked cluster resources**. A process that closes a handle's local connection, calls `list_active_sandboxes()`, and exits leaves the `SandboxClaim` in place and its backing sandbox pod running in the cluster. Verification used only the stubbed reproduction below rather than a live cluster, confirming that `delete_all()` never issues the `DELETE` call after `list_active_sandboxes()`.

One resolution is to evict only handles whose `SandboxClaim` a successful `terminate()` deleted while keeping locally closed handles tracked. Another resolution is to filter inactive handles out of `list_active_sandboxes()`'s return value without mutating `_active_connection_sandboxes`.

See also #<issue A>, where calling `list_active_sandboxes()` as a workaround for retained terminated handles exposes callers to this eviction bug.

**How can we reproduce it (as minimally and precisely as possible)?**

1. Install the package with `pip install k8s-agent-sandbox`, or install from a checkout of `main` with `pip install -e clients/python/agentic-sandbox-client`.
2. Save the script below as `repro.py` and run `python repro.py`. The script requires no cluster because it stubs the Kubernetes helper calls and records which claims `delete_sandbox_claim` receives. The first run establishes the baseline without `list_active_sandboxes()`, and the second run calls `list_active_sandboxes()` before `delete_all()`.

```python
from kubernetes.client import ApiClient
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig

deleted = []  # claims the client asked Kubernetes to delete

def run(list_first):
    client = SandboxClient(SandboxDirectConnectionConfig(api_url="http://router.invalid"), api_client=ApiClient())
    h = client.k8s_helper  # stub the three Kubernetes calls this path makes
    h.create_sandbox_claim = lambda *a, **k: {"metadata": {"uid": "u", "resourceVersion": "1"}}
    h.wait_for_claim_ready = lambda name, *a, **k: name
    h.delete_sandbox_claim = lambda name, *a, **k: deleted.append(name)
    sandbox = client.create_sandbox("pool")
    sandbox.close_connection()  # Closes locally. The claim still exists.
    if list_first:
        print("list_active_sandboxes() ->", client.list_active_sandboxes())
    deleted.clear()
    client.delete_all()  # the same walk the context managers and exit cleanup do
    print(f"list_active_sandboxes() called first={list_first}: delete_all() deleted {deleted or 'nothing'}")

run(list_first=False)
run(list_first=True)
```

Output on `main` with a generated claim name:

```
list_active_sandboxes() called first=False: delete_all() deleted ['sandbox-claim-3b8c52f8']
list_active_sandboxes() -> []
list_active_sandboxes() called first=True: delete_all() deleted nothing
```

**Version**

- upstream `main` `c4ed9ba962270bf2c34d868e8c92048e85038899`
- `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` installed from that commit, Python 3.13.15, Linux.

### Verification for Issue 3

```
$ venv-main/bin/python repro_3.py      # upstream main c4ed9ba9
list_active_sandboxes() called first=False: delete_all() deleted ['sandbox-claim-3b8c52f8']
list_active_sandboxes() -> []
list_active_sandboxes() called first=True: delete_all() deleted nothing
```

The script reproduces the defect on `main`. No fix branch exists yet.

---

## Issue 4

**Title**

Python SDK: atexit hook retains closed client instances, leaking memory until interpreter exit

**What happened?**

When an application instantiates a client per task, closes it with `await client.close()` or a `with` block, and drops all references to it, every client instance remains live in memory until the Python process exits.

`AsyncSandboxClient.__init__` calls `atexit.register(self._atexit_cleanup)` when `cleanup=True`, which is its default. `SandboxClient.__init__` calls `atexit.register(self._delete_automatic_sandboxes)` when `cleanup=True`, which defaults to `False` on the synchronous client. Because a bound method holds a strong reference to `self`, Python's global `atexit` callback list retains every registered client instance for the lifetime of the interpreter, even after `close()` or context-manager exit. Neither client ever unregisters its callback.

Reference counting should reclaim a closed, unreferenced client instance rather than retaining it until interpreter exit.

This defect causes **per-client memory retention** proportional to the number of clients instantiated with `cleanup=True`. Clients instantiated with `cleanup=False` register no `atexit` callback, so reference counting reclaims them once callers drop them.

- The cluster-free reproduction script below confirms that both a closed `AsyncSandboxClient` with default settings and a `SandboxClient(cleanup=True)` after its `with` block remain live after `gc.collect()`.
- A local `tracemalloc` benchmark across 200 clients per configuration on Python 3.12.15 in `test/benchmarks/batch-claim/results/g4/client_retained.py` and `client_retained.out` on the `bench/async-mem-probe` branch measured 1,369 B per unused `AsyncSandboxClient`, 9,600 B per used and closed `AsyncSandboxClient`, and 11,407 B per `SandboxClient(cleanup=True)` after `gc.collect()`. All 200 instances remained live in every case.

Unregistering the callback unconditionally inside `close()` or `__exit__` is insufficient for three reasons. `AsyncSandboxClient.close()` retains handles whose close failed so a subsequent call can retry cleanup. Callers may reuse a client instance after `close()`. Synchronous `SandboxClient.__exit__` likewise leaves the client reusable after the `with` block. Exit cleanup must continue covering all three cases as well as clients dropped without `close()`. Registering a static cleanup function bound via `functools.partial` to only the cleanup state avoids holding `self`, matching `urllib3`'s `weakref.finalize(self, _close_pool_connections, pool)` pattern. That state consists of the `_active_connection_sandboxes` registry dict, the `_explicit_claims` set, and, for async, the injected `ApiClient`.

See also #<issue A>.

**How can we reproduce it (as minimally and precisely as possible)?**

1. Install the package with `pip install "k8s-agent-sandbox[async]"`, or install from a checkout of `main` with `pip install -e "clients/python/agentic-sandbox-client[async]"`.
2. Save the script below as `repro.py` and run `python repro.py`. The script requires no cluster, makes no network calls, and uses `weakref` to test whether Python reclaims each closed client after `gc.collect()`.

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

- upstream `main` `c4ed9ba962270bf2c34d868e8c92048e85038899`
- `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` installed from that commit, Python 3.13.15, Linux.

### Verification for Issue 4

```
$ venv-main/bin/python repro_4.py      # upstream main c4ed9ba9
closed AsyncSandboxClient alive after gc.collect(): True (expected False)
closed SandboxClient(cleanup=True) alive after gc.collect(): True (expected False)

$ venv-pr3/bin/python repro_4.py       # PR 3, 368cedb6 (prose-only amend of afae285e, on PR 1 9feeb8a9)
closed AsyncSandboxClient alive after gc.collect(): False (expected False)
closed SandboxClient(cleanup=True) alive after gc.collect(): False (expected False)
```

The script reproduces the defect on `main` and confirms the fix on PR 3.

---

## Issue 5

**Title**

Python SDK: SandboxConnector.close() acts as a reset instead of a terminal close, allowing silent reconnects after close_connection()

**What happened?**

Sending a request through a sandbox connector after calling `close_connection()` produces opposite behavior in the synchronous and asynchronous clients. `SandboxConnector` silently reconnects and completes the request, whereas `AsyncSandboxConnector` raises `RuntimeError: sandbox connector is closed`.

In the synchronous client, `SandboxConnector.close()` serves two distinct roles under one method name.
- Internal recovery paths call `close()` to reset a live transport so the next request reconnects. `SandboxConnector.send_request()` calls `self.close()` on port-forward and connection errors. Connection strategies call their own `close()` before restarting a port-forward. The GKE snapshot extension calls `connector.close()` on a live handle during `suspend()` and `_restore_internal()`.
- Handle teardown paths `Sandbox.close_connection()` and `Sandbox.terminate()` call the same `close()` method as a final close.

By contrast, `AsyncSandboxConnector.close()` marks the connector permanently closed and rejects subsequent operations. This divergence stems from the underlying HTTP libraries rather than an intentional SDK contract, because `requests.Session` permits new requests after `close()` whereas `httpx` clients raise `RuntimeError` after `close()`.

Both clients should enforce the same terminal close contract and reject operations attempted after final handle teardown with a clear error.

This divergence creates **inconsistent lifecycle semantics** between the synchronous and asynchronous SDKs. Callers that retain `sandbox.connector` or a `commands` or `files` reference obtained before `close_connection()` or `terminate()` observe mode-dependent behavior in the synchronous SDK instead of a deterministic error. Depending on the connection mode, the connector reuses the pod IP, starts a new port-forward, or reconnects. Code inspection also indicates an unmeasured resource leak on `main`, where reconnecting through a handle that the client has already closed and untracked opens a new port-forward subprocess or HTTP session that nothing subsequently closes.

A clean resolution is to separate an internal transport-reset method from a terminal `close()` on `SandboxConnector` and its strategies, switch error-recovery and GKE snapshot suspend/resume callers to the internal reset, and make `SandboxConnector.close()` reject subsequent use just as `AsyncSandboxConnector.close()` does.

See also #1785, which tracks synchronous and asynchronous divergences across the Python SDK. The fix for #<issue B> clears handle callbacks on `Sandbox.close_connection()`, which alters post-close behavior in several modes without making `SandboxConnector.close()` terminal.

**How can we reproduce it (as minimally and precisely as possible)?**

1. Install the package with `pip install "k8s-agent-sandbox[async]"`, or install from a checkout of `main` with `pip install -e "clients/python/agentic-sandbox-client[async]"`.
2. Save the script below as `repro.py` and run `python repro.py`. The script requires no cluster because a local HTTP server stands in for the sandbox router and stubs replace the Kubernetes helper calls. `sandbox.connector.send_request()` exercises the same transport entrypoint used by `sandbox.commands` and `sandbox.files`.

```python
import asyncio, http.server, threading
from kubernetes.client import ApiClient
from k8s_agent_sandbox import AsyncSandboxClient, SandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig

class OK(http.server.BaseHTTPRequestHandler):  # stands in for the sandbox router
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
    def log_message(self, *a): pass

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OK)
threading.Thread(target=server.serve_forever, daemon=True).start()
config = SandboxDirectConnectionConfig(api_url=f"http://127.0.0.1:{server.server_port}")

async def async_ok(*a, **k): return {"metadata": {"uid": "u", "resourceVersion": "1"}}
async def async_ready(name, *a, **k): return name
async def async_none(*a, **k): return None

def stub_sync(h):  # stub the Kubernetes calls this path makes
    h.create_sandbox_claim = lambda *a, **k: {"metadata": {"uid": "u", "resourceVersion": "1"}}
    h.wait_for_claim_ready = lambda name, *a, **k: name
    h.get_sandbox = lambda *a, **k: None

client = SandboxClient(config, api_client=ApiClient())
stub_sync(client.k8s_helper)
sandbox = client.create_sandbox("pool")
sandbox.close_connection()
try:
    print("sync  connector after close_connection():", sandbox.connector.send_request("GET", "healthz").status_code)
except Exception as e:
    print("sync  connector after close_connection():", type(e).__name__, e)

async def main():
    client = AsyncSandboxClient(config, cleanup=False)
    h = client.k8s_helper
    h.create_sandbox_claim, h.wait_for_claim_ready, h.get_sandbox = async_ok, async_ready, async_none
    sandbox = await client.create_sandbox("pool")
    await sandbox.close_connection()
    try:
        print("async connector after close_connection():", (await sandbox.connector.send_request("GET", "healthz")).status_code)
    except Exception as e:
        print("async connector after close_connection():", type(e).__name__, e)

asyncio.run(main())
```

Output on `main`:

```
sync  connector after close_connection(): 200
async connector after close_connection(): RuntimeError sandbox connector is closed
```

**Version**

- upstream `main` `c4ed9ba962270bf2c34d868e8c92048e85038899`
- `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` installed from that commit, Python 3.13.15, requests 2.34.2, httpx 0.28.1, Linux.

### Verification for Issue 5

```
$ venv-main/bin/python repro_5.py      # upstream main c4ed9ba9
sync  connector after close_connection(): 200
async connector after close_connection(): RuntimeError sandbox connector is closed
```

The script reproduces the defect on `main`. No fix branch exists yet.

---

## Verification summary

| issue | reproduces on main `c4ed9ba9` | fixed on branch |
| --- | --- | --- |
| A | yes, 200 of 200 terminated handles retained, 0.69 MiB peak RSS per cycle | yes, PR 1 `9feeb8a9`, prose-only amend of `0a54a028`, retains 0 handles and leaves about 0.25 MiB per cycle until cyclic collection runs as tracked in Issue B |
| B | yes, closed, unreferenced handle alive until `gc.collect()` | yes, PR 2 `fbbbb944`, prose-only amend of `e62610f9`, reclaimed without cyclic collection |
| 3 | yes, `delete_all()` deletes nothing after `list_active_sandboxes()` | no fix branch |
| 4 | yes, closed async client and sync `cleanup=True` client both alive | yes, PR 3 `368cedb6`, prose-only amend of `afae285e` on PR 1 `9feeb8a9`, both reclaimed |
| 5 | yes, sync returns 200 after close, async raises | no fix branch |

Each script ran as `python repro_<x>.py` in its own virtual environment with the SDK installed editable from a detached worktree of the named commit. Every run produced empty stderr.
