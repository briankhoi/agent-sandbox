"""Which edits free a closed, dropped AsyncSandbox by reference counting alone (GC off)."""
import asyncio
import ctypes
import gc
from unittest.mock import MagicMock

from k8s_agent_sandbox.async_sandbox import AsyncSandbox
from k8s_agent_sandbox.models import SandboxdInClusterConnectionConfig

libc = ctypes.CDLL("libc.so.6")


class M(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in "arena ordblks smblks hblks hblkhd usmblks fsmblks uordblks fordblks keepcost".split()]


libc.mallinfo2.restype = M
N = 100


def clear_callbacks(s):
    c = s.connector
    c._get_pod_ip = c._get_service_fqdn = None
    st = c._sandboxd_strategy
    if st is not None:
        for name in ("_get_pod_ip", "_get_service_fqdn", "_get_pod_name"):
            if hasattr(st, name):
                setattr(st, name, None)


def drop_client(s):
    s.connector.client = None


async def trial(name, after_close):
    gc.collect()
    gc.disable()
    u0 = libc.mallinfo2().uordblks
    for i in range(N):
        s = AsyncSandbox(claim_name=f"c{i}", sandbox_id=f"s{i}", namespace="ns",
                         connection_config=SandboxdInClusterConnectionConfig(mode="in-cluster-pod-ip"), k8s_helper=MagicMock())
        await s.close_connection()
        after_close(s)
        del s
    held = (libc.mallinfo2().uordblks - u0) / N / 2**20
    found = gc.collect()
    gc.enable()
    print(f"{name}: {held:.3f} MiB per dropped handle before gc.collect(), {found / N:.0f} unreachable objects per handle")


async def main():
    await trial("as is", lambda s: None)
    await trial("connector and strategy callbacks cleared", clear_callbacks)
    await trial("connector.client dropped", drop_client)


asyncio.run(main())
