"""Repro: SandboxBatch._watch_loop retries at once, with no backoff, when every watch request gets a
retryable error carrying ``Retry-After: 0``, because ``batch_utils.retry_delay`` returns the header's
value (0 s) instead of falling back to the jittered backoff.

kube-apiserver itself only sends Retry-After when it is > 0, so this needs a proxy or load balancer
that answers 503 with ``Retry-After: 0``. The Go design ignores a suggested delay of 0 (finding F8).

Runs the REAL _watch_loop against a stub HTTP server; only the handle fields the loop reads are set.
Usage (from clients/python/agentic-sandbox-client of the Python checkout, PYTHONPATH=$PWD):
    python check_retry_after_zero.py [ra0|ra1|none] [seconds]
  ra0  = 503 + Retry-After: 0   (the bug)
  ra1  = 503 + Retry-After: 1   (control: one request per second)
  none = 503 without Retry-After (control: jittered backoff)
"""
import http.server
import socketserver
import sys
import threading
import time

from kubernetes import client

from k8s_agent_sandbox import batch_state, sandbox_batch
from k8s_agent_sandbox.k8s_helper import K8sHelper

MODE = sys.argv[1] if len(sys.argv) > 1 else "ra0"
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0

watch_requests = []


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if "watch=true" in self.path or "watch=True" in self.path:
            watch_requests.append(time.monotonic())
            body = b'{"kind":"Status","apiVersion":"v1","status":"Failure","code":503,"reason":"ServiceUnavailable","message":"proxy"}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            if MODE == "ra0":
                self.send_header("Retry-After", "0")
            elif MODE == "ra1":
                self.send_header("Retry-After", "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"kind":"List","apiVersion":"v1","metadata":{"resourceVersion":"100"},"items":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


srv = Server(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

cfg = client.Configuration()
cfg.host = f"http://127.0.0.1:{srv.server_address[1]}"
helper = K8sHelper(client.ApiClient(cfg))

b = sandbox_batch.SandboxBatch.__new__(sandbox_batch.SandboxBatch)
b.batch_id = "b"
b.namespace = "ns"
b._list_resource_version = "100"
b._watch_stop = threading.Event()
b._lock = threading.Lock()
b._changed = threading.Condition(b._lock)
b._state = batch_state.BatchState("b", [])
b._connected = {}  # read by the transport branch's stale-connection check


class _C:
    k8s_helper = helper


b._client = _C()

t = threading.Thread(target=b._watch_loop, daemon=True)
t.start()
time.sleep(DURATION)
b._watch_stop.set()
t.join(timeout=40)
gaps = [round(y - x, 3) for x, y in zip(watch_requests, watch_requests[1:])]
print(f"mode={MODE}: {len(watch_requests)} watch requests in {DURATION}s; err()={b._state.error()!r}; "
      f"first gaps={gaps[:6]} last gaps={gaps[-3:]}")
