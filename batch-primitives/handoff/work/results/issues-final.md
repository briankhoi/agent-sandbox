# Final issue texts with verified reproduction scripts

Five issues in the project's bug-report template, ready to paste. Placeholders such as `#<issue A>` are to be replaced with the real issue numbers once filed. Each section ends with a "Verification" block (not part of the issue text) that records the exact command and output on upstream main and, for A, B and 4, on the fix branch.

Common environment for every run below:
- upstream main `c4ed9ba962270bf2c34d868e8c92048e85038899` (2026-10-27); the SDK installed from it reports `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` (setuptools_scm version of that commit; the last release tag is 1.0.8).
- Python 3.13.15 on Linux, httpx 0.28.1, requests 2.34.2, kubernetes 37.0.1, kubernetes_asyncio 33.3.0.
- One fresh venv per tree: `python -m venv venv && venv/bin/pip install -e "<tree>/clients/python/agentic-sandbox-client[async]"`.
- No cluster: each script replaces only the Kubernetes helper calls its path makes with in-memory stubs. The user actions use public API only.

Fix branches:
- PR 1 `origin/fix/python-sdk-release-terminated-handles` at `9feeb8a9` (prose-only amend of `0a54a028`, base `7f40dc1a`), fixes A. Measured at `0a54a028` (installed version `1.0.9.dev38+g0a54a0288`).
- PR 2 `origin/fix/python-sdk-free-closed-connectors` at `fbbbb944` (prose-only amend of `e62610f9`, base `c4ed9ba9`, current main), fixes B. Measured at `e62610f9` (installed version `1.0.9.dev41+ge62610f95`).
- PR 3 `origin/fix/python-sdk-release-client-exit-hook` at `368cedb6` (prose-only amend of `afae285e`, on PR 1 `9feeb8a9`), fixes 4. Measured at `afae285e` (installed version `1.0.9.dev39+gafae285ed`).

---

## Issue A

**Title**

Python SDK: the client keeps every terminated sandbox handle for as long as it lives

**What happened?**

I created and terminated sandboxes in a loop with one long-lived `AsyncSandboxClient`, calling `create_sandbox`, running a command and calling `sandbox.terminate()`. The process's memory grew with every cycle and never came back down.

`SandboxClient` and `AsyncSandboxClient` store every handle they create or attach in the internal `_active_connection_sandboxes` registry. `Sandbox.terminate()` and `AsyncSandbox.terminate()` close the connection and delete the claim, but they leave the handle in that registry. The client therefore keeps one closed handle per sandbox it ever terminated, until a caller closes the client itself. Each async handle keeps its closed connector, and the closed `httpx.AsyncClient` inside it keeps its SSL context with the loaded CA bundle.

I expected the client to release a terminated handle, as `client.delete_sandbox()` already does.

This causes **unbounded growth**. Memory grows linearly with the number of sandboxes terminated over the client's lifetime.

- I measured this with the reproduction below, without a cluster. The async client uses 0.69 MiB of peak RSS per create/terminate cycle, and after 200 cycles it still holds 200 closed handles.
- I also measured this on a kind cluster with Python 3.12.15, glibc 2.41 and 40 workers, each doing create, one command and terminate. The results are in `test/benchmarks/batch-claim/results/memprobe/` and `results/g4/` on the `bench/async-mem-probe` branch.
  - Over 2,400 cycles, RSS rose from 93.8 to 1,752.4 MiB and was still rising, at 72.4 MiB per 100 cycles. glibc reported 1,660 MiB in use at the end, about 0.68 MiB per cycle. Setting `MALLOC_ARENA_MAX=2` left the slope at 94.5% of the default, so the growth is live memory, not fragmentation. memray attributes 84% of tracked memory to `ssl.create_default_context` under `AsyncSandboxConnector.__init__`.
  - Over 600 cycles, RSS rose from 156 to 525 MiB, at 71.6 MiB per 100 cycles in the second half.
  - After creating 100 sandboxes and terminating all of them, the client still tracked 101 handles and kept 0.667 MiB per handle.
- I measured the sync client locally without a cluster, using `test/benchmarks/batch-claim/results/leakfix/handle_retained.py` and `handle_retained.out` on the `bench/async-mem-probe` branch. With Python 3.13.15, a kept closed `Sandbox` handle holds 0.044 MiB, or 0.036 MiB in `tracemalloc`, because the sync connector builds no SSL context per handle.
- I inferred one more cost from the code and did not measure it. The async client's exit cleanup sends one DELETE per tracked key, so every terminated handle still tracked costs one redundant DELETE at exit. The API server answers it with 404, which the client treats as success.

