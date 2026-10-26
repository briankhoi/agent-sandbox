"""Memory kept per client that was created and closed, and whether the client is freed.

Clients register an exit hook by default (async) or with cleanup=True (sync). Measures 200 clients per case:
how many are still alive after gc.collect(), and the Python memory traced by tracemalloc that stays per client.
The GC stays on; gc.collect() runs before each reading so only memory that is really still referenced counts.
"""
import asyncio
import gc
import sys
import tracemalloc
import weakref
from unittest.mock import patch

from k8s_agent_sandbox.async_sandbox_client import AsyncSandboxClient
from k8s_agent_sandbox.models import SandboxdInClusterConnectionConfig
from k8s_agent_sandbox.sandbox_client import SandboxClient

N = 200
CONFIG = SandboxdInClusterConnectionConfig(mode="in-cluster-pod-ip")


def traced():
    gc.collect()
    return tracemalloc.get_traced_memory()[0]


def report(name, refs, t0):
    alive = sum(1 for r in refs if r() is not None)
    print(f"{name}: {alive}/{N} closed clients alive after gc.collect(), "
          f"{(traced() - t0) / N:.0f} B traced per client")


async def async_case(name, used, cleanup=True):
    t0 = traced()
    refs = []
    for _ in range(N):
        client = AsyncSandboxClient(connection_config=CONFIG, cleanup=cleanup)
        if used:
            await client.k8s_helper._ensure_initialized()
        await client.close()
        refs.append(weakref.ref(client))
        del client
    report(name, refs, t0)


def sync_case(name, cleanup):
    t0 = traced()
    refs = []
    for _ in range(N):
        with SandboxClient(connection_config=CONFIG, cleanup=cleanup) as client:
            pass
        refs.append(weakref.ref(client))
        del client
    report(name, refs, t0)


async def main():
    import k8s_agent_sandbox
    print(sys.version.split()[0], k8s_agent_sandbox.__file__)
    tracemalloc.start()
    # One of each first, so first-use imports and caches aren't counted.
    await async_case("warm-up", used=True)
    sync_case("warm-up", cleanup=True)
    await async_case("async cleanup=False, used", used=True, cleanup=False)
    await async_case("async default (cleanup=True), never used", used=False)
    await async_case("async default (cleanup=True), used then closed", used=True)
    sync_case("sync cleanup=False, with block", cleanup=False)
    sync_case("sync cleanup=True, with block", cleanup=True)


# Clients created here register real exit hooks; the process exits right after, so they only run on empty registries.
with patch("kubernetes.config.load_kube_config"):
    asyncio.run(main())
