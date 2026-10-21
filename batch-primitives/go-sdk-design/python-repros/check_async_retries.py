"""Repro (alignment A9/A15): does kubernetes_asyncio (aiohttp) retry a 503 + Retry-After internally, as urllib3 does for the
sync client? Counts server-side HTTP requests for one AsyncK8sHelper watch call, one delete and one
Lease PUT. Run from clients/python/agentic-sandbox-client with PYTHONPATH=$PWD.
"""
import asyncio
import http.server
import socketserver
import sys
import threading
import time

import kubernetes_asyncio
from kubernetes_asyncio import client

from k8s_agent_sandbox.async_k8s_helper import AsyncK8sHelper

RA = sys.argv[1] if len(sys.argv) > 1 else "0"
hits = {"GET": 0, "DELETE": 0, "PUT": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _503(self, method):
        hits[method] += 1
        body = b'{"kind":"Status","apiVersion":"v1","status":"Failure","code":503,"reason":"ServiceUnavailable","message":"proxy"}'
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        if RA != "none":
            self.send_header("Retry-After", RA)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._503("GET")

    def do_DELETE(self):
        self._503("DELETE")

    def do_PUT(self):
        self._503("PUT")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


async def main():
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = client.Configuration()
    cfg.host = f"http://127.0.0.1:{srv.server_address[1]}"
    api = client.ApiClient(cfg)
    helper = AsyncK8sHelper(api)
    print(f"kubernetes_asyncio {kubernetes_asyncio.__version__}, Retry-After={RA}")
    t0 = time.monotonic()
    try:
        async for _ in helper.watch_sandbox_claims("ns", "x=y", "1", 30, _request_timeout=35):
            pass
    except Exception as e:  # noqa: BLE001
        print(f"  watch: 1 SDK call -> {hits['GET']} HTTP GETs in {time.monotonic() - t0:.2f}s, raised {type(e).__name__} status={getattr(e, 'status', None)}")
    t0 = time.monotonic()
    try:
        await helper.custom_objects_api.delete_namespaced_custom_object(
            "extensions.agents.x-k8s.io", "v1beta1", "ns", "sandboxclaims", "b-0", _request_timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"  delete: 1 SDK call -> {hits['DELETE']} HTTP DELETEs in {time.monotonic() - t0:.2f}s, raised {type(e).__name__} status={getattr(e, 'status', None)}")
    t0 = time.monotonic()
    try:
        await helper.coordination_v1_api.replace_namespaced_lease(
            "batch-b", "ns", client.V1Lease(metadata=client.V1ObjectMeta(name="batch-b")), _request_timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"  lease PUT: 1 SDK call -> {hits['PUT']} HTTP PUTs in {time.monotonic() - t0:.2f}s, raised {type(e).__name__} status={getattr(e, 'status', None)}")
    await api.close()


asyncio.run(main())