There are two workarounds. Call `client.delete_sandbox(claim_name, namespace)` instead of `sandbox.terminate()`, which removes the entry. Or call `client.list_active_sandboxes()` periodically, which drops closed handles. That second workaround has its own problem, #<issue 3>, because it also drops closed handles whose sandbox still exists.

See also #<issue B> and #<issue 4>. #<issue B> is a separate defect where only the cyclic garbage collector frees a dropped handle, which causes late freeing with high peaks. #<issue 4> covers the exit hook keeping closed clients alive.

**How can we reproduce it (as minimally and precisely as possible)?**

1. Run `pip install "k8s-agent-sandbox[async]"`, or install from a checkout of main with `pip install -e "clients/python/agentic-sandbox-client[async]"`.
2. Save the script below as `repro.py` and run `python repro.py`. The script needs no cluster, because it stubs the three Kubernetes calls on this path, which create the claim, wait for it to be ready and delete it. The script reads the private registry only to count what the client holds, and uses Linux `ru_maxrss` in KiB for memory.

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

Output on main:

```
terminated 200 sandboxes; client still holds 200 handles, 200 of them closed (expected 0)
peak RSS grew 0.69 MiB per create/terminate cycle (Linux ru_maxrss)
```

The optional snippet below shows the shape of the real-cluster loop that produced the measured cluster numbers above. It needs a cluster with the extensions installed and a `SandboxWarmPool`, here `python-sandbox-pool`. I did not re-run it for this report.

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

With this shape, 40 workers and a warm pool of 120, RSS grew about 72 MiB per 100 cycles on main. It went from 156 to 525 MiB over 600 cycles and from 93.8 to 1,752.4 MiB over 2,400 cycles.

**Version**

- upstream main `c4ed9ba962270bf2c34d868e8c92048e85038899`
- `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` installed from that commit, Python 3.13.15, httpx 0.28.1, Linux. The cluster numbers come from Python 3.12.15. The 2,400-cycle runs used the SDK at `7fd162d9`, which has the same code on this path as main `840fdfde`. The 600-cycle and create-100 runs used main `7f40dc1a`.

### Verification (A)

```
$ venv-main/bin/python repro_a.py      # upstream main c4ed9ba9
terminated 200 sandboxes; client still holds 200 handles, 200 of them closed (expected 0)
peak RSS grew 0.69 MiB per create/terminate cycle (Linux ru_maxrss)

$ venv-pr1/bin/python repro_a.py       # PR 1, 9feeb8a9 (prose-only amend of 0a54a028)
terminated 200 sandboxes; client still holds 0 handles, 0 of them closed (expected 0)
peak RSS grew 0.25 MiB per create/terminate cycle (Linux ru_maxrss)
```

Reproduces on main and is fixed on PR 1. About 0.25 MiB per cycle remains on PR 1 while dropped handles wait for the cyclic collector, which is Issue B in PR 2. On the cluster, all three PRs together brought the 600-cycle slope from 71.6 to 0.85 MiB per 100 cycles in `results/g4/README.md`.

---

## Issue B

**Title**

Python SDK: only the cyclic garbage collector frees a closed sandbox handle

**What happened?**

I closed sandbox handles with `terminate()`, `close_connection()`, or `client.delete_sandbox()` and dropped every reference to them. Python freed them only when its cyclic garbage collector ran.

A sandbox handle passes its `get_pod_ip`, `get_pod_name` and `get_service_fqdn` bound methods to its connector, which passes them on to its connection strategy. Handle and connector therefore form a reference cycle, and reference counting cannot free a closed handle. Only the cyclic collector can. That collector triggers on object counts, not bytes, and does not see the roughly 0.7 MiB of OpenSSL state behind each closed async handle. Memory climbs to a peak set by when the collector runs before it falls. If the program stops allocating Python objects, memory stays at that peak.

I expected reference counting to free a closed handle as soon as nothing references it.

This causes **late freeing with high peaks**, not unbounded growth. Python returns the memory only when a collection happens to run. It affects closed handles whether or not the client still tracks them.

