"""Repro (alignment A4): run the real SandboxBatch._watch_loop against a 503 stub, with
  base     = the code as is
  gt0      = retry_delay ignores a Retry-After of 0 (the suggested `> 0` fix)
  gt0_nour = gt0 + the claims API client built with urllib3 retries off (configuration.retries = False)
Counts HTTP watch requests (server side) and SDK-level watch attempts (calls to watch_sandbox_claims).
Usage (from clients/python/agentic-sandbox-client, PYTHONPATH=$PWD): python check_retry_after_zero_layers.py VARIANT MODE [seconds]
MODE: ra0 | ra1 | none
"""
import http.server
import socketserver
import sys
import threading
import time

from kubernetes import client

from k8s_agent_sandbox import batch_state, batch_utils, sandbox_batch
from k8s_agent_sandbox import k8s_helper as kh
from k8s_agent_sandbox.k8s_helper import K8sHelper

VARIANT, MODE = sys.argv[1], sys.argv[2]
DURATION = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
http_watches = []
sdk_watches = []


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if "watch=true" in self.path or "watch=True" in self.path:
            http_watches.append(time.monotonic())
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


if VARIANT in ("gt0", "gt0_nour"):
    orig_backoff = batch_utils.backoff_delay

    def retry_delay(attempt, error):
        headers = getattr(error, "headers", None)
        retry_after = headers.get("Retry-After") if headers else None
        seconds = retry_after.strip() if isinstance(retry_after, str) else ""
        if seconds.isascii() and seconds.isdigit() and int(seconds) > 0:
            return min(float(seconds), batch_utils.BATCH_BACKOFF_MAX_SECONDS)
        return orig_backoff(attempt, batch_utils.BATCH_BACKOFF_BASE_SECONDS, batch_utils.BATCH_BACKOFF_MAX_SECONDS)

    batch_utils.retry_delay = retry_delay

srv = Server(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
cfg = client.Configuration()
cfg.host = f"http://127.0.0.1:{srv.server_address[1]}"
api = client.ApiClient(cfg)
helper = K8sHelper(api)
if VARIANT == "gt0_nour":
    helper.custom_objects_api = client.CustomObjectsApi(kh._api_client_without_retries(api))

orig_watch = helper.watch_sandbox_claims


def counting_watch(*a, **k):
    sdk_watches.append(time.monotonic())
    return orig_watch(*a, **k)


helper.watch_sandbox_claims = counting_watch

b = sandbox_batch.SandboxBatch.__new__(sandbox_batch.SandboxBatch)
b.batch_id = "b"
b.namespace = "ns"
b._list_resource_version = "100"
b._watch_stop = threading.Event()
b._lock = threading.Lock()
b._changed = threading.Condition(b._lock)
b._state = batch_state.BatchState("b", [])
b._connected = {}


class _C:
    k8s_helper = helper


b._client = _C()
t = threading.Thread(target=b._watch_loop, daemon=True)
t.start()
time.sleep(DURATION)
b._watch_stop.set()
t.join(timeout=40)
gaps = [round(y - x, 3) for x, y in zip(http_watches, http_watches[1:])]
print(f"{VARIANT:8s} {MODE:4s}: {len(http_watches):3d} HTTP watch requests, {len(sdk_watches):3d} SDK watch attempts in "
      f"{DURATION}s; err()={b._state.error()!r}; first gaps={gaps[:8]}")
