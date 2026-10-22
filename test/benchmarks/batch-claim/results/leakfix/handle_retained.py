"""Malloc bytes held by a closed Sandbox / AsyncSandbox handle that stays referenced, as a
terminated handle does in the client's _active_connection_sandboxes, versus dropped."""
import asyncio
import ctypes
import gc
import sys
import tracemalloc
from unittest.mock import MagicMock

from k8s_agent_sandbox.async_sandbox import AsyncSandbox
from k8s_agent_sandbox.models import SandboxdInClusterConnectionConfig
from k8s_agent_sandbox.sandbox import Sandbox

libc = ctypes.CDLL("libc.so.6")


class M(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in "arena ordblks smblks hblks hblkhd usmblks fsmblks uordblks fordblks keepcost".split()]


libc.mallinfo2.restype = M
MB = 2**20
N = 200
CONFIG = SandboxdInClusterConnectionConfig(mode="in-cluster-pod-ip")


def used():
    gc.collect()
    return libc.mallinfo2().uordblks / MB


def report(shell, u0, t0, keep):
    kept = (used() - u0) / N
    traced = (tracemalloc.get_traced_memory()[0] / MB - t0) / N
    keep.clear()
    dropped = (used() - u0) / N
    print(f"{shell}: kept closed handle {kept:.3f} MiB (tracemalloc {traced:.3f} MiB), after dropping {dropped:.3f} MiB per handle")


def sync_handles():
    keep = []
    u0, t0 = used(), tracemalloc.get_traced_memory()[0] / MB
    for i in range(N):
        s = Sandbox(claim_name=f"c{i}", sandbox_id=f"s{i}", namespace="ns", connection_config=CONFIG, k8s_helper=MagicMock())
        s.close_connection()
        keep.append(s)
    report("sync Sandbox", u0, t0, keep)


async def async_handles():
    keep = []
    u0, t0 = used(), tracemalloc.get_traced_memory()[0] / MB
    for i in range(N):
        s = AsyncSandbox(claim_name=f"c{i}", sandbox_id=f"s{i}", namespace="ns", connection_config=CONFIG, k8s_helper=MagicMock())
        await s.close_connection()
        keep.append(s)
    report("async AsyncSandbox", u0, t0, keep)


tracemalloc.start()
import k8s_agent_sandbox  # noqa: E402
print(sys.version.split()[0], k8s_agent_sandbox.__file__)
sync_handles()
asyncio.run(async_handles())