- I measured this with the reproduction below, without a cluster. With the cyclic collector disabled, a closed, unreferenced `AsyncSandbox` is still alive, and `gc.collect()` frees it.
- I also measured this on a kind cluster with Python 3.12.15 and the async client, on top of the fix for #<issue A> so the client no longer holds terminated handles. The results are in `test/benchmarks/batch-claim/results/g3/` on the `bench/async-mem-probe` branch.
  - Over 600 create, command and terminate cycles with 40 workers, RSS grew 29.3 MiB per 100 cycles in the second half, and glibc bytes in use peaked at 309 MiB. Breaking the cycle cut RSS growth to 0.46 MiB per 100 cycles. With the cycle broken, glibc bytes in use fell from 72 MiB at the start to 41 MiB at the end.
  - After creating 100 sandboxes, terminating all of them and creating nothing more, 0.66 MiB per handle remained allocated 60 s later with an empty registry. Breaking the cycle left 0.0001 MiB per handle.
- I measured the per-handle cycle locally without a cluster, using `test/benchmarks/batch-claim/results/leakfix/handle_cycle_edges.py` and `handle_cycle_edges.out` on the `bench/async-mem-probe` branch. With Python 3.13.15 and the cyclic collector disabled, a closed, dropped `AsyncSandbox` holds 0.694 MiB before `gc.collect()`. Clearing the callbacks at close leaves 0.006 MiB.

Breaking the cycle at close does not cover handles that a caller drops without closing them. They keep their live connector until the cyclic collector runs. Close or terminate handles you are done with.

See also #<issue A>, where the client keeps every terminated handle and causes the unbounded growth. This issue makes the remaining memory return late.

**How can we reproduce it (as minimally and precisely as possible)?**

1. Run `pip install "k8s-agent-sandbox[async]"`, or install from a checkout of main with `pip install -e "clients/python/agentic-sandbox-client[async]"`.
2. Save as `repro.py` and run `python repro.py`. The script needs no cluster, because it stubs the Kubernetes calls. The script calls `client.delete_sandbox()` so that the client's own registry does not hold the handle, which is #<issue A>. A `weakref` with the cyclic collector disabled shows whether reference counting alone frees the handle.

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

Output on main:

```
closed handle with no references, before gc.collect(): alive=True (expected False)
after gc.collect(): alive=False
```

**Version**

- upstream main `c4ed9ba962270bf2c34d868e8c92048e85038899`
- `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` installed from that commit, Python 3.13.15, httpx 0.28.1, Linux.

### Verification (B)

```
$ venv-main/bin/python repro_b.py      # upstream main c4ed9ba9
closed handle with no references, before gc.collect(): alive=True (expected False)
after gc.collect(): alive=False

$ venv-pr2/bin/python repro_b.py       # PR 2, fbbbb944 (prose-only amend of e62610f9)
closed handle with no references, before gc.collect(): alive=False (expected False)
after gc.collect(): alive=False
```

Reproduces on main and is fixed on PR 2.

---

## Issue 3

**Title**

Python SDK: list_active_sandboxes() drops handles whose sandbox still exists, so cleanup never deletes them

**What happened?**

I closed a handle's connection with `sandbox.close_connection()`, which leaves the sandbox running. I then called `client.list_active_sandboxes()` and `client.delete_all()`. `client.delete_all()` did not delete the sandbox.

`list_active_sandboxes()` removes every tracked handle that is not active. The loop that pops `not obj.is_active` does this in both `sandbox_client.py` and `async_sandbox_client.py`. A handle becomes inactive when its connection closes. That happens on `close_connection()` or on a failed `get_sandbox()` lookup, where the client closes the handle locally and keeps tracking it so cleanup still reaches it. The claim behind such a handle still exists in the cluster. Once `list_active_sandboxes()` has dropped it, nothing deletes that claim, because `delete_all()`, the context managers and the exit cleanup only walk the tracked handles.

I expected listing to be read-only, or at least to drop only handles whose claim is gone, so that `delete_all()` and exit cleanup still delete the sandbox.

This causes **leaked cluster resources**. A program that closes a handle, lists, and exits leaves the SandboxClaim and its sandbox pod running. I measured this with the stubbed reproduction below, where the client never issues the DELETE. I did not run it against a cluster.

One direction is to drop only handles whose claim a successful terminate deleted, and keep reporting or tracking closed handles. Another is to report only active handles without dropping the others.

See also #<issue A>, where the client keeps terminated handles. Calling `list_active_sandboxes()` is one of its workarounds, which is how programs reach this path.

