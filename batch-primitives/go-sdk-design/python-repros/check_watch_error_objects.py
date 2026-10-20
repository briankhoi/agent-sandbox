"""B7(c): how Python TOP (8a72827f) SandboxBatch._watch_loop treats malformed in-stream ERROR events.

Runs the REAL _watch_loop against a stub HTTP server whose watch stream carries one line, then ends.
Modes:
  nocode   {"type":"ERROR","object":{"kind":"Status","apiVersion":"v1","status":"Failure"}}   (no code)
  claimobj {"type":"ERROR","object":{"kind":"SandboxClaim",...}}                          (not a Status)
  garbage  a line that is not JSON
  code0    a Status with "code": 0
  code500  a well-formed Status with code 500 and a reason (control: should be retried)
Usage (from clients/python/agentic-sandbox-client of the TOP checkout):
    PYTHONPATH=$PWD python check_watch_error_objects.py <mode> [seconds]
"""
import http.server
import json
import socketserver
import sys
import threading
import time

from kubernetes import client

from k8s_agent_sandbox import batch_state, sandbox_batch
from k8s_agent_sandbox.k8s_helper import K8sHelper

MODE = sys.argv[1]
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
LINES = {
    "nocode": json.dumps({"type": "ERROR", "object": {"kind": "Status", "apiVersion": "v1", "status": "Failure"}}),
    "claimobj": json.dumps({"type": "ERROR", "object": {"kind": "SandboxClaim", "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
                                                         "metadata": {"name": "b-0", "resourceVersion": "101"}}}),
    "garbage": "<html>502 Bad Gateway</html>",
    "code0": json.dumps({"type": "ERROR", "object": {"kind": "Status", "apiVersion": "v1", "code": 0, "reason": "", "message": ""}}),
    "code500": json.dumps({"type": "ERROR", "object": {"kind": "Status", "apiVersion": "v1", "code": 500,
                                                        "reason": "InternalError", "message": "boom"}}),
}
watch_requests = []


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if "watch=true" in self.path or "watch=True" in self.path:
            watch_requests.append(time.monotonic())
            line = (LINES[MODE] + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(line), line))
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


class _C:
    k8s_helper = helper


b._client = _C()
t = threading.Thread(target=b._watch_loop, daemon=True)
t.start()
t.join(timeout=DURATION)
alive = t.is_alive()
b._watch_stop.set()
t.join(timeout=40)
err = b._state.error()
print(f"mode={MODE}: {len(watch_requests)} watch requests in {DURATION}s; loop "
      f"{'still running (retrying)' if alive else 'EXITED'}; err()={type(err).__name__ + ': ' + str(err) if err else None}")
