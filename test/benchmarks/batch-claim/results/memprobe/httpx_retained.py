"""Malloc bytes pinned by a closed httpx.AsyncClient that stays referenced (as a terminated
AsyncSandbox handle does in AsyncSandboxClient._active_connection_sandboxes)."""
import asyncio
import ctypes
import gc
import tracemalloc

import httpx

libc = ctypes.CDLL("libc.so.6")


class M(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in "arena ordblks smblks hblks hblkhd usmblks fsmblks uordblks fordblks keepcost".split()]


libc.mallinfo2.restype = M


def used():
    return libc.mallinfo2().uordblks / 2**20


async def main(n=200):
    tracemalloc.start()
    keep = []
    u0, t0 = used(), tracemalloc.get_traced_memory()[0] / 2**20
    for _ in range(n):
        c = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(verify=True), timeout=httpx.Timeout(60.0))
        await c.aclose()
        keep.append(c)
    print(f"httpx {httpx.__version__}: malloc in-use per kept closed client {(used() - u0) / n:.3f} MB, "
          f"tracemalloc-traced {(tracemalloc.get_traced_memory()[0] / 2**20 - t0) / n:.3f} MB")
    keep.clear()
    gc.collect()
    print(f"after dropping the references, malloc in-use delta {used() - u0:.1f} MB (from {n} clients)")


asyncio.run(main())