**How can we reproduce it (as minimally and precisely as possible)?**

1. Run `pip install k8s-agent-sandbox`, or install from a checkout of main with `pip install -e clients/python/agentic-sandbox-client`.
2. Save as `repro.py` and run `python repro.py`. The script needs no cluster, because it stubs the Kubernetes calls and its delete stub records which claims the client asks to delete. The first run is the control, without `list_active_sandboxes()`.

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

Output on main, with a random claim name:

```
list_active_sandboxes() called first=False: delete_all() deleted ['sandbox-claim-3b8c52f8']
list_active_sandboxes() -> []
list_active_sandboxes() called first=True: delete_all() deleted nothing
```

**Version**

- upstream main `c4ed9ba962270bf2c34d868e8c92048e85038899`
- `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` installed from that commit, Python 3.13.15, Linux.

### Verification (3)

```
$ venv-main/bin/python repro_3.py      # upstream main c4ed9ba9
list_active_sandboxes() called first=False: delete_all() deleted ['sandbox-claim-3b8c52f8']
list_active_sandboxes() -> []
list_active_sandboxes() called first=True: delete_all() deleted nothing
```

Reproduces on main. No fix branch exists.

---

## Issue 4

**Title**

Python SDK: the client's exit hook keeps every client alive until the program exits

**What happened?**

I created a client per task, closed it after use with `await client.close()` or a `with` block, and dropped it. Every client stayed in memory until the process exited.

`AsyncSandboxClient.__init__` calls `atexit.register(self._atexit_cleanup)` when `cleanup=True`, which is its default. `SandboxClient` calls `atexit.register(self._delete_automatic_sandboxes)` when `cleanup=True`, which defaults to `False` there. A bound method references its object, so the atexit registry holds each client for the life of the process, even after `close()`. Neither client ever unregisters.

I expected reference counting to free a closed client once nothing references it.

This causes **per-client retention**. Memory grows with the number of clients created, not with sandboxes. With `cleanup=False`, the atexit registry retains no client.

- I measured this with the reproduction below, without a cluster. A closed `AsyncSandboxClient` with default arguments and a `SandboxClient(cleanup=True)` after its `with` block are both still alive after `gc.collect()`.
- I measured per-client memory with Python 3.12.15, 200 clients per case, and `tracemalloc` after `gc.collect()`, using `test/benchmarks/batch-claim/results/g4/client_retained.py` and `client_retained.out` on the `bench/async-mem-probe` branch. An unused async client keeps 1,369 B. A used and closed async client keeps 9,600 B. A sync `cleanup=True` client keeps 11,407 B. All 200 stayed alive in each case.

Simply unregistering in `close()` is not enough, for three reasons. Async `close()` keeps handles whose close failed so a later call can retry. A caller can use a client again after `close()`. Sync `__exit__` leaves the client usable. Exit cleanup has to keep covering those cases and clients that callers drop without `close()`. One way is to register a function bound only to the data exit cleanup reads, instead of a bound method. That data is the `_active_connection_sandboxes` registry dict, the `_explicit_claims` set and, for async, the injected `ApiClient`. urllib3 does the same with `weakref.finalize(self, _close_pool_connections, pool)`.

See also #<issue A>.

**How can we reproduce it (as minimally and precisely as possible)?**

1. Run `pip install "k8s-agent-sandbox[async]"`, or install from a checkout of main with `pip install -e "clients/python/agentic-sandbox-client[async]"`.
2. Save as `repro.py` and run `python repro.py`. The script needs no cluster and makes no Kubernetes calls. A `weakref` shows whether Python freed each client.

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

Output on main:

```
closed AsyncSandboxClient alive after gc.collect(): True (expected False)
closed SandboxClient(cleanup=True) alive after gc.collect(): True (expected False)
```

**Version**

- upstream main `c4ed9ba962270bf2c34d868e8c92048e85038899`
- `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` installed from that commit, Python 3.13.15, Linux.

### Verification (4)

```
$ venv-main/bin/python repro_4.py      # upstream main c4ed9ba9
closed AsyncSandboxClient alive after gc.collect(): True (expected False)
closed SandboxClient(cleanup=True) alive after gc.collect(): True (expected False)

$ venv-pr3/bin/python repro_4.py       # PR 3, 368cedb6 (prose-only amend of afae285e, on PR 1 9feeb8a9)
closed AsyncSandboxClient alive after gc.collect(): False (expected False)
closed SandboxClient(cleanup=True) alive after gc.collect(): False (expected False)
```

