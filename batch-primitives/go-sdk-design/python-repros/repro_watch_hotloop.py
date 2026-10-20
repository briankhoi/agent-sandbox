"""Repro: SandboxBatch._watch_loop (Python TOP 8a72827f) hot-loops when the apiserver (or a proxy in
front of it) answers a watch with 200 and ends the body immediately.

Runs the REAL _watch_loop against a stub HTTP server. Nothing else is mocked except the handle's
fields the loop reads. Usage (from clients/python/agentic-sandbox-client of the TOP checkout):
    python repro_watch_hotloop.py
"""
import http.server
import socket
import socketserver
import sys
import threading
import time

from kubernetes import client

from k8s_agent_sandbox import batch_state, sandbox_batch
from k8s_agent_sandbox.k8s_helper import K8sHelper

MODE = sys.argv[1] if len(sys.argv) > 1 else "empty200"
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0

watch_requests = []


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if "watch=true" in self.path or "watch=True" in self.path:
            watch_requests.append(time.monotonic())
            if MODE == "reset":
                # Drop the connection before any response: a call-time transport error.
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            # 200 with an empty chunked body: the stream ends at once.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"0\r\n\r\n")
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

# Build a handle with only the fields _watch_loop reads.
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
start = time.monotonic()
t.start()
time.sleep(DURATION)
b._watch_stop.set()
t.join(timeout=40)
gaps = [round(y - x, 3) for x, y in zip(watch_requests, watch_requests[1:])]
print(f"mode={MODE}: {len(watch_requests)} watch requests in {DURATION}s; err()={b._state.error()!r}; "
      f"first gaps={gaps[:8]} last gaps={gaps[-4:]}")
