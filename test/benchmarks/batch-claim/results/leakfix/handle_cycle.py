"""Whether a dropped, closed AsyncSandbox is freed by reference counting or only by the cyclic GC.
The handle passes its bound methods to its connector, so handle and connector form a cycle."""
import asyncio
import ctypes
import gc
import sys
from unittest.mock import MagicMock

from k8s_agent_sandbox.async_sandbox import AsyncSandbox
from k8s_agent_sandbox.models import SandboxdInClusterConnectionConfig

libc = ctypes.CDLL("libc.so.6")


class M(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in "arena ordblks smblks hblks hblkhd usmblks fsmblks uordblks fordblks keepcost".split()]


libc.mallinfo2.restype = M
MB = 2**20
N = 200


def used():
    return libc.mallinfo2().uordblks / MB


async def main():
    gc.collect()
    gc.disable()
    u0 = used()
    for i in range(N):
        s = AsyncSandbox(claim_name=f"c{i}", sandbox_id=f"s{i}", namespace="ns",
                         connection_config=SandboxdInClusterConnectionConfig(mode="in-cluster-pod-ip"), k8s_helper=MagicMock())
        await s.close_connection()
        del s
    print(f"{sys.version.split()[0]}: after creating, closing and dropping {N} handles with the GC off, "
          f"{(used() - u0) / N:.3f} MiB per handle still allocated")
    found = gc.collect()
    print(f"gc.collect() found {found} unreachable objects; now {(used() - u0) / N:.3f} MiB per handle")
    gc.enable()


asyncio.run(main())