Reproduces on main and is fixed on PR 3.

---

## Issue 5

**Title**

Python SDK: the sync connector's close() is a reset, while the async connector's close() is final

**What happened?**

I closed a sandbox connection with `close_connection()` and then sent a request through the handle's connector, once with the sync client and once with the async client. The sync connector silently reconnected and the request succeeded. The async connector raised `RuntimeError: sandbox connector is closed`.

In the sync client, `SandboxConnector.close()` does two jobs under one name.
- First, it resets a live connection so the next request can recover. `send_request` calls it on port-forward and connection errors. Strategies call their own `close()` before restarting a port-forward. The GKE snapshot extension calls `connector.close()` on a live handle during suspend and resume.
- Second, `Sandbox.close_connection()` and `terminate()` call it as the final close.

`AsyncSandboxConnector.close()` is final, and later use raises. The difference follows the HTTP libraries, not a deliberate sync/async design choice. `requests.Session` keeps working after `close()`, while `httpx` clients refuse requests after `close()`.

I expected the same lifecycle on both. After the final close, using the connector should raise a clear error.

This is **inconsistent lifecycle semantics** between the sync and async SDKs. Code that keeps a sync connection after `close_connection()` or `terminate()` gets mode-dependent behavior instead of an error. Such code holds `sandbox.connector`, or a `commands` or `files` object obtained before the close. Depending on the connection mode, it uses the pod IP, starts a new port-forward, or reconnects. I inferred one more consequence from the code and did not measure it. On a handle that the client has already closed and stopped tracking, such a reconnect reopens a port-forward or session that nothing closes again.

One direction is to split a private reset from the final `close()` in the sync connector and its strategies, and make the final close refuse later use as the async one does. The internal callers for error recovery and snapshot suspend and resume would then switch to the reset.

See also #1785, which tracks sync and async divergences in the Python SDK. The fix for #<issue B> drops a closed handle's callbacks. That changes what a reused sync connection does after close in some modes, but it does not make close final.

**How can we reproduce it (as minimally and precisely as possible)?**

1. Run `pip install "k8s-agent-sandbox[async]"`, or install from a checkout of main with `pip install -e "clients/python/agentic-sandbox-client[async]"`.
2. Save as `repro.py` and run `python repro.py`. The script needs no cluster. A local HTTP server stands in for the sandbox router, and the script stubs the Kubernetes calls. `sandbox.connector.send_request()` is the call the `commands` and `files` objects make.

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

Output on main:

```
sync  connector after close_connection(): 200
async connector after close_connection(): RuntimeError sandbox connector is closed
```

**Version**

- upstream main `c4ed9ba962270bf2c34d868e8c92048e85038899`
- `k8s-agent-sandbox` `1.0.9.dev40+gc4ed9ba96` installed from that commit, Python 3.13.15, requests 2.34.2, httpx 0.28.1, Linux.

### Verification (5)

```
$ venv-main/bin/python repro_5.py      # upstream main c4ed9ba9
sync  connector after close_connection(): 200
async connector after close_connection(): RuntimeError sandbox connector is closed
```

Reproduces on main. No fix branch exists.

---

## Verification summary

| issue | reproduces on main `c4ed9ba9` | fixed on branch |
| --- | --- | --- |
| A | yes: 200 of 200 terminated handles kept, 0.69 MiB peak RSS per cycle | yes, PR 1 `9feeb8a9` (prose-only amend of `0a54a028`): 0 kept (about 0.25 MiB per cycle left until the GC runs, which is B) |
| B | yes: closed, dropped handle alive until `gc.collect()` | yes, PR 2 `fbbbb944` (prose-only amend of `e62610f9`): freed without the GC |
| 3 | yes: `delete_all()` deletes nothing after `list_active_sandboxes()` | no fix branch |
| 4 | yes: closed async client and sync `cleanup=True` client both alive | yes, PR 3 `368cedb6` (prose-only amend of `afae285e`, on PR 1 `9feeb8a9`): both freed |
| 5 | yes: sync returns 200 after close, async raises | no fix branch |

Each script was run as `python repro_<x>.py` in its own venv with the SDK installed editable from a detached worktree of the named commit; stderr was captured with stdout and was empty in every run.
