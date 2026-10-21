"""Repro (alignment A4 layer 2, A15): how many HTTP requests does ONE sync batch API call send?

The sync kubernetes client's urllib3 pool uses urllib3's default Retry(3) unless configuration.retries is
set. For GET, PUT and DELETE it re-sends a request (a) after a read timeout, with a fresh timeout, and (b)
after a 413/429/503 that carries Retry-After, sleeping the header's value first. POST is retried on
neither. The batch's Lease client is built with retries off (_api_client_without_retries) and is the
control. The async client (kubernetes_asyncio/aiohttp) sends one request: see check_async_retries.py.

Usage (from clients/python/agentic-sandbox-client of the Python checkout, PYTHONPATH=$PWD):
    python check_urllib3_retries.py
"""
import http.server
import socket
import socketserver
import threading
import time

import urllib3
from kubernetes import client

from k8s_agent_sandbox.k8s_helper import K8sHelper

G, V, P = "extensions.agents.x-k8s.io", "v1beta1", "sandboxclaims"
RA = {"value": "0"}
hits = {"GET": 0, "DELETE": 0, "PUT": 0, "POST": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _503(self, method):
        if method in ("POST", "PUT"):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
        hits[method] += 1
        body = b'{"kind":"Status","apiVersion":"v1","status":"Failure","code":503,"reason":"ServiceUnavailable","message":"proxy"}'
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        if RA["value"] != "none":
            self.send_header("Retry-After", RA["value"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._503("GET")

    def do_DELETE(self):
        self._503("DELETE")

    def do_PUT(self):
        self._503("PUT")

    def do_POST(self):
        self._503("POST")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def run(label, method, call):
    before = hits[method]
    t0 = time.monotonic()
    try:
        call()
        outcome = "returned"
    except Exception as e:  # noqa: BLE001
        outcome = f"raised {type(e).__name__}"
    print(f"  {label}: 1 call -> {hits[method] - before} HTTP {method}s in {time.monotonic() - t0:.2f}s, {outcome}")


srv = Server(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
cfg = client.Configuration()
cfg.host = f"http://127.0.0.1:{srv.server_address[1]}"
helper = K8sHelper(client.ApiClient(cfg))
print(f"urllib3 {urllib3.__version__}; claims client pool retries: "
      f"{helper.custom_objects_api.api_client.rest_client.pool_manager.connection_pool_kw.get('retries')} (None = Retry(3))")
for ra in ("0", "1", "none"):
    RA["value"] = ra
    print(f"503 with Retry-After: {ra}")
    run("watch", "GET", lambda: list(helper.watch_sandbox_claims("ns", "x=y", "1", 30, _request_timeout=35)))
    run("delete", "DELETE", lambda: helper.custom_objects_api.delete_namespaced_custom_object(G, V, "ns", P, "b-0", _request_timeout=30))
    run("create", "POST", lambda: helper.custom_objects_api.create_namespaced_custom_object(G, V, "ns", P, {"metadata": {"name": "b-0"}}, _request_timeout=30))
    run("Lease PUT (no-retry client)", "PUT", lambda: helper.coordination_v1_api.replace_namespaced_lease(
        "batch-b", "ns", client.V1Lease(metadata=client.V1ObjectMeta(name="batch-b")), _request_timeout=5))

# A server that accepts connections and never answers: each retry gets a fresh timeout.
accepted = []
lsock = socket.socket()
lsock.bind(("127.0.0.1", 0))
lsock.listen(64)


def hang():
    while True:
        c, _ = lsock.accept()
        accepted.append(c)


threading.Thread(target=hang, daemon=True).start()
cfg2 = client.Configuration()
cfg2.host = f"http://127.0.0.1:{lsock.getsockname()[1]}"
h2 = K8sHelper(client.ApiClient(cfg2))
print("server never answers, _request_timeout=1")
for label, call in [
    ("list (GET)", lambda: h2.custom_objects_api.list_namespaced_custom_object(G, V, "ns", P, _request_timeout=1)),
    ("delete (DELETE)", lambda: h2.custom_objects_api.delete_namespaced_custom_object(G, V, "ns", P, "b-0", _request_timeout=1)),
    ("create (POST)", lambda: h2.custom_objects_api.create_namespaced_custom_object(G, V, "ns", P, {"metadata": {"name": "b-0"}}, _request_timeout=1)),
    ("Lease GET (no-retry client)", lambda: h2.coordination_v1_api.read_namespaced_lease("batch-b", "ns", _request_timeout=1)),
]:
    before = len(accepted)
    t0 = time.monotonic()
    try:
        call()
    except Exception as e:  # noqa: BLE001
        print(f"  {label}: {len(accepted) - before} connections in {time.monotonic() - t0:.2f}s, raised {type(e).__name__}")
