"""Memory kept per AsyncSandboxClient that was created and closed, and whether it is freed.
The client registers atexit.register(self._atexit_cleanup), so atexit keeps a reference to it."""
import asyncio
import ctypes
import gc
import sys
import tracemalloc
import weakref

from k8s_agent_sandbox.async_sandbox_client import AsyncSandboxClient
from k8s_agent_sandbox.models import SandboxdInClusterConnectionConfig

libc = ctypes.CDLL("libc.so.6")


class M(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in "arena ordblks smblks hblks hblkhd usmblks fsmblks uordblks fordblks keepcost".split()]


libc.mallinfo2.restype = M
N = 200


def used():
    gc.collect()
    return libc.mallinfo2().uordblks / 2**20


async def trial(cleanup):
    u0 = used()
    t0 = tracemalloc.get_traced_memory()[0]
    refs = []
    for _ in range(N):
        client = AsyncSandboxClient(connection_config=SandboxdInClusterConnectionConfig(mode="in-cluster-pod-ip"), cleanup=cleanup)
        await client.k8s_helper._ensure_initialized() if hasattr(client.k8s_helper, "_ensure_initialized") else None
        await client.close()
        refs.append(weakref.ref(client))
        del client
    alive = sum(1 for r in refs if r() is not None)
    malloc_kib = (used() - u0) / N * 1024
    traced_kib = (tracemalloc.get_traced_memory()[0] - t0) / N / 1024
    print(f"cleanup={cleanup}: {alive}/{N} closed clients still alive after gc.collect(), "
          f"{malloc_kib:.1f} KiB malloc and {traced_kib:.1f} KiB traced by tracemalloc retained per client")


async def main():
    print(sys.version.split()[0])
    tracemalloc.start()
    await trial(cleanup=False)
    await trial(cleanup=True)


asyncio.run(main())
